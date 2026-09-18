"""应用门面：UI 与引擎之间**唯一**的接口。

铁律：本文件不 import 任何 UI 库（webview / tkinter / PySide）。
这样引擎可以脱离界面被自动化测试驱动——`tools/selftest_offline.py` 就是这么跑的。

线程模型：
  · 所有会调 LLM 的动作都跑在后台工作线程里，界面永远不卡。
  · 事件通过 queue 交付，前端轮询 poll_events() 取。
    之所以不用主动 push：轮询在这个场景下不可见（一轮要几秒到几十秒），
    但可靠性高得多，也不会出现 push 与 poll 双份投递。
"""

from __future__ import annotations

import copy
import queue
import re
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from . import chargen, config as cfgmod, glossary, module_lib, player_memory
from . import rules as rules_mod
from . import errlog
from .llm import LLMError, probe_seat
from .session import Session
from .turns import GameLoop

MAX_EVENTS_PER_POLL = 400


class App:
    def __init__(self) -> None:
        self.cfg: dict[str, Any] = cfgmod.load_config()
        # 名册改过之后，老配置里的 player_id 可能已经不存在了——load_config 会
        # 自动重映射，这里把结果说给用户听，别让换人发生在暗处
        self.migration_notes: list[str] = list(self.cfg.pop("_migration_notes", []) or [])
        self.session: Session | None = None
        self.loop: GameLoop | None = None
        self.module: module_lib.Module | None = None

        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._running = threading.Event()
        self._auto = threading.Event()
        self._busy = threading.Lock()
        self.last_job_error = ""

    # ══════════════════════════════════════════════ 事件

    def _emit(self, evt: dict[str, Any]) -> None:
        self._events.put(evt)

    # ══════════════════════════════════════════════ 出错记录

    def log_client_error(self, message: str, detail: str = "") -> dict[str, Any]:
        """界面自己崩了也要留个记录。

        不然就是现在这样：用户说"报错了"，我翻遍 data\\ 只有一份 session.json，
        什么线索都没有。
        """
        errlog.log("界面", message, detail)
        return {"ok": True}

    def recent_errors(self, n: int = 30) -> dict[str, Any]:
        return {"items": errlog.recent(n), "path": str(errlog.log_path()),
                "tail": errlog.tail(160)}

    def clear_errors(self) -> dict[str, Any]:
        errlog.clear()
        return {"ok": True}

    def poll_events(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for _ in range(MAX_EVENTS_PER_POLL):
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                break
        return out

    def _sys(self, text: str) -> None:
        self._emit({"type": "system", "text": text, "round": self._round(), "ts": 0})

    def _round(self) -> int:
        return self.session.round if self.session else 0

    # ══════════════════════════════════════════════ 后台任务

    @property
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def wait_idle(self, timeout: float = 600.0) -> bool:
        """等当前后台任务结束。给自动化测试与"保存前先等一下"用。"""
        import time as _t
        deadline = _t.monotonic() + timeout
        while self.busy and _t.monotonic() < deadline:
            _t.sleep(0.05)
        return not self.busy

    def _run_async(self, fn: Callable[[], None], label: str = "") -> bool:
        if self.busy:
            self._sys(f"上一个任务（{label or '进行中'}）还没结束，请稍等。")
            return False
        self.last_job_error = ""

        def wrapper() -> None:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.last_job_error = f"{type(e).__name__}: {e}"
                errlog.log(f"后台任务/{label or 'job'}", self.last_job_error,
                           traceback.format_exc())
                self._sys(f"任务出错：{self.last_job_error}")
                traceback.print_exc()
            finally:
                self._running.clear()

        self._worker = threading.Thread(target=wrapper, daemon=True,
                                        name=f"coc-{label or 'job'}")
        self._running.set()
        self._worker.start()
        return True

    # ══════════════════════════════════════════════ 配置

    def bootstrap(self) -> dict[str, Any]:
        """界面启动时拉取的全部初始数据。"""
        for note in self.migration_notes:
            self._sys(f"〔名册变更〕座位上的「{note}」——凭据已保留，"
                      f"请在「🎭 名册与站位」里确认。")
        return {
            "config": cfgmod.public_config(self.cfg),
            "modules": module_lib.scan_modules(),
            "errors": errlog.recent(10),
            "sessions": Session.list_sessions()[:50],
            "players": player_memory.list_player_cards(),
            "roster": self.get_roster(),
            "studies": self.list_studies(),
            "glossary": glossary.coverage(),
            "rules_full": rules_mod.has_full_text(),
            "busy": self.busy,
        }

    def save_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        self.cfg = cfgmod.save_config(cfg)
        self._sys("设置已保存。")
        return cfgmod.public_config(self.cfg)

    def reset_config(self) -> dict[str, Any]:
        self.cfg = cfgmod.default_config()
        cfgmod.save_config(self.cfg)
        self._sys("已恢复默认设置。")
        return cfgmod.public_config(self.cfg)

    # ══════════════════════════════════════════════ 名册与站位

    def get_roster(self) -> dict[str, Any]:
        """名册 + 当前站位。站位可以随时轮换，记忆跟着人走。"""
        from . import roster as roster_mod
        entries = roster_mod.public_list()
        valid = {e["id"] for e in entries}
        kp = ""
        players: list[str] = []
        for s in self.cfg.get("seats", []):
            pid = (s.get("profile") or {}).get("player_id") or ""
            if pid not in valid:          # 悬空引用不要显示成一个人
                continue
            if s.get("kind") == "KP":
                kp = pid
            elif s.get("kind") == "PL":
                players.append(pid)
        if not kp and entries:            # 兜底：至少给个能用的守秘人
            kp = next((e["id"] for e in entries if e["default_role"] == "KP"),
                      entries[0]["id"])
        return {"entries": entries, "keeper": kp, "players": players}

    def save_roster(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        from . import roster as roster_mod
        p = roster_mod.save_roster(entries or [])
        if self.loop:
            self.loop.cfg = self.cfg
        self._sys("名册已保存，下局生效。")
        return {"ok": True, "path": str(p), "roster": self.get_roster()}

    def assign_roles(self, kp_player_id: str,
                     pl_player_ids: list[str]) -> dict[str, Any]:
        """重排站位：谁当守秘人、谁当玩家。

        「今天你当 KP，明天他当 KP」——凭据按**人**继承（换位置不会丢 Key），
        跨周目记忆本来就按 player_id 存，也自动跟着人走。
        """
        if self.busy:
            return {"ok": False, "message": "有任务在跑，先停下再来。"}
        if not kp_player_id:
            return {"ok": False, "message": "得先指定一个人当守秘人。"}
        self.cfg = cfgmod.apply_roster(self.cfg, kp_player_id, list(pl_player_ids or []))
        cfgmod.save_config(self.cfg)
        if self.loop:
            self.loop.cfg = self.cfg
        labels = {s["seat_id"]: s["display_name"] for s in self._seat_states()}
        kp_name = labels.get("kp", kp_player_id)
        pl_names = [labels.get(s["seat_id"], "") for s in self._seat_states()
                    if s["kind"] == "PL"]
        self._sys(f"站位已调整，守秘人：{kp_name}；玩家：{'、'.join(pl_names)}")
        return {"ok": True, "config": cfgmod.public_config(self.cfg),
                "roster": self.get_roster()}

    def rotate_keeper(self) -> dict[str, Any]:
        """轮到下一个人当守秘人。原守秘人补进玩家席，人数保持不变。"""
        from . import roster as roster_mod
        r = self.get_roster()
        nxt = roster_mod.next_keeper(r["keeper"])
        players = [p for p in r["players"] if p and p != nxt.id]
        if r["keeper"] and r["keeper"] != nxt.id and r["keeper"] not in players:
            players.insert(0, r["keeper"])
        res = self.assign_roles(nxt.id, players)
        if res.get("ok"):
            self._sys(f"换庄：这一局由「{nxt.handle}」当守秘人。")
        return res

    def set_player_count(self, n: int, kp_player_id: str = "") -> dict[str, Any]:
        """设定玩家人数。人不够就从名册里补没上桌的人。"""
        from . import roster as roster_mod
        n = max(0, min(6, int(n)))
        r = self.get_roster()
        kp = kp_player_id or r["keeper"] or roster_mod.default_kp().id
        current = [p for p in r["players"] if p]
        chosen = current[:n]
        for e in r["entries"]:
            if len(chosen) >= n:
                break
            if e["id"] != kp and e["id"] not in chosen:
                chosen.append(e["id"])
        return self.assign_roles(kp, chosen)

    def add_pl_seat(self, player_id: str = "") -> dict[str, Any]:
        r = self.get_roster()
        chosen = [p for p in r["players"] if p]
        if not player_id:
            for e in r["entries"]:
                if e["id"] != r["keeper"] and e["id"] not in chosen:
                    player_id = e["id"]
                    break
        if not player_id or player_id == r["keeper"] or player_id in chosen:
            return {"ok": False, "message": "没有可加的人（名册里的人都在桌上了）。"}
        chosen.append(player_id)
        return self.assign_roles(r["keeper"], chosen)

    def remove_pl_seat(self, token: str) -> dict[str, Any]:
        """token 可以是 seat_id 也可以是 player_id。"""
        r = self.get_roster()
        pid = token
        for s in self.cfg.get("seats", []):
            if s.get("seat_id") == token:
                pid = (s.get("profile") or {}).get("player_id") or token
                break
        chosen = [p for p in r["players"] if p and p != pid]
        if len(chosen) == len([p for p in r["players"] if p]):
            return {"ok": False, "message": "没找到这个玩家。"}
        return self.assign_roles(r["keeper"], chosen)

    def preset_options(self) -> dict[str, Any]:
        return {"providers": cfgmod.PROVIDERS}

    # ══════════════════════════════════════════════ 连接测试

    def fetch_models(self, seat_id: str = "", base_url: str = "",
                     api_key: str = "") -> dict[str, Any]:
        """拉取模型列表，省得手打模型名。

        给 seat_id 就用那个座位已经保存好的地址和 Key；
        也可以直接给 base_url / api_key（OCR 那个视觉模型面板就是这么用的）。
        """
        from .llm import fetch_models as _fetch
        label = ""
        if seat_id:
            seat = cfgmod.find_seat(self.cfg, seat_id)
            if not seat:
                return {"ok": False, "message": "找不到这个座位。"}
            base_url = seat.get("base_url", "")
            api_key = seat.get("api_key", "")
            label = seat.get("display_name") or seat_id
        if not (api_key or "").strip() and not base_url:
            return {"ok": False, "message": "先填 Base URL 和 API Key（填完点一下保存）。"}
        res = _fetch(base_url, api_key)
        if res.get("ok"):
            cfgmod.remember_models(self.cfg, base_url, res["models"])
            cfgmod.save_config(self.cfg)
            self._sys(f"〔模型列表〕{label or base_url} 拉到 {res['count']} 个模型"
                      f"（已记下来，下次直接点选）。")
        else:
            errlog.log(f"拉取模型列表/{label or base_url}",
                       str(res.get("message", "")), f"base_url={base_url}")
            self._sys(f"〔模型列表〕拉取失败：{res.get('message', '')[:120]}")
        return res

    def probe(self, seat_id: str) -> dict[str, Any]:
        seat = cfgmod.find_seat(self.cfg, seat_id)
        if not seat:
            return {"ok": False, "message": "找不到这个座位。"}
        ok, msg = probe_seat(seat)
        if not ok:
            errlog.log(f"连接测试/{seat.get('display_name', seat_id)}", msg,
                       f"base_url={seat.get('base_url', '')} "
                       f"model={seat.get('model', '')}")
        self._sys(f"〔连接测试〕{seat.get('display_name', seat_id)}：{msg}")
        return {"ok": ok, "message": msg}

    def probe_all(self) -> list[dict[str, Any]]:
        out = []
        for seat in self.cfg.get("seats", []):
            if not seat.get("enabled", True):
                continue
            ok, msg = probe_seat(seat)
            out.append({"seat_id": seat.get("seat_id"),
                        "display_name": seat.get("display_name"),
                        "ok": ok, "message": msg})
            self._sys(f"〔连接测试〕{seat.get('display_name')}：{msg}")
        return out

    # ══════════════════════════════════════════════ 模组

    def module_detail(self, module_id: str) -> dict[str, Any]:
        m = module_lib.load_module(module_id)
        if not m:
            return {"ok": False, "message": f"找不到模组 {module_id}"}
        return {"ok": True, "module": m.to_dict()}

    def open_player_folder(self, player_id: str) -> dict[str, Any]:
        """打开某个玩家的专属文件夹（记忆 + 角色卡都在里面）。"""
        p = player_memory.player_dir(player_id)
        return self._open_folder(p)

    def open_seat_folder(self, seat_id: str) -> dict[str, Any]:
        seat = cfgmod.find_seat(self.cfg, seat_id)
        if not seat:
            return {"ok": False, "message": "找不到该座位。"}
        pid = (seat.get("profile") or {}).get("player_id")
        if not pid:
            return {"ok": False, "message": "这个座位没有绑定名册上的人。"}
        return self.open_player_folder(pid)

    @staticmethod
    def _open_folder(p: Any) -> dict[str, Any]:
        try:
            import os
            os.startfile(str(p))          # noqa: S606  Windows 专用，故意的
            return {"ok": True, "path": str(p)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "path": str(p),
                    "message": f"打不开资源管理器，你手动去这个目录：{p}（{e}）"}

    def open_modules_dir(self) -> str:
        return str(cfgmod.modules_root())

    def open_modules_folder(self) -> dict[str, Any]:
        """在资源管理器里打开模组仓库，方便把下载来的模组直接粘进去。"""
        p = cfgmod.modules_root()
        try:
            import os
            os.startfile(str(p))          # noqa: S606  Windows 专用，故意的
            return {"ok": True, "path": str(p)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "path": str(p),
                    "message": f"打不开资源管理器，你手动去这个目录：{p}（{e}）"}

    # ══════════════════════════════════════════════ 扫描版 PDF → OCR

    def vision_settings(self) -> dict[str, Any]:
        from . import ocr as ocr_mod
        raw = ocr_mod.vision_config(self.cfg)
        out = dict(raw)
        out["api_key"] = cfgmod.mask_key(raw.get("api_key", ""))
        out["api_key_set"] = bool(raw.get("api_key"))
        return out

    def save_vision_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        from . import ocr as ocr_mod
        cur = ocr_mod.vision_config(self.cfg)
        merged = {**cur, **(data or {})}
        key = str(merged.get("api_key") or "").strip()
        if key in (cfgmod.KEEP, cfgmod.mask_key(cur.get("api_key", ""))):
            merged["api_key"] = cur.get("api_key", "")
        self.cfg["vision"] = merged
        cfgmod.save_config(self.cfg)
        self._sys("OCR 用的视觉模型配置已保存。")
        return {"ok": True, "vision": self.vision_settings()}

    def probe_pdf(self, path: str) -> dict[str, Any]:
        """看看这个 PDF 是文本型还是扫描型——决定要不要 OCR。"""
        from . import ocr as ocr_mod
        p = Path(str(path).strip().strip('"'))
        if not p.exists():
            return {"ok": False, "message": f"找不到文件：{p}"}
        info = ocr_mod.probe_pdf(p)
        info["ok"] = not info.get("error")
        info["name"] = p.name
        return info

    def ocr_pdf(self, path: str, module_id: str = "",
                title: str = "", pages: str = "") -> dict[str, Any]:
        """把扫描版 PDF 识别成可跑的模组。跑在后台线程里，界面不会卡。"""
        if self.busy:
            return {"ok": False, "message": "有任务在跑，先停下再来。"}
        from . import ocr as ocr_mod
        p = Path(str(path).strip().strip('"'))
        if not p.exists():
            return {"ok": False, "message": f"找不到文件：{p}"}
        vcfg = ocr_mod.vision_config(self.cfg)
        if not (vcfg.get("base_url") and vcfg.get("model")):
            return {"ok": False, "message":
                    "还没配视觉模型。OCR 需要一个看得懂图的接口——"
                    "在下面的面板里填 Base URL / Key / 模型名。"
                    "（DeepSeek 官方目前没有视觉模型，这一步通常要另配一家。）"}

        page_idx: list[int] | None = None
        if pages.strip():
            out: list[int] = []
            for part in pages.split(","):
                part = part.strip()
                if "-" in part:
                    a, _, b = part.partition("-")
                    try:
                        out.extend(range(int(a) - 1, int(b)))
                    except ValueError:
                        pass
                elif part.isdigit():
                    out.append(int(part) - 1)
            page_idx = sorted({i for i in out if i >= 0}) or None

        def job() -> None:
            self._sys(f"── 开始识别扫描版 PDF：{p.name} ──")
            last = [0]

            def progress(msg: str, i: int, n: int) -> None:
                if i == n or i - last[0] >= 3:
                    last[0] = i
                    self._sys(f"OCR {i}/{n}　{msg}")

            res = ocr_mod.ocr_pdf(p, vcfg, pages=page_idx, progress=progress)
            if not res.ok:
                self._sys(f"OCR 失败：{res.error}")
                if res.failed:
                    self._sys(f"失败的页：{res.failed[:20]}　"
                              f"（已完成的 {res.pages_done} 页有缓存，修好重跑会跳过）")
                return
            root = ocr_mod.build_module_from_ocr(p, res.text, module_id, title)
            self._sys(f"OCR 完成：{res.pages_done}/{res.pages_total} 页"
                      f"（缓存命中 {res.from_cache} 页）")
            self._sys(f"模组已生成：{root}")
            self._sys("建议打开 module_info.yaml 补上 era（时代背景）和 players"
                      "，守秘人审卡时会用到 era。")

        self._run_async(job, "ocr")
        return {"ok": True, "message": "OCR 任务已启动"}

    # ══════════════════════════════════════════════ 模组研读室

    def study_room(self, module_id: str, force: bool = False) -> dict[str, Any]:
        """开一间研读室：**只有你和守秘人**，没有玩家。

        它会先通读模组、做一份功课，再给你一份**不藏着的**通读报告——
        幕后真相、骨架、难点、吐槽，全摊开说。因为这里没有玩家，
        它的保密义务不存在。
        """
        if self.busy:
            return {"ok": False, "message": "有任务在跑，先停下再来。"}
        mid = (module_id or "").strip()
        if not mid:
            return {"ok": False, "message": "先选一个模组。"}
        module = module_lib.load_module(mid)
        if not module:
            return {"ok": False, "message": f"找不到模组 {mid}"}

        cfg = copy.deepcopy(self.cfg)
        cfg["seats"] = [s for s in cfg.get("seats", []) if s.get("kind") == "KP"]
        if not cfg["seats"]:
            return {"ok": False, "message": "没有可用的守秘人座位，先去「设置」配一个。"}

        self.module = module
        # 研读室按**模组**复用同一个会话目录：同一个本反复打开，聊过的话还在。
        sid = "study-" + re.sub(r'[\\/:*?"<>|]+', "_", mid)[:60]
        prev = Session.load(sid)
        self.session = prev if (prev and prev.mode == "study") else Session(sid, module_id=mid)
        self.session.module_id = mid
        self.session.mode = "study"
        self.session.phase = "study"
        try:
            self.loop = GameLoop(cfg, self.session, self._emit, module, study_mode=True)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

        already = bool(self.session.study_report) and not force

        def job() -> None:
            assert self.loop and self.session
            self.loop._ensure_kp()
            kp = self.loop.kp
            assert kp is not None
            name = self.loop._player_of(kp)
            self._sys(f"── 研读室 ·《{module.title}》──")
            for w in (module.warnings or []):
                self._sys(f"模组提示：{w}")
            self._sys(f"这个模组被切成了 {len(module.scenes)} 幕，"
                      f"守秘人资料约 {len(module.truth):,} 字。")
            if already and self.session.study_report:
                self._sys("上次谈过的内容还在，已经摆回桌上了。")
                self._emit({"type": "study_report", "text": self.session.study_report,
                            "name": name, "seat_id": "kp", "round": 0, "ts": 0,
                            "meta": {"restored": True}})
                for t in self.session.study_talk:
                    self._emit({"type": "study_talk", "text": t.get("text", ""),
                                "name": t.get("who", ""), "round": 0, "ts": 0,
                                "meta": {"restored": True}})
                return

            self.loop._kp_study(force=force)

            self._sys("── 守秘人正在写通读报告 ──")
            brief = self.loop._module_brief(include_party=False)
            res = kp.study_report(brief, module.title)
            if not res.get("ok"):
                self._sys(f"通读报告没写出来：{res.get('error')}")
            else:
                self.session.study_report = res["report"]
                self._emit({"type": "study_report", "text": res["report"],
                            "name": name, "seat_id": "kp", "round": 0, "ts": 0})
            if res.get("ooc"):
                self.session.study_talk.append({"who": name, "text": res["ooc"]})
                self._emit({"type": "study_talk", "text": res["ooc"], "name": name,
                            "seat_id": "kp", "round": 0, "ts": 0})
            self.session.save()
            self._sys("── 下面你可以直接问它任何关于这个模组的问题"
                      "（它不会藏）──")

        self._run_async(job, "study")
        return {"ok": True, "message": "研读室已开", "session": self.state()}

    def study_ask(self, question: str) -> dict[str, Any]:
        """在研读室里问守秘人一个问题。"""
        q = (question or "").strip()
        if not q:
            return {"ok": False, "message": "没有内容。"}
        if not self.loop or not self.session or self.session.mode != "study":
            return {"ok": False, "message": "研读室还没开。先点「📖 让 KP 读一遍」。"}
        if self.busy:
            return {"ok": False, "message": "上一个问题还在回答，等一下。"}

        def job() -> None:
            assert self.loop and self.session
            self.loop._ensure_kp()
            kp = self.loop.kp
            assert kp is not None
            name = self.loop._player_of(kp)
            self.session.study_talk.append({"who": "导演", "text": q})
            self._emit({"type": "study_talk", "text": q, "name": "导演",
                        "round": 0, "ts": 0})
            brief = self.loop._module_brief(include_party=False)
            res = kp.study_answer(brief, self.session.module_study,
                                  self.session.study_talk[:-1], q)
            if not res.get("ok"):
                self._sys(f"守秘人没答上来：{res.get('error')}")
                return
            self.session.study_talk.append({"who": name, "text": res["answer"]})
            self._emit({"type": "study_talk", "text": res["answer"], "name": name,
                        "seat_id": "kp", "round": 0, "ts": 0})
            self.session.save()

        self._run_async(job, "study_ask")
        return {"ok": True}

    def study_state(self) -> dict[str, Any]:
        s = self.session
        if not s or s.mode != "study":
            return {"open": False}
        return {
            "open": True,
            "module_id": s.module_id,
            "module_title": self.module.title if self.module else "",
            "scene_count": len(self.module.scenes) if self.module else 0,
            "report": s.study_report,
            "talk": s.study_talk,
            "study": s.module_study,
            "busy": self.busy,
        }

    def list_studies(self) -> list[dict[str, Any]]:
        from . import study as study_mod
        return study_mod.list_studies()

    def forget_study(self, module_id: str) -> dict[str, Any]:
        """忘掉某个模组的研读（下次会重新读一遍）。"""
        from . import study as study_mod
        ok = study_mod.delete_study(module_id)
        self._sys(f"已忘掉《{module_id}》的研读记录。" if ok else "没有这个模组的研读记录。")
        return {"ok": ok}

    def module_study(self) -> dict[str, Any]:
        """守秘人开局前做的功课（理解 / 大纲 / 扩展 / 彩蛋），导演可见。"""
        if not self.session:
            return {"ok": False, "message": "还没有会话。", "study": {}}
        return {"ok": True, "study": self.session.module_study or {},
                "revealed": self.session.module_revealed,
                "module_title": self.module.title if self.module else ""}

    # ══════════════════════════════════════════════ 规则

    def search_rules(self, query: str) -> dict[str, Any]:
        return {"query": query, "hits": rules_mod.retrieve_rules(query, k=5)}

    def glossary_terms(self) -> dict[str, str]:
        return glossary.all_terms()

    def roll_now(self, expr: str = "1d100", purpose: str = "手动掷骰") -> dict[str, Any]:
        """手动掷一次骰（界面上的骰子台用）。走的是同一个内核。"""
        from .dice import DiceKernel
        expr = (expr or "1d100").strip() or "1d100"
        if self.loop:
            info = self.loop.kernel.do_roll("导演", expr, purpose, source="manual")
            self.loop._public_dice.append(f"导演：{info['summary']}")
        else:
            info = DiceKernel().do_roll("导演", expr, purpose, source="manual")
        self._emit({"type": "dice", "text": info["summary"], "name": "导演",
                    "seat_id": "", "round": self._round(), "ts": 0,
                    "meta": {**info, "manual": True}})
        return {"ok": True, **info}

    def dice_log(self) -> dict[str, Any]:
        """骰子审计。这是「拒绝骰子服务于剧情」可以被查证的地方。"""
        if not self.loop:
            return {"ok": False, "message": "还没有会话。", "audit": {}, "recent": []}
        k = self.loop.kernel
        return {"ok": True, "audit": k.audit(), "recent": k.recent(80),
                "turn": k.turn}

    # ══════════════════════════════════════════════ 会话

    def new_session(self, module_id: str = "", premise: str = "") -> dict[str, Any]:
        if self.busy:
            return {"ok": False, "message": "有任务在跑，先停下再来。"}
        self.module = module_lib.load_module(module_id) if module_id else None
        self.session = Session(module_id=module_id, premise=premise or "")
        self.session.phase = "setup"
        self.cfg.setdefault("module", {})
        self.cfg["module"]["id"] = module_id
        self.cfg["ui"] = {**(self.cfg.get("ui") or {}), "last_session": self.session.session_id}
        cfgmod.save_config(self.cfg)
        try:
            self.loop = GameLoop(self.cfg, self.session, self._emit, self.module)
        except Exception as e:  # noqa: BLE001
            self._sys(f"初始化失败：{e}")
            return {"ok": False, "message": str(e)}
        self.session.save()
        self._sys(f"新会话已建立：{self.session.session_id}"
                  f"（模组：{self.module.title if self.module else '自由跑团'}）")
        if self.module and self.module.warnings:
            for w in self.module.warnings:
                self._sys(f"模组提示：{w}")
        return {"ok": True, "session": self.state()}

    def load_session(self, session_id: str) -> dict[str, Any]:
        if self.busy:
            return {"ok": False, "message": "有任务在跑，先停下再来。"}
        s = Session.load(session_id)
        if not s:
            return {"ok": False, "message": f"找不到会话 {session_id}"}
        self.session = s
        self.module = module_lib.load_module(s.module_id) if s.module_id else None
        try:
            self.loop = GameLoop(self.cfg, self.session, self._emit, self.module)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

        # 把存档里的角色卡还原到座位与记忆上
        for seat in self.cfg.get("seats", []):
            st = s.seats.get(seat.get("seat_id")) or {}
            if seat.get("kind") == "PL" and st.get("character"):
                seat["character"] = st["character"]

        # 回放历史事件到界面
        for evt in s.events[-600:]:
            self._emit(dict(evt, replay=True))
        self._sys(f"已载入会话 {session_id}（第 {s.round} 轮）")
        return {"ok": True, "session": self.state()}

    def _study_guard(self) -> dict[str, Any] | None:
        """研读室里没有玩家，跑团的那几个按钮点了也不该有反应。"""
        if self.session and self.session.mode == "study":
            return {"ok": False,
                    "message": "现在是研读室（桌上只有你和 KP），没法跑团。"
                               "想开真局就点左下角的「用这个模组开真局」。"}
        return None

    def prepare(self) -> dict[str, Any]:
        """车卡：掷属性 + 让每个 PL 按规则书自己造一张卡。"""
        if not self.session:
            return {"ok": False, "message": "还没有建立会话。"}
        guard = self._study_guard()
        if guard:
            return guard

        def job() -> None:
            assert self.loop and self.session
            self._sys("── 掷属性并开始车卡，这需要一些时间 ──")
            ok = self.loop.prepare()
            self._sys("车卡完成，可以开局了。" if ok else "车卡失败。")

        self._run_async(job, "chargen")
        return {"ok": True, "message": "车卡任务已启动"}

    def start(self) -> dict[str, Any]:
        """开局：守秘人开场 + 各 PL 入戏锚定。"""
        if not self.session or not self.loop:
            return {"ok": False, "message": "还没有建立会话。"}
        guard = self._study_guard()
        if guard:
            return guard
        if self.session.phase == "setup":
            return {"ok": False, "message": "请先点击『车卡』。"}

        def job() -> None:
            assert self.loop
            self._sys("── 守秘人开场 ──")
            ok = self.loop.begin()
            self._sys("开局完成，可以开始跑团了。" if ok else "开局失败。")

        self._run_async(job, "begin")
        return {"ok": True, "message": "开局任务已启动"}

    def step(self) -> dict[str, Any]:
        if not self.session or not self.loop:
            return {"ok": False, "message": "还没有建立会话。"}
        guard = self._study_guard()
        if guard:
            return guard

        def job() -> None:
            assert self.loop
            self.loop.reset_stop()
            cont = self.loop.step()
            if not cont:
                self.loop.finish(self.session.ended_reason or "单步结束")

        self._run_async(job, "step")
        return {"ok": True, "message": "推进一轮"}

    def run_auto(self, rounds: int | None = None) -> dict[str, Any]:
        """自动推进。随时可以按停止。"""
        if not self.session or not self.loop:
            return {"ok": False, "message": "还没有建立会话。"}
        guard = self._study_guard()
        if guard:
            return guard

        limit = int(rounds or self.cfg.get("options", {}).get("max_rounds", 40))

        def job() -> None:
            assert self.loop and self.session
            self.loop.reset_stop()
            self._auto.set()
            self._sys(f"── 自动推进开始（上限 {limit} 轮，随时可停）──")
            for _ in range(limit):
                if self.loop.stopped:
                    break
                if not self.loop.step():
                    break
            else:
                self.session.ended_reason = f"达到本轮自动推进上限 {limit} 轮"
                self._sys(f"── 已推进 {limit} 轮，自动暂停 ──")
            self.loop.finish(self.session.ended_reason or "手动停止")
            self._auto.clear()

        self._run_async(job, "auto")
        return {"ok": True, "message": "自动推进已启动"}

    def stop(self) -> dict[str, Any]:
        if self.loop:
            self.loop.request_stop()
            self._sys("已请求停止，当前这一轮走完就停。")
        return {"ok": True}

    def director_note(self, text: str) -> dict[str, Any]:
        """人类导演插话：会作为引擎提示交给守秘人。"""
        text = (text or "").strip()
        if not text or not self.session:
            return {"ok": False, "message": "没有内容。"}
        self.session.director_notes.append(text)
        self.session.log({"type": "director", "text": text, "name": "导演",
                          "round": self.session.round})
        self._emit({"type": "director", "text": text, "name": "导演",
                    "round": self.session.round})
        return {"ok": True}

    def finish(self) -> dict[str, Any]:
        guard = self._study_guard()
        if guard:
            return guard
        if self.loop:
            self.loop.finish("手动结束")
        return {"ok": True, "session": self.state()}

    # ══════════════════════════════════════════════ 状态查询

    def state(self) -> dict[str, Any]:
        s = self.session
        seats = self._seat_states()      # 席位属于**配置**，不该依赖会话是否存在
        if not s:
            return {
                "has_session": False, "busy": self.busy, "auto": False,
                "session_id": "", "module_id": "", "module_title": "",
                "scenes": [], "scene_id": "", "round": 0, "phase": "setup",
                "mode": "setup",
                "ended_reason": "", "seats": seats,
                "tokens": None, "context": None,
                "last_error": self.last_job_error,
            }
        return {
            "has_session": True,
            "busy": self.busy,
            "auto": self._auto.is_set(),
            "session_id": s.session_id,
            "module_id": s.module_id,
            "module_title": self.module.title if self.module else "",
            "mode": s.mode,
            "scenes": ([{"id": sc.id, "title": sc.title} for sc in self.module.scenes]
                       if self.module else []),
            "scene_id": s.scene_id,
            "round": s.round,
            "phase": s.phase,
            "clock": (s.clock.render() if s.clock else ""),
            "clock_short": (s.clock.short() if s.clock else ""),
            "ended_reason": s.ended_reason,
            "seats": seats,
            "tokens": self.loop.token_report() if self.loop else None,
            "context": self.loop.context_report() if self.loop else None,
            "last_error": self.last_job_error,
        }

    def _seat_states(self) -> list[dict[str, Any]]:
        out = []
        s = self.session
        for seat in self.cfg.get("seats", []):
            sid = seat.get("seat_id")
            st = (s.seats.get(sid) if s else None) or {}
            char = (st.get("character") if st else None) or seat.get("character") or {}
            attrs = (char or {}).get("attributes") or {}
            prof = seat.get("profile") or {}
            handle = prof.get("player_name") or seat.get("display_name") or sid
            char_name = str((char or {}).get("name") or "").strip()
            # 桌上怎么称呼这个座位：
            #   车卡之前（还没有角色）→ 就是**玩家本人**，显示网名；
            #   车出角色之后          → `角色名（网名）`，一眼看出谁在演谁。
            # 名字是 AI 自己取的，引擎不预设（见 config.make_pl_seat）。
            label = f"{char_name}（{handle}）" if char_name and handle != char_name \
                else (char_name or handle)
            out.append({
                "seat_id": sid,
                "kind": seat.get("kind"),
                "label": label,
                "has_character": bool(char_name),
                # display_name 保留"角色名"的原意：提示词里、叙述里用的是它
                "display_name": char_name or seat.get("display_name"),
                "player_name": prof.get("player_name", ""),
                "handle": prof.get("handle", ""),
                "occupation": char.get("occupation", ""),
                "model": seat.get("model", ""),
                "provider": seat.get("provider", ""),
                "enabled": seat.get("enabled", True),
                "set_up": cfgmod.seat_ready(seat)[0],
                "attrs": attrs,
                "skills": char.get("skills") or [],
                "inventory": char.get("inventory") or [],
                "backstory": char.get("backstory", ""),
                "warnings": st.get("chargen_warnings") or [],
                # 车卡过程：引擎改了哪些、最后花了多少 / 预算多少。
                # 界面拿这些显示一句中性的话，而不是把修复前的违规当红字报警。
                "chargen_repairs": st.get("chargen_repairs") or [],
                "chargen_violations": st.get("chargen_violations") or [],
                "chargen_spent": st.get("chargen_spent"),
                "chargen_budget": st.get("chargen_budget"),
                "ready": bool(st.get("ready")),
                "sheet_path": st.get("sheet_path", ""),
                "player_id": (seat.get("profile") or {}).get("player_id", ""),
                "player_folder": str(player_memory.player_dir(
                    (seat.get("profile") or {}).get("player_id")))
                if (seat.get("profile") or {}).get("player_id") else "",
            })
        return out

    def seat_memory(self, seat_id: str) -> dict[str, Any]:
        """取某个座位的记忆卡（界面右栏用）。"""
        if not self.loop:
            return {"ok": False, "message": "还没有会话。"}
        agent = None
        if self.loop.kp and self.loop.kp.seat_id == seat_id:
            agent = self.loop.kp
        for pl in self.loop.pls:
            if pl.seat_id == seat_id:
                agent = pl
        if agent is None:
            return {"ok": False, "message": "找不到该座位。"}
        card = agent.memory
        return {
            "ok": True,
            "seat_id": seat_id,
            "display_name": card.agent_label,
            "kind": card.kind,
            "character_sheet": card.character_sheet,
            "player_profile": card.player_profile,
            "situation": card.situation,
            "chronicle": [e.to_dict() for e in card.chronicle],
            "tree": card.tree_dicts(),
            "tokens": agent.token_report(),
        }

    def transcript(self, limit: int = 300) -> list[dict[str, Any]]:
        if not self.session:
            return []
        return self.session.events[-limit:]

    def export_markdown(self) -> dict[str, Any]:
        if not self.session:
            return {"ok": False, "message": "还没有会话。"}
        p = self.session.export_markdown()
        self._sys(f"跑团记录已导出：{p}")
        return {"ok": True, "path": str(p)}

    def save_now(self) -> dict[str, Any]:
        if not self.session:
            return {"ok": False, "message": "还没有会话。"}
        if self.loop:
            for pl in self.loop.pls:
                pl.save_memory()
            if self.loop.kp:
                self.loop.kp.save_memory()
        self.session.save()
        self._sys("已存档。")
        return {"ok": True}

    def resume_last(self) -> dict[str, Any]:
        """启动时把上一局接回来。

        数据本来就一直在落盘（transcript.jsonl 逐条 append），
        但界面如果开局是空的，用户就会以为记录没了——所以这里主动接回去。
        """
        if self.session:
            return {"ok": True, "already": True, "session": self.state()}
        sessions = Session.list_sessions()
        if not sessions:
            return {"ok": False, "message": "还没有任何存档。"}
        last = ((self.cfg.get("ui") or {}).get("last_session") or "").strip()
        ids = [s["session_id"] for s in sessions]
        target = last if last in ids else ids[0]
        res = self.load_session(target)
        res["already"] = False
        return res

    def list_sessions(self) -> list[dict[str, Any]]:
        return Session.list_sessions()

    # ══════════════════════════════════════════════ 跨周目玩家档案

    def player_cards(self) -> list[dict[str, Any]]:
        return player_memory.list_player_cards()

    def player_card(self, player_id: str) -> dict[str, Any]:
        card = player_memory.load_card(player_id)
        if not card:
            return {"ok": False, "message": "这个玩家还没有档案（一局都没跑过）。"}
        return {"ok": True, "card": card.to_dict()}

    def player_card_for_seat(self, seat_id: str) -> dict[str, Any]:
        seat = cfgmod.find_seat(self.cfg, seat_id)
        if not seat:
            return {"ok": False, "message": "找不到该座位。"}
        pid = player_memory.safe_id(
            (seat.get("profile") or {}).get("player_id")
            or (seat.get("profile") or {}).get("player_name") or seat_id)
        return self.player_card(pid)

    def save_player_card(self, data: dict[str, Any]) -> dict[str, Any]:
        pid = str((data or {}).get("player_id") or "").strip()
        if not pid:
            return {"ok": False, "message": "缺少 player_id。"}
        card = player_memory.PlayerCard.load_or_create(pid)
        card.load_dict(data)
        p = card.save()
        self._sys(f"玩家档案已保存：{card.display_name or pid}")
        return {"ok": True, "path": str(p), "card": card.to_dict()}

    def reset_player_card(self, player_id: str) -> dict[str, Any]:
        """清空某个玩家的跨周目记忆（会先备份成 .bak，不删数据）。"""
        p = player_memory.players_dir() / f"{player_memory.safe_id(player_id)}.yaml"
        if not p.exists():
            return {"ok": False, "message": "找不到该档案。"}
        backup = p.with_suffix(".yaml.bak")
        try:
            if backup.exists():
                backup.unlink()
            p.rename(backup)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"备份失败：{e}"}
        self._sys(f"已清空玩家档案 {player_id}（原文件备份为 {backup.name}）。")
        return {"ok": True, "backup": str(backup)}

    def preview_character(self, seat_id: str, seed: int | None = None) -> dict[str, Any]:
        """不改动会话，只掷一组属性给用户看看。"""
        import random as _r
        rng = _r.Random(seed)
        attrs = chargen.roll_attributes(rng)
        return {"attrs": attrs, "derived": chargen.derive(attrs)}
