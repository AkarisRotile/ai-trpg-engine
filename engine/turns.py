"""回合管理器：把 KP、PL、骰子内核、记忆与模组串成一个跑团循环。

一轮的时序：

    KP 生成 <narr> 公开叙述
        ↓
    引擎执行 <state> 指令（掷骰 / 伤害 / SAN / 私发线索 / 切场景）
        ↓
    全体 PL 并行行动（探索轮互不可见当轮他人输出，避免人设互相迎合）
        ↓
    引擎解析 <ooc> 里的检定申请并掷骰（骰子永远在引擎手里）
        ↓
    汇总「全体 act + 桌边闲聊 + 骰子结果」交回 KP，进入下一轮
"""

from __future__ import annotations

import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import yaml

from . import chargen, memory as memory_mod, module_lib, rules as rules_mod
from . import clock as clock_mod
from . import config as cfgmod
from . import player_memory, spoiler
from . import study as study_mod
from .agents import BaseAgent, ChannelOutput, Directive, KPAgent, PLAgent
from .dice import DiceKernel
from .llm import LLMError
from .session import Session


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + " …（后略）"


class GameLoop:
    """驱动一整局。所有对外通知都通过 emit 回调（事件总线）。"""

    def __init__(self, cfg: dict[str, Any], session: Session,
                 emit: Callable[[dict[str, Any]], None],
                 module: module_lib.Module | None = None,
                 study_mode: bool = False) -> None:
        self.cfg = cfg
        self.session = session
        self.emit_raw = emit
        self.module = module
        self.study_mode = study_mode or session.mode == "study"
        self.options = dict(cfg.get("options") or {})

        seed = random.SystemRandom().randrange(1, 2 ** 31)
        self.kernel = DiceKernel(seed=seed)
        self.kernel.set_turn(session.round)

        kp_seats = [s for s in cfg.get("seats", [])
                    if s.get("kind") == "KP" and s.get("enabled", True)]
        pl_seats = [s for s in cfg.get("seats", [])
                    if s.get("kind") == "PL" and s.get("enabled", True)]
        if not kp_seats:
            raise RuntimeError("没有可用的守秘人座位。请先在设置里配置 KP。")
        if not pl_seats and not self.study_mode:
            raise RuntimeError("没有可用的玩家座位。请在设置里至少配置一名 PL。"
                              "（只想让 KP 读模组的话，用「📖 模组研读室」。）")

        self.kp: KPAgent | None = None
        self.pls: list[PLAgent] = []
        self._kp_seat = kp_seats[0]
        self._pl_seats = pl_seats

        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.last_narr = ""
        self._last_scene_id = ""
        self._round_publics: list[dict[str, Any]] = []
        self._kp_pending_dice: list[str] = []
        # 明骰：这一轮全桌都看见的骰子，下一轮发给所有 PL（含掷的人自己）
        self._public_dice: list[str] = []
        # 桌上的钟：模型自己算日子一定会把什么都叫"昨天"，所以引擎替它算
        self._clock_dirty = False
        # 桌边插话轮里"这次轮到谁接话"的游标（免得每次都点同一个人）
        self._tt_cursor = 0
        if session.clock is None:
            # 守秘人研读模组时自己挑的开场日，优先级高于引擎的猜。
            study = session.module_study or {}
            session.clock = clock_mod.resolve_start(
                module, self.options, picked=str(study.get("clock") or ""))

    # ══════════════════════════════════════════════ 时间

    def _tick_clock(self) -> str:
        """每轮开头拨一次钟，并给出这一轮的「现在几点 + 日期对照表」。

        默认每轮往前走一点点（可配 `minutes_per_round`，设 0 就完全由守秘人说了算）。
        守秘人上一轮要是自己写了 `advance`/`time`，这里就不再叠加默认值——
        否则"等了两个钟头"会被引擎偷偷再加十分钟。
        """
        c = self.session.clock
        if c is None:
            return ""
        if not self._clock_dirty:
            c.advance(int(self.options.get("minutes_per_round", 10) or 0))
        self._clock_dirty = False
        # 让新长出来的记忆自动带上时间戳：没有它，"这是三天前听来的"
        # 这种话模型根本说不出来，它只会说"刚才"。
        stamp = c.stamp()
        for agent in [self.kp, *self.pls]:
            if agent is not None and getattr(agent, "memory", None) is not None:
                agent.memory.now = stamp
        return c.render(
            past_days=int(self.options.get("clock_past_days", 7) or 7),
            future_days=int(self.options.get("clock_future_days", 3) or 3),
        )

    # ══════════════════════════════════════════════ 事件

    def _e(self, etype: str, text: str = "", name: str = "", seat_id: str = "",
           meta: dict[str, Any] | None = None) -> dict[str, Any]:
        evt = {"type": etype, "text": text, "name": name, "seat_id": seat_id,
               "meta": meta or {}}
        self.session.log(evt)
        try:
            self.emit_raw(evt)
        except Exception:
            pass
        return evt

    def request_stop(self) -> None:
        self._stop.set()

    def reset_stop(self) -> None:
        self._stop.clear()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # ══════════════════════════════════════════════ 建座位

    def _ensure_pls(self) -> None:
        if self.pls:
            return
        for seat in self._pl_seats:
            agent = PLAgent(
                seat, self.options, self.session.session_id,
                seed=random.SystemRandom().randrange(1, 2 ** 31),
                setting=self._setting(),
            )
            agent.kernel = self.kernel      # AI 只能通过它申请掷骰
            self.pls.append(agent)

    def _ensure_kp(self) -> None:
        """守秘人**延迟到开局才构造**——因为它的 system 里要带全班调查员的资料。"""
        if self.kp is not None:
            return
        self.kp = KPAgent(
            self._kp_seat, self.options, self.session.session_id,
            module_title=(self.module.title if self.module else ""),
            premise=self.session.premise,
            module_brief=self._module_brief(include_party=False),
            seed=random.SystemRandom().randrange(1, 2 ** 31),
            setting=self._setting(),
        )
        self.kp.kernel = self.kernel

    @staticmethod
    def _player_of(agent: Any) -> str:
        prof = agent.seat.get("profile") or {}
        return prof.get("player_name") or agent.display_name

    @staticmethod
    def _fix_pointbuy(attrs: dict[str, int], total: int = 480,
                      lo: int = 40, hi: int = 90) -> dict[str, int]:
        """把不合规的购点方案拉回预算内（夹到区间 + 均摊差额）。

        注意参数名不能叫 min/max——那会把内置函数遮住，
        函数体里的 min()/max() 就变成"调用一个整数"了。
        """
        out = {k: max(lo, min(hi, int(attrs.get(k) or lo)))
               for k in chargen.ATTR_ORDER}
        diff = total - sum(out.values())
        guard = 0
        while diff != 0 and guard < 4000:
            guard += 1
            for k in chargen.ATTR_ORDER:
                if diff == 0:
                    break
                if diff > 0 and out[k] < hi:
                    out[k] += 1
                    diff -= 1
                elif diff < 0 and out[k] > lo:
                    out[k] -= 1
                    diff += 1
        out["LUCK"] = int(attrs.get("LUCK") or 50)
        return out

    def _chargen_table_talk(self, briefing: str,
                            attrs_by_seat: dict[str, dict[str, int]],
                            method: str = "roll",
                            budget: dict[str, int] | None = None
                            ) -> tuple[list[str], dict[str, dict[str, Any]]]:
        """车卡桌上的多轮商量。

        真人在桌上不是一次性填完卡的：先说自己想玩什么，被同伴吐槽、
        被劝着补某个技能、或者干脆改主意，聊够了才动笔。
        这个过程本身就是跑团的乐趣，所以这里分几轮聊出来。
        """
        rounds = max(0, int(self.options.get("chargen_chat_rounds", 2)))
        self._e("system", "── 桌上开始聊车卡 ──")
        chatter: list[str] = []
        pitches: dict[str, str] = {}
        meta: dict[str, dict[str, Any]] = {}

        # 报意向：大家同时说，这一轮互相还看不到
        for pl in self.pls:
            res = pl.chargen_pitch(briefing, attrs_by_seat.get(pl.seat_id, {}),
                                   method, budget)
            if not res.get("ok"):
                self._e("system", f"{pl.display_name} 没接上话：{res.get('error','')}",
                        seat_id=pl.seat_id)
                continue
            meta[pl.seat_id] = {"age": res.get("age") or 0,
                                "attrs": res.get("attrs") or {}}
            if res.get("pitch"):
                pitches[pl.seat_id] = res["pitch"]
                self._e("pitch", res["pitch"], name=pl.display_name, seat_id=pl.seat_id)
            if res.get("ooc"):
                chatter.append(f"· {pl.display_name}（{self._player_of(pl)}）：{res['ooc']}")
                self._e("ooc", res["ooc"], name=self._player_of(pl),
                        seat_id=pl.seat_id, meta={"chargen_chat": True})

        # 商量轮：互相都看得见，KP 也在桌上
        for _ in range(rounds):
            for pl in self.pls:
                if self.stopped:
                    return chatter
                res = pl.chargen_chat(briefing, chatter, own=pitches.get(pl.seat_id, ""))
                if not res.get("ok"):
                    continue
                if res.get("pitch"):
                    pitches[pl.seat_id] = res["pitch"]
                    chatter.append(f"· {pl.display_name}（{self._player_of(pl)}）"
                                   f"改主意了：{res['pitch']}")
                    self._e("pitch", res["pitch"], name=pl.display_name,
                            seat_id=pl.seat_id, meta={"changed": True})
                if res.get("ooc"):
                    chatter.append(f"· {pl.display_name}（{self._player_of(pl)}）：{res['ooc']}")
                    self._e("ooc", res["ooc"], name=self._player_of(pl),
                            seat_id=pl.seat_id, meta={"chargen_chat": True})
            if self.kp:
                kres = self.kp.chargen_chat(briefing, chatter)
                if kres.get("ooc"):
                    kp_name = self._player_of(self.kp)
                    chatter.append(f"· 守秘人（{kp_name}）：{kres['ooc']}")
                    self._e("ooc", kres["ooc"], name=kp_name,
                            seat_id=self.kp.seat_id, meta={"chargen_chat": True})
        return chatter, meta

    def _party_sheets_text(self) -> str:
        lines: list[str] = []
        for pl in self.pls:
            c = pl.seat.get("character") or {}
            a = c.get("attributes") or {}
            sk = "、".join(f"{s.get('name')}{s.get('value')}"
                           for s in (c.get("skills") or [])[:8])
            inv = "、".join(str(x) for x in (c.get("inventory") or [])) or "（空）"
            lines.append(
                f"【{pl.seat_id}】{c.get('name', '')} · {c.get('occupation', '')} · "
                f"{c.get('age', '')}岁\n"
                f"  属性：STR{a.get('STR')} CON{a.get('CON')} DEX{a.get('DEX')} "
                f"APP{a.get('APP')} POW{a.get('POW')} SIZ{a.get('SIZ')} "
                f"INT{a.get('INT')} EDU{a.get('EDU')}\n"
                f"  技能：{sk}\n"
                f"  随身物品：{inv}\n"
                f"  背景：{(c.get('backstory') or '')[:180]}")
        return "\n".join(lines)

    def _emit_audit(self, rows: list[dict[str, str]], title: str,
                    final: bool = False) -> None:
        txt = "\n".join(
            f"{r['verdict']}　{r['seat']}　{r['item']}　{r['note']}" for r in rows)
        self._e("audit", txt, name="守秘人",
                meta={"rows": rows, "title": title, "final": final})

    def _chargen_audit(self) -> None:
        """审卡。

        跑团最好笑的部分之一：总有人想在车卡的时候夹带点不该有的东西，
        以为守秘人不会细看。被逮住之后那几句讨价还价，比正片还好看。
        所以这一步是**真的**：守秘人质疑、玩家申辩、守秘人裁决，
        裁决说「拿掉」引擎就真的从角色卡上把东西删掉。
        """
        if not self.kp or not self.pls:
            return
        setting = ""
        if self.module:
            setting = self.module.era or (self.module.summary or "")[:140]
        self._e("system", "── 守秘人审卡 ──")
        res = self.kp.audit_sheets(setting, self._party_sheets_text(),
                                   self.module.title if self.module else "")
        rows = res.get("rows") or []
        if not res.get("ok"):
            self._e("system", f"审卡没做成（{res.get('error')}），跳过。")
            return
        if not rows:
            self._e("system", "守秘人扫了一遍卡，什么都没说。")
            return
        self._emit_audit(rows, "审卡")
        if res.get("ooc"):
            self._e("ooc", res["ooc"], seat_id=self.kp.seat_id,
                    name=self._player_of(self.kp), meta={"audit": True})

        targets: list[tuple[PLAgent, list[dict[str, str]]]] = []
        for pl in self.pls:
            mine = [r for r in rows
                    if r["seat"] in (pl.seat_id, pl.display_name)
                    and r["verdict"] == "质疑"]
            if mine:
                targets.append((pl, mine))
        if not targets:
            self._e("system", "全部通过，没人被卡。")
            return

        talk: list[str] = []
        for pl, issues in targets:
            d = pl.chargen_defend(pl.seat.get("character") or {}, issues)
            if not d.get("ok"):
                continue
            if d.get("ooc"):
                talk.append(f"{self._player_of(pl)}（{pl.display_name}）：{d['ooc']}")
                self._e("ooc", d["ooc"], seat_id=pl.seat_id,
                        name=self._player_of(pl), meta={"audit": True})
            # 玩家自己划掉的东西
            delta = memory_mod.parse_mem_block(d.get("mem", ""))
            if delta:
                for ch in pl.memory.apply_delta(delta, turn=0):
                    self._e("system", f"〔{pl.display_name}〕{ch}", seat_id=pl.seat_id)
                pl.seat.setdefault("character", {})["inventory"] = \
                    list(pl.memory.situation.get("inventory") or [])
        if not talk:
            return

        vres = self.kp.rule_sheets(setting, talk)
        vrows = vres.get("rows") or []
        if vres.get("ooc"):
            self._e("ooc", vres["ooc"], seat_id=self.kp.seat_id,
                    name=self._player_of(self.kp), meta={"audit": True})
        if not vrows:
            return
        for r in vrows:
            if r["verdict"] != "拿掉":
                continue
            pl = self._find_pl(r["seat"])
            if not pl:
                continue
            inv = pl.memory.situation.setdefault("inventory", [])
            item = r["item"]
            hit = next((x for x in inv
                        if item and (item in str(x) or str(x) in item)), None)
            if hit:
                inv.remove(hit)
                pl.seat.setdefault("character", {})["inventory"] = list(inv)
                pl.memory.add("event", f"审卡时被拿掉了：{hit}", turn=0)
                self._e("system", f"〔角色卡变更〕{pl.display_name} 的「{hit}」被拿掉了。",
                        seat_id=pl.seat_id)
        self._emit_audit(vrows, "裁决", final=True)

    @staticmethod
    def _safe_name(text: str) -> str:
        return re.sub(r'[\\/:*?"<>|\s]+', "_", (text or "").strip())[:40] or "调查员"

    def _write_sheet(self, pl: PLAgent, char: dict[str, Any]) -> Any:
        """把这个角色的卡写进**他自己的文件夹**：

            data\\players\\<网名>\\sheets\\<角色名>.xlsx     ← COC7 空白卡填出来的
            data\\players\\<网名>\\sheets\\<角色名>.yaml     ← 同一张卡的结构化版本
        """
        from . import player_memory, sheet as sheet_mod
        folder = (player_memory.player_dir(pl.player_id) if pl.player_id
                  else cfgmod.memory_root() / "unassigned")
        outdir = folder / "sheets"
        base = self._safe_name(char.get("name", ""))
        player_name = pl.card.display_name or pl.display_name
        xlsx = sheet_mod.write_sheet(char, outdir / f"{base}.xlsx",
                                     player_name=player_name,
                                     residence=str(pl.memory.situation.get(
                                         "current_location") or ""))
        sheet_mod.dump_character_yaml(char, outdir / f"{base}.yaml", player_name)
        self._e("system", f"角色卡已写入 {xlsx}", seat_id=pl.seat_id)
        return xlsx

    def _roster_block(self) -> str:
        """给守秘人写彩蛋用的素材：这几个人是谁、他们的梗、上一局演的是谁。

        彩蛋之所以是彩蛋，就因为它只对**这几个人**有意义。
        所以这里把跨周目记忆摊给守秘人看——它才知道该埋什么。
        """
        lines: list[str] = []
        for pl in self.pls:
            card = pl.card
            bits = [f"· {card.display_name}"]
            if card.memes:
                bits.append("    常念叨：" + "；".join(f"「{m.text}」" for m in card.memes[:3]))
            if card.quirks:
                bits.append("    桌上的习惯：" + "；".join(card.quirks[:3]))
            if card.characters:
                last = card.characters[-1]
                bits.append(f"    上一个角色：{last.name}（{last.occupation}，"
                            f"{last.fate or '结局不明'}）")
            n = int(card.stats.get("sessions_played") or 0)
            if n:
                bits.append(f"    跟他一起跑过 {n} 局")
            lines.append("\n".join(bits))
        return "\n".join(lines) or "（这几个人是第一次一起跑，没有旧事可挖）"

    def _kp_study(self, force: bool = False) -> dict[str, Any]:
        """开局前的功课：读模组 → 写理解 / 大纲 / 扩展 / 彩蛋。

        已读过的模组**直接复用**——真实主持人读完一遍就记住了，
        下次带同一个本不用重读。想重读就 force=True。

        「大纲不能偏」是可校验的：它抄下来的场景 id 必须和模组实际的一字不差，
        对不上就打回重写；再对不上就强制以模组原文为准。
        """
        assert self.kp is not None
        mid = self.module.id if self.module else ""
        if mid and not force:
            saved = study_mod.load_study(mid)
            if saved:
                self.session.module_study = {
                    k: saved.get(k, "") for k in
                    ("spine", "spine_ids", "understanding", "expansion", "eggs")}
                self._e("system", f"── 这个模组之前已经研读过"
                                  f"（{saved.get('updated_at', '')}，"
                                  f"{saved.get('keeper', '')}），直接复用 ──")
                self._e("study", self.session.module_study.get("understanding", ""),
                        name=self.kp.display_name,
                        meta={"spine_ids": self.session.module_study.get("spine_ids", []),
                              "expansion": self.session.module_study.get("expansion", ""),
                              "eggs": self.session.module_study.get("eggs", ""),
                              "cached": True})
                return self.session.module_study

        title = self.module.title if self.module else ""
        ids = [s.id for s in self.module.scenes] if self.module else []
        brief = self._module_brief(include_party=False)
        roster = self._roster_block()

        self._e("system", "── 守秘人先读一遍模组，做开局前的功课 ──")
        res = self.kp.study_module(brief, roster, ids, title, len(self.pls))
        if not res.get("ok"):
            self._e("system", f"功课没做成（{res.get('error')}），这一局按模组原文跑。")
            return {}

        def mismatch(spine: list[str]) -> tuple[list[str], list[str]]:
            return ([x for x in ids if x not in spine],
                    [x for x in spine if x not in ids])

        if ids:
            miss, extra = mismatch(res.get("spine_ids") or [])
            if miss or extra:
                self._e("system",
                        f"大纲对不上（漏了 {'、'.join(miss) or '无'}；"
                        f"多了 {'、'.join(extra) or '无'}），让守秘人重写一次。")
                note = ("# 你上一版的大纲有问题，重写\n"
                        f"必须一字不差地包含这些 id：{'、'.join(ids)}\n"
                        f"不能出现这些不存在的 id：{'、'.join(extra) or '（无）'}")
                res2 = self.kp.study_module(brief, roster, ids, title,
                                            len(self.pls), note)
                m2, e2 = mismatch(res2.get("spine_ids") or []) if res2.get("ok") else ([1], [1])
                if res2.get("ok") and not m2 and not e2:
                    res = res2
                    self._e("system", "重写后大纲与模组一致。")
                else:
                    self._e("system", "重写后仍对不上，已强制以模组原文的场景为准。")
                    res["spine_ids"] = list(ids)
            else:
                self._e("system", "大纲核对通过，与模组一致。")

        self.session.module_study = {
            "spine": res.get("spine", ""),
            "spine_ids": res.get("spine_ids", []),
            "understanding": res.get("understanding", ""),
            "expansion": res.get("expansion", ""),
            "eggs": res.get("eggs", ""),
        }
        self._e("study", res.get("understanding", ""), name=self.kp.display_name,
                meta={"spine_ids": res.get("spine_ids", []),
                      "expansion": res.get("expansion", ""),
                      "eggs": res.get("eggs", "")})
        if res.get("eggs"):
            self._e("system", "〔彩蛋已埋下，跑完才揭晓〕")
        # 按模组存下来，下次开团直接复用
        if mid:
            try:
                study_mod.save_study(
                    mid, self.session.module_study,
                    keeper=self._player_of(self.kp),
                    model=self.kp.seat.get("model", ""),
                    title=self.module.title if self.module else mid)
                self._e("system", f"功课已存档，以后用这个模组开团会直接复用。")
            except Exception as e:  # noqa: BLE001
                self._e("system", f"功课存档失败（不影响这一局）：{e}")
        return self.session.module_study

    def _module_fulltext(self) -> str:
        """散场解禁时给玩家看的模组原文。"""
        m = self.module
        if not m:
            return "（这一局是自由跑团，没有现成模组）"
        parts = [f"《{m.title}》"]
        if m.summary:
            parts.append(f"[简介]\n{m.summary}")
        if m.premise:
            parts.append(f"[开场设定]\n{m.premise}")
        for s in m.scenes:
            parts.append(f"[{s.title}]\n{_clip(s.body, 1800)}")
        for h in m.handouts:
            parts.append(f"[文件《{h.title}》]\n{_clip(h.body, 800)}")
        if m.truth:
            parts.append(f"[幕后真相]\n{_clip(m.truth, 4000)}")
        return "\n\n".join(parts)

    def _reveal_and_teatime(self, rounds: int = 2) -> None:
        """结局之后：模组解禁，全桌坐下来聊这局。

        **解禁只发生在这里**。在此之前，模组的原文和幕后真相
        从来不会进入任何 PL 的上下文——不是靠提示词约束，是代码路径上就拿不到。
        """
        if not self.module:
            return
        self.session.module_revealed = True
        text = self._module_fulltext()
        self._e("system", "── 结局已过，模组对全桌解禁 ──")
        self._e("reveal", text, name="守秘人", meta={"chars": len(text)})

        digest = _session_digest(self.session, 1200)
        talk: list[str] = []
        for r in range(max(1, rounds)):
            for pl in self.pls:
                if self.stopped:
                    return
                res = pl.teatime(text, digest)
                if res.get("ooc"):
                    talk.append(f"{self._player_of(pl)}：{res['ooc']}")
                    self._e("ooc", res["ooc"], seat_id=pl.seat_id,
                            name=self._player_of(pl), meta={"teatime": True})
            if self.kp and talk:
                res = self.kp.teatime(talk, digest, self.session.module_study)
                if res.get("ooc"):
                    talk.append(f"{self._player_of(self.kp)}（守秘人）：{res['ooc']}")
                    self._e("ooc", res["ooc"], seat_id=self.kp.seat_id,
                            name=self._player_of(self.kp), meta={"teatime": True})
        self.session.save()

    def _setting(self) -> str:
        """模组的时代背景，用来给提示词定用词语域。

        没有模组、或者模组没写时代的时候返回空串，
        提示词那边会退成「你自己从正文里判断」。
        """
        if not self.module:
            return ""
        return str(getattr(self.module, "era", "") or "").strip()

    def _module_brief(self, include_party: bool = True) -> str:
        """守秘人的全部情报。放进 system 而不是消息历史——
        system 前缀恒定不变，能命中服务端的前缀缓存；消息历史每轮都变，命中不了。

        `include_party=False` 用于车卡之前：那时玩家还没有角色。
        """
        parts: list[str] = []
        if self.module:
            if self.module.summary:
                parts.append(f"[模组简介（这段是公开的，玩家也看得到）]\n{self.module.summary}")
            if self.module.truth:
                parts.append(f"[幕后真相 —— 只有你能看]\n{_clip(self.module.truth, 7000)}")
            if self.module.players:
                parts.append(f"[推荐人数] {self.module.players}")
            outline = "\n".join(f"  {s['order']}. {s['id']} —— {s['title']}"
                                for s in self.module.outline())
            if outline:
                parts.append("[场景目录（用 advance_scene 切换，只能用下面这些 id）]\n" + outline)
            if self.module.handouts:
                parts.append("[handout 文件目录（用 grant_handout 发放）]\n"
                             + "\n".join(f"  {h.id} —— {h.title}" for h in self.module.handouts))
        if self.session.premise:
            parts.append(f"[本局设定]\n{self.session.premise}")
        if self.session.chargen_briefing:
            parts.append("[你已经发给玩家的赛前简报（他们看过这个）]\n"
                         + self.session.chargen_briefing)
        st = self.session.module_study or {}
        if st:
            parts.append("[你开局前做的功课 —— 这是你自己的东西，照着它跑]\n"
                         f"大纲（不能偏离）：\n{st.get('spine', '')}\n"
                         f"你的理解：\n{st.get('understanding', '')}\n"
                         f"你要加的东西：\n{st.get('expansion', '')}\n"
                         f"你为他们埋的彩蛋：\n{st.get('eggs', '')}")
        if include_party and self.pls:
            party = "\n".join(
                f"  · {pl.display_name}（{pl.seat.get('character', {}).get('occupation', '?')}）"
                f"—— 玩家叫 {pl.seat.get('profile', {}).get('player_name', '?')}"
                for pl in self.pls)
            parts.append(f"[本桌的调查员]\n{party}")
            backs = []
            for pl in self.pls:
                bg = (pl.seat.get("character") or {}).get("backstory", "")
                if bg:
                    backs.append(f"  · {pl.display_name}：{_clip(bg, 260)}")
            if backs:
                parts.append("[给恐怖取材用的角色背景（让他们怕的东西具体化）]\n"
                             + "\n".join(backs))
        return "\n\n".join(parts)

    def _fallback_brief(self) -> str:
        """简报生成失败或疑似剧透时的兜底——宁可平淡，不能剧透。"""
        m = self.module
        if not m:
            return ("## 这是个什么故事\n这一局没有现成模组，世界由守秘人现场搭。\n\n"
                    "## 车卡建议\n车一个你想演的人就行。")
        parts = ["## 这是个什么故事", m.summary or m.premise or "（模组没有写简介）"]
        if m.premise and m.premise != m.summary:
            parts.append(m.premise)
        parts.append("\n## 车卡建议\n（守秘人的简报没能自动生成，这里只保留模组作者写的公开简介。"
                     "你可以看第一幕大致会遇到什么，自行判断要不要点某个技能。）")
        return "\n".join(parts)

    def _kp_briefing(self) -> str:
        """车卡之前，先让守秘人写一份不给玩家剧透的开场简报。

        两层防剧透：
          · 事前把「秘密词表」塞进提示词，明确告诉它这些词一个都不许出现
          · 事后用同一张表查它写出来的东西，命中就重写；再命中就退回模组自带的公开简介
        """
        assert self.kp is not None
        terms = spoiler.hidden_terms(self.module) if self.module else []
        title = self.module.title if self.module else ""
        brief_md = self._module_brief(include_party=False)

        self._e("system", "── 守秘人先看一遍模组，写一份给玩家的开场简报 ──")
        res = self.kp.chargen_briefing(brief_md, terms, title, len(self.pls))
        if not res.get("ok"):
            self._e("system", f"简报生成失败（{res.get('error')}），改用模组自带的公开简介。")
            brief = self._fallback_brief()
        else:
            brief = res["brief"]
            hits = spoiler.audit(brief, terms)
            if hits:
                self._e("system", f"简报里出现了疑似剧透词（{'、'.join(hits[:6])}），"
                                  f"让守秘人重写一次。")
                note = ("# 你上一版写到了这些不该出现的词，这一版一个都不许再出现\n"
                        + "、".join(hits))
                res2 = self.kp.chargen_briefing(brief_md, terms, title, len(self.pls), note)
                brief2 = res2.get("brief", "") if res2.get("ok") else ""
                if brief2 and not spoiler.audit(brief2, terms):
                    brief = brief2
                    self._e("system", "重写通过，这一次没有漏出秘密词。")
                else:
                    self._e("system", "重写后仍有疑似剧透，已退回模组自带的公开简介"
                                      "（宁可平淡也不剧透）。")
                    brief = self._fallback_brief()

        self.session.chargen_briefing = brief
        self._e("brief", brief, name=self.kp.display_name,
                meta={"secret_terms": len(terms), "terms": terms[:12]})
        return brief

    # ══════════════════════════════════════════════ 准备阶段：掷属性 + 车卡

    def prepare(self) -> bool:
        """车卡阶段：守秘人先出简报，然后每个 PL 按规则书自己车一张卡。

        属性由引擎掷，技能点预算由引擎校验。PL 可以看到守秘人的简报，
        但**完全可以不理**——大家各车各的本来也是跑团的乐趣之一。
        """
        self._ensure_pls()
        self._ensure_kp()
        self.session.phase = "chargen"
        self._e("system", f"── 车卡阶段开始（模组：{self.module.title if self.module else '自由跑团'}）──")

        # ── 第零步：守秘人先做功课（读模组 → 理解 / 大纲 / 扩展 / 彩蛋）──
        self._kp_study()
        # 功课要进 system 提示，所以重建一次
        self.kp.update_brief(self._module_brief(include_party=False))

        briefing = self._kp_briefing()

        # ── 第一步：属性（掷骰 or 购点预算）──
        method = str(self.options.get("chargen_method") or "roll").lower()
        budget = {"total": int(self.options.get("pointbuy_total", 480)),
                  "min": int(self.options.get("pointbuy_min", 40)),
                  "max": int(self.options.get("pointbuy_max", 90))}
        attrs_by_seat: dict[str, dict[str, int]] = {}
        for pl in self.pls:
            st = self.session.seat_state(pl.seat_id)
            attrs = st.get("attrs") or chargen.roll_attributes(self.kernel.rng)
            st["attrs"] = attrs
            attrs_by_seat[pl.seat_id] = attrs
            if method == "pointbuy":
                self._e("system",
                        f"{pl.display_name}（玩家 {self._player_of(pl)}）：购点车卡，"
                        f"八项属性共 {budget['total']} 点，每项 {budget['min']}–{budget['max']}，"
                        f"幸运另掷 = {attrs.get('LUCK')}",
                        seat_id=pl.seat_id)
            else:
                self._e("system",
                        f"{pl.display_name}（玩家 {self._player_of(pl)}）的属性已由守秘人掷出："
                        + "  ".join(f"{k}{v}" for k, v in attrs.items()),
                        seat_id=pl.seat_id)

        # ── 第二步：先在桌上聊几轮，别一上来就吐成品卡 ──
        chatter, pitch_meta = self._chargen_table_talk(briefing, attrs_by_seat, method, budget)

        # ── 第三步：结算属性（购点校验 + 年龄补正）──
        final_attrs: dict[str, dict[str, int]] = {}
        for pl in self.pls:
            base = dict(attrs_by_seat.get(pl.seat_id) or {})
            meta = pitch_meta.get(pl.seat_id) or {}
            a = base
            if method == "pointbuy" and meta.get("attrs"):
                proposed = dict(base)
                proposed.update({k: v for k, v in (meta["attrs"] or {}).items()
                                 if k in chargen.ATTR_ORDER})
                warns = chargen.pointbuy_warnings(proposed, **budget)
                if warns:
                    self._e("system",
                            f"{pl.display_name} 的购点方案有问题（{'；'.join(warns)}），"
                            f"已按预算自动修正。", seat_id=pl.seat_id)
                    proposed = self._fix_pointbuy(proposed, **budget)
                proposed["LUCK"] = int(base.get("LUCK") or 50)
                a = proposed
            age = int(meta.get("age") or 0)
            if age and self.options.get("age_adjust", True):
                adjusted, notes = chargen.age_adjust(a, age, self.kernel.rng)
                if notes:
                    self._e("system", f"{pl.display_name} 年龄 {age}：{'；'.join(notes)}",
                            seat_id=pl.seat_id)
                a = adjusted
            final_attrs[pl.seat_id] = a
            if method == "pointbuy" or age:
                self._e("system", "最终属性：" + "  ".join(
                    f"{k}{v}" for k, v in a.items()), seat_id=pl.seat_id)

        # ── 第四步：定妆，各自把卡写出来 ──
        self._e("system", "── 聊到这儿，各自定妆 ──")
        ok_count = 0
        for pl in self.pls:
            st = self.session.seat_state(pl.seat_id)
            st["final_attrs"] = final_attrs.get(pl.seat_id) or {}
            res = pl.run_chargen(final_attrs.get(pl.seat_id, {}), briefing, chatter)
            if not res.get("ok"):
                self._e("system", f"车卡失败：{res.get('error')}", seat_id=pl.seat_id)
                continue

            char = res["character"]
            violations = res.get("violations") or []
            repairs = res.get("repairs") or []
            st.update({
                "character": char, "chargen_warnings": res.get("warnings", []),
                "chargen_violations": violations, "chargen_repairs": repairs,
                "chargen_spent": res.get("spent"), "chargen_budget": res.get("budget"),
                "chargen_raw": res.get("raw", ""), "chargen_ooc": res.get("ooc", ""),
                "ready": True,
            })
            # 角色先落地，后面所有事件才叫得出**角色名**（车卡阶段叫的是网名，
            # 因为那会儿坐在这儿的确实是玩家本人——见 config.make_pl_seat）
            pl.adopt_character(char)
            # 违规与修正都摆到台面上——车卡规范是硬的，但过程要看得见
            for v in violations:
                self._e("system", f"〔车卡违规〕{pl.display_name}：{v['detail']}",
                        seat_id=pl.seat_id)
            for note in repairs:
                self._e("system", f"〔车卡修正〕{pl.display_name}：{note}",
                        seat_id=pl.seat_id)
            b = res.get("budget") or {}
            if b:
                self._e("system",
                        f"〔车卡结算〕{pl.display_name}：技能点 {res.get('spent')}"
                        f" / {b.get('total')}（职业 {b.get('occ')} + 兴趣 {b.get('interest')}，"
                        f"公式 {b.get('formula')}）" + ("，已裁到合规" if repairs else "，合规"),
                        seat_id=pl.seat_id)
            try:
                pl.save_memory()
                pl.card.save()       # 让这个人的文件夹在车卡阶段就建起来
            except Exception:
                pass
            # ★ 落一张真的 COC7 Excel 角色卡到这个人的文件夹里
            try:
                st["sheet_path"] = str(self._write_sheet(pl, char))
            except Exception as e:  # noqa: BLE001
                self._e("system", f"Excel 角色卡没写出来：{type(e).__name__}: {e}",
                        seat_id=pl.seat_id)
            ok_count += 1

            sheet_yaml = yaml.safe_dump(
                {k: char.get(k) for k in ("name", "occupation", "age", "gender")}
                | {"attributes": char.get("attributes")} | {"skills": char.get("skills")},
                allow_unicode=True, sort_keys=False, width=100, default_flow_style=False)
            self._e("chargen", sheet_yaml, name=char.get("name", ""), seat_id=pl.seat_id,
                    meta={"warnings": res.get("warnings", []), "character": char})
            if res.get("ooc"):
                self._e("ooc", res["ooc"], name=pl.seat.get("profile", {}).get("player_name", ""),
                        seat_id=pl.seat_id)
            if res.get("warnings"):
                self._e("system", "规则校验：" + "；".join(res["warnings"]),
                        seat_id=pl.seat_id)

        self.session.save()
        if ok_count == 0:
            self._e("system", "没有任何一名 PL 车卡成功，无法开局。")
            return False
        # ☆ 审卡：总有人想夹带点不该有的东西
        if self.options.get("chargen_audit", True):
            try:
                self._chargen_audit()
            except Exception as e:  # noqa: BLE001
                self._e("system", f"审卡出错（已跳过）：{type(e).__name__}: {e}")
        self._e("system", f"── 车卡完成，{ok_count}/{len(self.pls)} 名调查员就绪 ──")
        self.session.save()
        return True

    # ══════════════════════════════════════════════ 开局：KP 开场 + 入戏锚定

    def begin(self) -> bool:
        self._ensure_pls()
        self._ensure_kp()
        assert self.kp is not None

        # 车卡已经结束，把「本桌有哪些调查员、他们的背景」补进守秘人的 system
        self.kp.update_brief(self._module_brief(include_party=True))

        scene = None
        if self.module:
            sid = self.session.scene_id or self.module.first_scene_id()
            scene = self.module.scene(sid)
            if scene:
                self.session.scene_id = scene.id
                self._last_scene_id = scene.id
                self.session.scene_log.append(scene.id)

        handouts = [h.id for h in (self.module.handouts if self.module else [])]

        try:
            out = self.kp.opening(
                scene_text=scene.body if scene else "",
                scene_title=scene.title if scene else "",
                handouts=handouts,
                time_block=self.session.clock.render() if self.session.clock else "",
            )
        except LLMError as e:
            self._e("system", f"守秘人开场失败：{e.message}")
            return False

        if scene:
            self._e("scene", f"{scene.title}", meta={"scene_id": scene.id})
        self.last_narr = out.narr or "（守秘人没有写出叙述）"
        self._emit_kp(out)

        # 入戏锚定：每个 PL 先用自己的正式通道写一小段，作为它的第一条 assistant 历史
        time_block = self.session.clock.render() if self.session.clock else ""
        for pl in self.pls:
            anchor = pl.anchor(self.last_narr, time_block)
            if anchor and (anchor.act or anchor.think):
                self._e("act", anchor.act or "（入戏）", name=pl.display_name,
                        seat_id=pl.seat_id, meta={"anchor": True})
                if anchor.ooc:
                    self._e("ooc", anchor.ooc, seat_id=pl.seat_id,
                            name=pl.seat.get("profile", {}).get("player_name", ""),
                            meta={"anchor": True})

        self.session.phase = "running"
        # 跨周目：本局计入玩家的生涯，之后所有注入都能说"你已经跑过 N 局了"
        # 站位会轮换，所以按各自的角色分别记录
        for pl in self.pls:
            pl.card.begin_session(self.session.session_id, role="PL")
            pl.card.save()
        if self.kp:
            self.kp.card.begin_session(self.session.session_id, role="KP")
            self.kp.card.save()
        self.session.save()
        return True

    # ══════════════════════════════════════════════ 单轮

    def step(self) -> bool:
        """推进一轮。返回 False 表示该结束了。"""
        with self._lock:
            if self.kp is None:
                self._ensure_pls()
                self._ensure_kp()
            assert self.kp is not None
            if self.stopped:
                return False

            self.session.round += 1
            self.kernel.set_turn(self.session.round)
            max_rounds = int(self.options.get("max_rounds", 40))
            if self.session.round > max_rounds:
                self.session.ended_reason = f"达到回合上限 {max_rounds}"
                self._e("system", f"── 已达回合上限（{max_rounds}），自动停止 ──")
                return False

            self._e("system", f"── 第 {self.session.round} 轮 ──")

            # 拨钟：这一轮从什么时候开始。两个阶段（PL / KP）共用同一份，
            # 免得同一轮里两边看到的时间不一样。
            time_block = self._tick_clock()
            if self.session.clock is not None:
                self._e("system", f"〔时间〕{self.session.clock.short()}",
                        meta={"clock": self.session.clock.to_dict()})

            # ---- PL 阶段 ----
            results = self._pl_phase(time_block)
            if results is None:
                return False

            outputs, dice_lines = results
            self._round_publics = [
                {"seat_id": pl.seat_id,
                 "display_name": pl.display_name,
                 "player_name": pl.seat.get("profile", {}).get("player_name", ""),
                 "act": outputs[pl.seat_id].act,
                 "ooc": outputs[pl.seat_id].ooc,
                 "tt": False}
                for pl in self.pls if pl.seat_id in outputs
            ]

            # ---- 桌边插话轮 ----
            # 主回合里所有 PL 是同时行动的，彼此看不到当轮对方的话。
            # 真人桌上不是这样：A 一句「你赶紧过来啊」，B 当场就回
            # 「我又不知道你们去了，我角色不知道啊」。这一轮专门补上这个来回。
            if self.options.get("table_talk", True) and len(self.pls) > 1:
                try:
                    self._table_talk_round(int(self.options.get("table_talk_exchanges", 2)))
                except Exception as e:  # noqa: BLE001
                    self._e("system", f"桌边插话轮出错（已跳过）：{type(e).__name__}: {e}")

            if self.stopped:
                return False

            # ---- KP 阶段 ----
            scene_text = ""
            if self.module and self.session.scene_id != self._last_scene_id:
                sc = self.module.scene(self.session.scene_id)
                if sc:
                    scene_text = sc.body
                    self._last_scene_id = self.session.scene_id

            table_talk = [f"{o['player_name']}（{o['display_name']}）：{o['ooc']}"
                          for o in self._round_publics if o.get("ooc")]

            notes = list(self.session.director_notes[-3:])
            # 守秘人上一轮自己申请的掷骰结果，这一轮开头还给它
            dice_lines = list(getattr(self, "_kp_pending_dice", [])) + dice_lines
            self._kp_pending_dice = []
            try:
                kp_out = self.kp.respond(
                    actions=self._round_publics, dice_lines=dice_lines,
                    scene_text=scene_text, engine_notes=notes, table_talk=table_talk,
                    time_block=time_block,
                )
            except LLMError as e:
                self._e("system", f"守秘人回合失败：{e.message}")
                self.session.phase = "paused"
                self.session.save()
                return False

            self._emit_kp(kp_out)
            self.last_narr = kp_out.narr or self.last_narr

            # 守秘人也能用 <mem> 维护自己的剧情树（它记的是"世界现在什么状态"）
            if kp_out.mem and self.kp:
                delta = memory_mod.parse_mem_block(kp_out.mem)
                for ch in self.kp.memory.apply_delta(delta, turn=self.session.round,
                                                     scene=self.session.scene_id):
                    self._e("system", f"〔守秘人备忘〕{ch}")

            # 守秘人掷的骰子也在工具轮里掷好了——它**同一轮**就拿到了点数。
            #   <roll>          → 暗骰：只有 KP 和"你"看得到，玩家上下文里不会出现
            #   <state> openroll → 明骰：全桌都看得见，下一轮发给大家
            for info in kp_out.roll_results:
                self._e("secret_dice", info["summary"], name="守秘人",
                        seat_id=self.kp.seat_id if self.kp else "", meta=info)
            for info in kp_out.open_rolls:
                self._e("dice", info["summary"], name="守秘人",
                        seat_id=self.kp.seat_id if self.kp else "", meta=info)
                self._public_dice.append(f"守秘人（公开掷骰）：{info['summary']}")

            engine_notes = self._execute_directives(kp_out, dice_lines)

            self.session.phase = "running"
            self._persist_round()
            delay = int(self.options.get("turn_delay_ms", 400))
            if delay > 0 and not self.stopped:
                time.sleep(delay / 1000.0)
            return True

    def _persist_round(self) -> None:
        """每轮结束都落盘一次。

        事件流水本来就逐条 append 到 transcript.jsonl，但记忆卡（HP、线索、物品）
        原先只在车卡后和散场时写——跑到一半被关掉就丢了。
        每轮写一次，代价是几个小文件，换来的是"随时关掉都不丢"。
        """
        for pl in self.pls:
            try:
                pl.save_memory()
            except Exception:
                pass
        if self.kp:
            try:
                self.kp.save_memory()
            except Exception:
                pass
        self.session.save()

    # ══════════════════════════════════════════════ PL 阶段

    def _pl_phase(self, time_block: str = "") -> tuple[dict[str, ChannelOutput], list[str]] | None:
        """全体 PL 行动。探索轮互不可见当轮他人输出。"""
        narr = self.last_narr
        scene_id = self.session.scene_id
        # 上一轮的**明骰**：全桌都看见了，这一轮发给每个人（包括掷的人自己）
        public_prev = list(self._public_dice)
        self._public_dice = []

        def run_one(pl: PLAgent) -> tuple[str, ChannelOutput | None, str]:
            private = self.session.pending_private.pop(pl.seat_id, [])
            seat_dice = self.session.pending_dice.pop(pl.seat_id, []) + public_prev
            extra = ""
            if private:
                extra = "[只有你知道的事]\n" + "\n".join(f"- {p}" for p in private)
            try:
                out = pl.act(
                    narr=narr, scene_id=scene_id,
                    others=self._round_publics if self.session.round > 1 else [],
                    dice_lines=seat_dice or None,
                    extra=extra,
                    unknown=self._unknown_for(pl),
                    time_block=time_block,
                )
                return pl.seat_id, out, ""
            except LLMError as e:
                return pl.seat_id, None, e.message

        outputs: dict[str, ChannelOutput] = {}
        errors: dict[str, str] = {}
        if self.options.get("parallel_pl", True) and len(self.pls) > 1:
            with ThreadPoolExecutor(max_workers=min(6, len(self.pls))) as pool:
                for sid, out, err in pool.map(run_one, self.pls):
                    if out is not None:
                        outputs[sid] = out
                    else:
                        errors[sid] = err
        else:
            for pl in self.pls:
                sid, out, err = run_one(pl)
                if out is not None:
                    outputs[sid] = out
                else:
                    errors[sid] = err

        # 按座位顺序发事件，避免并行导致界面乱序
        for pl in self.pls:
            if pl.seat_id in errors:
                pl.stats.errors += 1
                pl.stats.last_error = errors[pl.seat_id]
                self._e("system", f"{pl.display_name} 这一轮没有出声：{errors[pl.seat_id]}",
                        seat_id=pl.seat_id)
                continue
            out = outputs[pl.seat_id]
            player = pl.seat.get("profile", {}).get("player_name", "")
            if out.think:
                self._e("think", out.think, name=pl.display_name, seat_id=pl.seat_id)
            if out.act:
                self._e("act", out.act, name=pl.display_name, seat_id=pl.seat_id)
            if out.ooc:
                self._e("ooc", out.ooc, name=player, seat_id=pl.seat_id)
            if out.recall_results:
                hits = [d["term"] for d in out.recall_results if d.get("ok")]
                miss = [d["term"] for d in out.recall_results if not d.get("ok")]
                msg = ("想起来：" + "、".join(hits)) if hits else "使劲想了想，什么也没想起来"
                if miss:
                    msg += "；想不起：" + "、".join(miss)
                self._e("recall", msg, name=pl.display_name, seat_id=pl.seat_id,
                        meta={"results": out.recall_results})
            for n in out.notes:
                self._e("system", f"〔{pl.display_name}〕{n}", seat_id=pl.seat_id)

        if not outputs:
            self._e("system", "所有玩家这一轮都没有成功行动，停止推进。")
            return None

        # ---- 解析检定申请并掷骰（骰子在引擎手里）----
        dice_lines: list[str] = []
        for pl in self.pls:
            out = outputs.get(pl.seat_id)
            if not out:
                continue
            for cr in out.checks:
                char = pl.seat.get("character") or {}
                value = chargen.skill_value(char, cr.skill)
                res = self.kernel.do_check(
                    pl.display_name, cr.skill, value,
                    difficulty=cr.difficulty, bonus=cr.bonus, penalty=cr.penalty)
                line = res.text()
                dice_lines.append(line)
                self._e("dice", line, name=pl.display_name, seat_id=pl.seat_id,
                        meta={**res.to_dict(), "visibility": "public"})
                # 玩家在桌上掷骰是**公开**的：别人也看得见，下一轮大家都会知道
                self._public_dice.append(f"{pl.display_name}：{line}")

            # ---- AI 自己掷的骰子：在工具轮里就掷好了，它本人在同一轮已经看到点数 ----
            for info in out.roll_results:
                dice_lines.append(info["summary"])
                self._e("dice", info["summary"], name=pl.display_name,
                        seat_id=pl.seat_id, meta=info)
                # 玩家在桌上掷骰是公开的：别人下一轮也看得见
                self._public_dice.append(f"{pl.display_name}：{info['summary']}")

        return outputs, dice_lines

    # ══════════════════════════════════════════════ 知识边界

    def _unknown_for(self, pl: PLAgent) -> list[str]:
        """「桌上你知道、但你的角色并不知道的事」。

        这一栏是防超游的地基。不显式列出来，AI 就会让角色凭空知道同伴
        在哪儿、拿到了什么——那样就永远不会有「我角色不知道啊」这种真实拉扯。
        """
        out: list[str] = []
        for sid, labels in (self.session.seat_private_log or {}).items():
            if sid == pl.seat_id or not labels:
                continue
            other = self._find_pl(sid)
            if not other:
                continue
            for label in labels[-2:]:
                out.append(f"{other.display_name} 私下收到了「{label}」——"
                           f"你只看见守秘人把东西递了过去，没看到内容。")

        mine = (pl.memory.situation.get("current_location") or "").strip()
        for other in self.pls:
            if other is pl:
                continue
            loc = (other.memory.situation.get("current_location") or "").strip()
            if loc and loc != mine:
                out.append(f"{other.display_name} 现在在{loc}，不在你视线里——"
                           f"除非他喊你、或者你自己过去，否则你不知道他看见了什么。")
        return out[:5]

    def _pc_state_brief(self, pl: PLAgent) -> str:
        sit = pl.memory.situation
        bits: list[str] = []
        if sit.get("current_location"):
            bits.append("你在" + str(sit["current_location"]))
        if sit.get("current_objective"):
            bits.append("你正打算" + str(sit["current_objective"]))
        inv = sit.get("inventory") or []
        if inv:
            bits.append("身上带着" + "、".join(str(x) for x in inv[:6]))
        unknown = self._unknown_for(pl)
        if unknown:
            bits.append("你角色不知道：" + "；".join(unknown[:3]))
        return "。".join(bits) + "。"

    # ══════════════════════════════════════════════ 桌边插话轮

    def _table_talk_round(self, max_exchanges: int = 2) -> None:
        """点名—回话—再回话。真人桌上最鲜活的那部分就是在这儿冒出来的。"""
        for exchange in range(max(0, max_exchanges)):
            by_seat = {o["seat_id"]: o for o in self._round_publics}
            # 第一轮看所有人说的话；之后只看新冒出来的桌边回话，避免复读
            pool = self._round_publics if exchange == 0 else \
                [o for o in self._round_publics if o.get("tt")]

            targets: list[tuple[PLAgent, str]] = []
            named: set[str] = set()
            for pl in self.pls:
                if pl.seat_id not in by_seat:
                    continue
                names = {pl.display_name,
                         (pl.seat.get("profile") or {}).get("player_name", "")}
                names.discard("")
                for other in pool:
                    if other["seat_id"] == pl.seat_id:
                        continue
                    ooc = other.get("ooc") or ""
                    if any(n and n in ooc for n in names):
                        targets.append((pl, other["player_name"] or other["display_name"]))
                        named.add(pl.seat_id)
                        break

            # 没人被点名也不该就这么冷场。
            # 真桌上，A 说一句「这门是从外面锁的」，B 就算没被叫到也会接一句。
            # 以前这里只在"被点名"时才说话，于是大家各说各的、一轮下来零来回——
            # 那正是"桌边感"最要紧的那部分。现在按顺序轮着补一两个人上来接话。
            if not targets:
                quiet = [pl for pl in self.pls
                         if pl.seat_id in by_seat and pl.seat_id not in named]
                if not quiet:
                    return
                want = 2 if exchange == 0 else 1
                want = max(1, min(int(self.options.get("table_talk_fallback", want)), want,
                                  len(quiet)))
                start = self._tt_cursor % len(quiet)
                for k in range(want):
                    targets.append((quiet[(start + k) % len(quiet)], ""))
                self._tt_cursor = start + want

            if not targets:
                return

            talk_lines = [f"{o['player_name']}（{o['display_name']}）：{o['ooc']}"
                          for o in self._round_publics if (o.get("ooc") or "").strip()]
            replies: list[tuple[PLAgent, str]] = []
            for pl, who in targets:
                if self.stopped:
                    return
                reply = pl.table_talk(talk_lines, self._pc_state_brief(pl), who)
                if reply:
                    replies.append((pl, reply))
            if not replies:
                return

            for pl, reply in replies:
                self._e("ooc", reply, seat_id=pl.seat_id,
                        name=(pl.seat.get("profile") or {}).get("player_name", ""),
                        meta={"table_talk": True})
                pl.append_table_talk(reply)
                entry = by_seat.get(pl.seat_id)
                if entry is not None:
                    entry["ooc"] = (entry.get("ooc", "") + "\n" + reply).strip()
                    entry["tt"] = True

    # ══════════════════════════════════════════════ KP 指令执行

    def _emit_kp(self, out: ChannelOutput) -> None:
        if out.secret:
            self._e("secret", out.secret, name="守秘人")
        if out.narr:
            self._e("narr", out.narr, name="守秘人")
        if out.ooc:
            self._e("ooc", out.ooc, name="守秘人")

    def _find_pl(self, token: str) -> PLAgent | None:
        token = (token or "").strip()
        if not token:
            return None
        for pl in self.pls:
            if token == pl.seat_id or token == pl.display_name:
                return pl
            if token == (pl.seat.get("profile") or {}).get("player_name"):
                return pl
            if token in pl.display_name or pl.display_name in token:
                return pl
        # 允许 pl_1 / 1 / PL1 之类的写法
        m = re.search(r"(\d+)", token)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(self.pls):
                return self.pls[idx]
        return None

    def _execute_directives(self, out: ChannelOutput,
                            prior_dice: list[str]) -> list[str]:
        notes: list[str] = list(prior_dice)
        for d in out.directives:
            try:
                notes.extend(self._one_directive(d))
            except Exception as e:  # noqa: BLE001
                self._e("system", f"指令 {d.kind} 执行失败：{type(e).__name__}: {e}")
        self.session.save()
        return notes

    def _one_directive(self, d: Directive) -> list[str]:
        kind = d.kind
        target = d.arg(1)
        payload = d.arg(2)

        if kind == "note":
            self._e("system", f"守秘人备注：{d.arg(1)}")
            return []

        # ---- 拨钟：故事里时间往前走（等了两小时、开车过去、一直熬到天亮）----
        action, arg = clock_mod.parse_clock_directive(kind, target, payload)
        if action and self.session.clock is not None:
            c = self.session.clock
            self._clock_dirty = True          # 这轮就别再叠默认推进了
            before = c.short()
            if action == "advance":
                minutes = clock_mod.parse_duration(arg)
                if minutes <= 0:
                    return [f"你想拨钟但引擎没看懂「{arg}」。写法示例：advance 2h / advance 30m / advance 1d"]
                if minutes > 60 * 24 * 365 * 5:
                    minutes = 60 * 24 * 365 * 5
                c.advance(minutes)
            else:
                abs_dt = clock_mod.parse_absolute(arg, c.dt)
                if abs_dt is None:
                    return [f"你想把时间设成「{arg}」但引擎没看懂。写法示例：time 10月5日 08:00"]
                c.set_dt(abs_dt)
            self._e("system", f"〔时间〕{before} → {c.short()}", meta={"clock": c.to_dict()})
            if self.kp:
                self.kp.note_engine_event(f"时间到了 {c.short()}", kind="event")
            return [f"时间已经拨到 {c.date_cn()} {c.clock_cn()}。"
                    f"（距离场过了 {c.elapsed_text()}）"]

        if kind == "advance_scene":
            sid = target.strip()
            if self.module and self.module.scene(sid):
                sc = self.module.scene(sid)
                self.session.scene_id = sc.id
                self.session.scene_log.append(sc.id)
                self._e("scene", f"{sc.title}", meta={"scene_id": sc.id})
                if self.kp:
                    self.kp.note_engine_event(f"场景推进到「{sc.title}」", kind="event")
                return [f"场景已切换到「{sc.title}」，其原文已在下一条消息里给你。"]
            self._e("system", f"守秘人要求切换到未知场景「{sid}」，已忽略（没有这个场景）。")
            return [f"你要求切换到场景 {sid}，但模组里没有这个场景，请用场景目录里已有的 id。"]

        pl = self._find_pl(target)
        if pl is None:
            self._e("system", f"指令 {kind} 找不到目标「{target}」，已忽略。")
            return [f"指令 {kind} 的目标「{target}」不存在，可用的是："
                    + "、".join(f"{p.seat_id}({p.display_name})" for p in self.pls)]

        if kind == "whisper":
            text = payload.strip()
            if not text:
                return []
            pl.note_engine_event(f"（守秘人私下告诉你）{text}", kind="clue",
                                 scene=self.session.scene_id)
            _push_pending(self.session, pl.seat_id, f"（守秘人私下告诉你）{text}")
            _log_private(self.session, pl.seat_id, "守秘人的私聊")
            self._e("system", f"〔私发〕{pl.display_name} 收到一条只有他看得见的信息。",
                    seat_id=pl.seat_id)
            return [f"你已把一段私密信息单独交给了 {pl.display_name}。"]

        if kind == "grant_handout":
            h = self.module.handout(payload) if self.module else None
            if not h:
                self._e("system", f"守秘人想发放 handout「{payload}」，但模组里没有这个文件。")
                return [f"没有名为「{payload}」的 handout。可用的是："
                        + ("、".join(x.id for x in self.module.handouts)
                           if self.module and self.module.handouts else "（无）")]
            body = _clip(h.body, 1200)
            pl.note_engine_event(f"（你拿到了文件《{h.title}》）\n{body}",
                                 kind="handout", scene=self.session.scene_id)
            _push_pending(self.session, pl.seat_id,
                          f"你拿到了文件《{h.title}》，内容如下：\n{body}")
            _log_private(self.session, pl.seat_id, f"《{h.title}》")
            self._e("system", f"〔发放〕{pl.display_name} 获得 handout《{h.title}》。",
                    seat_id=pl.seat_id)
            return [f"你已把《{h.title}》交给 {pl.display_name}。"]

        if kind == "check":
            skill = payload.split()[0] if payload.split() else ""
            diff = "regular"
            parts = payload.split()
            if len(parts) > 1:
                diff = {"常规": "regular", "困难": "hard", "极难": "extreme"}.get(
                    parts[1], parts[1] if parts[1] in ("regular", "hard", "extreme") else "regular")
            char = pl.seat.get("character") or {}
            value = chargen.skill_value(char, skill)
            res = self.kernel.do_check(pl.display_name, skill, value, difficulty=diff)
            self._e("dice", res.text(), name=pl.display_name, seat_id=pl.seat_id,
                    meta=res.to_dict())
            _push_pending(self.session, pl.seat_id, f"守秘人要你掷的检定结果：{res.text()}")
            return [res.text()]

        if kind == "damage":
            char = pl.seat.get("character") or {}
            db = (char.get("attributes") or {}).get("DB", "0")
            info = self.kernel.do_damage("守秘人", pl.display_name, payload or "1d3", db=str(db))
            pl.memory.apply_engine_stat("HP", -info["total"])
            hp = (pl.memory.character_sheet.get("attributes") or {}).get("HP", 0)
            pl.note_engine_event(f"你受到了 {info['total']} 点伤害，剩余生命 {hp}。",
                                 kind="injury", scene=self.session.scene_id)
            self._e("dice", info["summary"] + f"（剩余 HP {hp}）",
                    seat_id=pl.seat_id, meta=info)
            _push_pending(self.session, pl.seat_id,
                          f"你受了 {info['total']} 点伤害，现在生命 {hp}。")
            return [f"{pl.display_name} 受到 {info['total']} 点伤害，剩余 HP {hp}。"]

        if kind == "san":
            char = pl.seat.get("character") or {}
            san_now = int((char.get("attributes") or {}).get("SAN", 50))
            res = self.kernel.do_check(pl.display_name, "理智", san_now)
            loss = self.kernel.do_san(pl.display_name, payload or "0/1d4")
            amount = loss["success_loss"] if res.succeeded else loss["fail_loss"]
            pl.memory.apply_engine_stat("SAN", -amount)
            san_after = (pl.memory.character_sheet.get("attributes") or {}).get("SAN", 0)
            pl.note_engine_event(
                f"理智检定{res.level_cn}，损失 {amount} 点理智，当前 {san_after}。",
                kind="event", scene=self.session.scene_id)
            self._e("dice", f"{res.text()}；理智损失 {amount} 点（剩余 SAN {san_after}）",
                    seat_id=pl.seat_id, meta={"check": res.to_dict(), "loss": loss})
            _push_pending(self.session, pl.seat_id,
                          f"你的理智检定{res.level_cn}，损失 {amount} 点理智，现在 SAN {san_after}。")
            return [f"{pl.display_name} 的理智检定{res.level_cn}，损失 {amount} 点，剩余 SAN {san_after}。"]

        if kind == "heal":
            n = _int(payload, 1)
            pl.memory.apply_engine_stat("HP", n)
            hp = (pl.memory.character_sheet.get("attributes") or {}).get("HP", 0)
            pl.note_engine_event(f"你恢复了 {n} 点生命，现在 {hp}。", kind="event")
            self._e("dice", f"{pl.display_name} 恢复 {n} 点生命（现在 HP {hp}）",
                    seat_id=pl.seat_id)
            return [f"{pl.display_name} 恢复 {n} 点 HP，现在 {hp}。"]

        if kind == "luck":
            n = _int(payload, 0)
            pl.memory.apply_engine_stat("LUCK", -abs(n))
            luck = (pl.memory.character_sheet.get("attributes") or {}).get("LUCK", 0)
            self._e("dice", f"{pl.display_name} 消耗 {abs(n)} 点幸运（剩余 {luck}）",
                    seat_id=pl.seat_id)
            return [f"{pl.display_name} 消耗幸运，剩余 {luck}。"]

        self._e("system", f"未知指令「{kind}」，已忽略。")
        return []

    # ══════════════════════════════════════════════ 收尾

    def finish(self, reason: str = "") -> None:
        self.session.phase = "ended"
        if reason:
            self.session.ended_reason = reason

        # ★ 跨周目复盘：这是玩家层记忆唯一的生长点。
        #   不做这一步，每个 AI 下局都会重新变成陌生人。
        if self.options.get("retrospective", True) and self.session.round > 0 and not self.stopped:
            self._run_retrospectives()

        # ★ 结局之后才解禁模组，然后开一场散场茶话会
        if (self.options.get("reveal_after_end", True)
                and self.session.round > 0 and not self.stopped):
            try:
                self._reveal_and_teatime(int(self.options.get("teatime_rounds", 2)))
            except Exception as e:  # noqa: BLE001
                self._e("system", f"散场茶话会出错（已跳过）：{type(e).__name__}: {e}")

        for pl in self.pls:
            try:
                pl.save_memory()
            except Exception:
                pass
        if self.kp:
            try:
                self.kp.save_memory()
            except Exception:
                pass
        self.session.save()
        self._e("system", f"── 本局结束{('：' + reason) if reason else ''} ──")

    def _run_retrospectives(self) -> None:
        digest = _session_digest(self.session)
        if not digest.strip():
            return
        title = self.module.title if self.module else "自由跑团"
        self._e("system", "── 散场 · 各人复盘这一局 ──")

        # 守秘人也要复盘：今天当 KP 的人，下一局可能就是玩家，
        # 他这一局对同桌人的观感、对自己的总结，同样该留下来。
        everyone: list[tuple[BaseAgent, str]] = []
        if self.kp:
            everyone.append((self.kp, "KP"))
        for pl in self.pls:
            everyone.append((pl, "PL"))

        for agent, role in everyone:
            label = agent.card.display_name or agent.display_name
            self._e("system", f"{label}（{'守秘人' if role == 'KP' else '玩家'}）"
                              f"正在想这一局留下了什么…", seat_id=agent.seat_id)
            try:
                res = agent.retrospective(
                    digest, module_title=title,
                    fate=(self._fate(agent) if role == "PL" else "当了一局守秘人"))
            except Exception as e:  # noqa: BLE001
                self._e("system", f"复盘出错：{type(e).__name__}: {e}", seat_id=agent.seat_id)
                continue
            if not res.get("ok"):
                self._e("system", f"复盘失败：{res.get('error')}", seat_id=agent.seat_id)
                continue
            if res.get("ooc"):
                self._e("ooc", res["ooc"], seat_id=agent.seat_id,
                        name=label, meta={"retro": True})
            changes = res.get("changes") or []
            self._e("system",
                    f"〔跨周目〕{label}：" + ("；".join(changes) if changes
                                              else "这一局没留下什么新东西。"),
                    seat_id=agent.seat_id, meta={"player_card": res.get("card")})

    def _fate(self, pl: PLAgent) -> str:
        attrs = (pl.memory.character_sheet.get("attributes") or {})
        hp = attrs.get("HP")
        san = attrs.get("SAN")
        if isinstance(hp, int) and hp <= 0:
            return "死了"
        if isinstance(san, int) and san <= 0:
            return "永久疯狂"
        return "活下来了"

    def token_report(self) -> dict[str, Any]:
        seats = [pl.token_report() for pl in self.pls]
        if self.kp:
            seats.insert(0, self.kp.token_report())
        pricing = self.cfg.get("pricing") or {}
        p_in = float(pricing.get("input", 0) or 0)
        p_cached = float(pricing.get("input_cached", p_in) or 0)
        p_out = float(pricing.get("output", 0) or 0)
        cost = 0.0
        for s in seats:
            fresh = max(0, s["prompt_tokens"] - s["cached_tokens"])
            cost += fresh / 1e6 * p_in
            cost += s["cached_tokens"] / 1e6 * p_cached
            cost += s["completion_tokens"] / 1e6 * p_out
        return {
            "seats": seats, "cost_estimate": round(cost, 4),
            "currency": "元",
            "total_prompt": sum(s["prompt_tokens"] for s in seats),
            "total_completion": sum(s["completion_tokens"] for s in seats),
            "total_cached": sum(s["cached_tokens"] for s in seats),
        }

    def context_report(self) -> dict[str, Any]:
        """给界面看的上下文体积明细——让用户知道 token 花在哪。"""
        rows = []
        for pl in self.pls:
            sys_len = len(pl.messages[0]["content"]) if pl.messages else 0
            hist_len = sum(len(m["content"]) for m in pl.messages[1:])
            rows.append({
                "seat_id": pl.seat_id, "name": pl.display_name, "kind": "PL",
                "system_chars": sys_len, "history_chars": hist_len,
                "history_msgs": max(0, len(pl.messages) - 1),
            })
        if self.kp:
            rows.insert(0, {
                "seat_id": self.kp.seat_id, "name": self.kp.display_name, "kind": "KP",
                "system_chars": len(self.kp.messages[0]["content"]) if self.kp.messages else 0,
                "history_chars": sum(len(m["content"]) for m in self.kp.messages[1:]),
                "history_msgs": max(0, len(self.kp.messages) - 1),
            })
        for r in rows:
            r["system_tokens"] = rules_mod.estimate_tokens("字" * r["system_chars"])
            r["history_tokens"] = rules_mod.estimate_tokens("字" * r["history_chars"])
        return {"rows": rows, "rules_detail": self.options.get("rules_detail", "lean")}


# ══════════════════════════════════════════════ 小工具

def _int(text: str, default: int = 0) -> int:
    m = re.search(r"[+-]?\d+", text or "")
    return int(m.group()) if m else default


def _push_pending(session: Session, seat_id: str, text: str) -> None:
    session.pending_private.setdefault(seat_id, []).append(text)


def _session_digest(session: Session, budget: int = 2600) -> str:
    """把一局的公开流水压成一段给复盘用的摘要。保留结尾——结局比开头重要。"""
    lines: list[str] = []
    for e in session.events:
        t = e.get("type")
        name = e.get("name", "")
        text = (e.get("text") or "").strip().replace("\n", " ")
        if not text:
            continue
        if t == "narr":
            lines.append("【守秘人】" + text[:200])
        elif t == "act":
            if (e.get("meta") or {}).get("anchor"):
                continue
            lines.append(f"【{name}】" + text[:160])
        elif t == "ooc":
            lines.append(f"（{name}）" + text[:120])
        elif t == "dice":
            lines.append("🎲 " + text[:120])
        elif t == "scene":
            lines.append(f"—— 场景：{text} ——")
    blob = "\n".join(lines)
    if len(blob) > budget:
        blob = "…（前略）\n" + blob[-budget:]
    return blob


def _push_dice(session: Session, seat_id: str, text: str) -> None:
    session.pending_dice.setdefault(seat_id, []).append(text)


def _log_private(session: Session, seat_id: str, label: str) -> None:
    """记下"谁私下收到了什么"（**只记标签、不记内容**）。

    用来给其他玩家生成知识边界——桌上你看见守秘人把一张纸递给了阿凛，
    但你不知道上面写了什么。这正是真人跑团里最常见的超游摩擦来源。
    """
    lst = session.seat_private_log.setdefault(seat_id, [])
    if label not in lst:
        lst.append(label)
    del lst[:-6]
