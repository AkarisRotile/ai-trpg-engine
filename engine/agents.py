"""Agent 运行时：座位即身份。

每个座位持有一个**真实的 messages 数组**，这是"历史即身份"的落点：
  system   —— 身份断言 + 语域契约（每轮恒定，用于前缀缓存与人格锚定）
  assistant—— 该 PC 过去说过的原话与做过的动作，逐字保留
  user     —— 世界发生的事（纯虚构，零祈使句）+ 当前角色卡/已知信息 + 结构记号

模型因此是在**续写一段自己写了很久的记录**，而不是在**回答一个问题**。
角色一致性由历史自身维持，不再靠每回合重新说服——这是全套设计里最关键的一环。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import chargen, glossary as glossary_mod, memory as memory_mod, prompts
from . import player_memory
from . import rules as rules_mod
from .llm import BaseClient, LLMError, LLMResult, make_client
from .linter import (ACT_SOFT_LIMIT, KP_CHANNELS, PL_CHANNELS, LintReport,
                     clean_output, kp_overreach, lint, narr_too_long, too_long)

_TAG_NAMES = ("think|act|ooc|mem|narr|secret|state|roll|recall|brief|sheet"
              "|pitch|audit|spine|understanding|expansion|eggs|report|verdict|clock")
# 一段内容到哪里结束：遇到下一个开标签或闭标签就停。
# 以前写的是 `(?:</\1>|$)`，要求闭合标签一字不差。模型把 `</ooc>` 写成
# `</ ooc>` 或者多打一个空格，非贪婪匹配就退化成"一直吃到结尾"，
# 于是后面所有块都被吞进 ooc，裸标签直接漏到屏幕上。
TAG_RE = re.compile(
    rf"<[\s]*({_TAG_NAMES})[\s]*>(.*?)"
    rf"(?=<[\s]*/[\s]*(?:{_TAG_NAMES})[\s]*>|<[\s]*(?:{_TAG_NAMES})[\s]*>|$)",
    re.S | re.I)

# 残留的标签记号。屏幕上出现裸标签是最难看的失败，出口统一擦一遍。
_STRAY_TAG = re.compile(rf"</?[\s]*(?:{_TAG_NAMES})[\s]*/?>", re.I)

# 通用掷骰工具的调用格式：一行一条，「表达式 | 用途」
_ROLL_EXPR = re.compile(r"^[0-9dD+\-*\s]{1,40}$")


def parse_rolls(block: str) -> list[tuple[str, str]]:
    """解析 <roll> 通道。

    AI 自己决定什么时候掷什么——引擎只负责真的掷，并把**每一颗骰子的原始点数**
    回给它。模型碰不到随机源，所以出目不可能被摆布成"剧情需要的样子"。
    """
    out: list[tuple[str, str]] = []
    for raw in (block or "").splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if not line or line.startswith("#"):
            continue
        expr, _, purpose = line.partition("|")
        if not _:
            expr, _, purpose = line.partition("｜")
        expr = expr.replace(" ", "").strip().rstrip("：:")
        if "d" not in expr.lower() or not _ROLL_EXPR.match(expr):
            continue
        out.append((expr, purpose.strip()))
    return out

CHECK_RE = re.compile(
    r"^[\[\(（]?\s*(?:检定|投掷|掷骰)?申请?\s*[:：]?\s*"
    r"([^\s\[\]（）()：:，,。]+)\s*"
    r"(?:[\[\(（]\s*(常规|困难|极难|奖励|惩罚|regular|hard|extreme)\s*[\)）\]])?\s*[\]\)）]?\s*$",
    re.M,
)
CHECK_LINE = re.compile(r"^.*(?:检定申请|申请检定|申请骰|投掷申请).*$", re.M)

DIFFICULTY_MAP = {
    "常规": "regular", "困难": "hard", "极难": "extreme",
    "regular": "regular", "hard": "hard", "extreme": "extreme",
    "奖励": "regular", "惩罚": "regular",
}


# ══════════════════════════════════════════════════════════════ 输出结构

@dataclass
class CheckRequest:
    skill: str
    difficulty: str = "regular"
    bonus: int = 0
    penalty: int = 0


@dataclass
class Directive:
    kind: str
    args: list[str] = field(default_factory=list)

    def arg(self, i: int, default: str = "") -> str:
        """第 i 个参数，**从 1 开始**。

        解析器把一条 `<state>` 拆成 `[目标, 内容]`，而所有调用点写的都是
        「arg(1) = 目标，arg(2) = 内容」。这里以前是 0-based，于是 whisper、
        grant_handout、check、damage、san、advance_scene、note 全部**静默取错**
        参数：私聊发不出去、handout 发不出去、场景推不动、守秘人主动发起的
        检定从来不执行、备注永远是空的——而且只在日志里留一句"找不到目标"。

        别改回 0-based。
        """
        return self.args[i - 1] if 1 <= i <= len(self.args) else default


@dataclass
class ChannelOutput:
    raw: str = ""
    think: str = ""
    act: str = ""
    ooc: str = ""
    mem: str = ""
    narr: str = ""
    secret: str = ""
    state: str = ""
    roll: str = ""
    recall: str = ""
    free: str = ""
    checks: list[CheckRequest] = field(default_factory=list)
    directives: list[Directive] = field(default_factory=list)
    rolls: list[tuple[str, str]] = field(default_factory=list)
    recalls: list[str] = field(default_factory=list)
    norm: list[dict[str, Any]] = field(default_factory=list)
    roll_results: list[dict[str, Any]] = field(default_factory=list)
    open_rolls: list[dict[str, Any]] = field(default_factory=list)
    recall_results: list[dict[str, Any]] = field(default_factory=list)
    recall_passes: int = 0
    tool_passes: int = 0
    lint: LintReport | None = None
    usage: LLMResult | None = None
    repaired: bool = False
    notes: list[str] = field(default_factory=list)

    def public_text(self) -> str:
        return "\n".join(x for x in (self.act, self.ooc) if x.strip())


def _strip_stray_tags(s: str) -> str:
    """把通道内容里残留的标签记号擦掉。"""
    return _STRAY_TAG.sub("", s or "").strip()


def split_tags(text: str) -> dict[str, str]:
    """按标签切通道。容忍缺失或写歪的闭合标签。"""
    out: dict[str, str] = {}
    consumed: list[tuple[int, int]] = []
    for m in TAG_RE.finditer(text or ""):
        tag = m.group(1).lower()
        body = _strip_stray_tags(m.group(2))
        if body:
            out[tag] = (out.get(tag, "") + "\n" + body).strip()
        else:
            out.setdefault(tag, "")
        consumed.append((m.start(), m.end()))
    # 把标签外的散字收集起来——它往往是元层泄漏的所在地
    rest = []
    pos = 0
    for s, e in consumed:
        if s > pos:
            rest.append(text[pos:s])
        pos = max(pos, e)
    if pos < len(text):
        rest.append(text[pos:])
    free = "\n".join(_strip_stray_tags(r) for r in rest if _strip_stray_tags(r)).strip()
    if free:
        out["free"] = free
    return out


def parse_recalls(block: str) -> list[str]:
    """解析 <recall> 通道：一行一个想回忆起来的名词。"""
    out: list[str] = []
    for raw in (block or "").splitlines():
        line = raw.strip().lstrip("-*•").strip().strip("【】[]「」")
        if not line or line.startswith("#") or len(line) > 24:
            continue
        if line not in out:
            out.append(line)
    return out[:4]


_ATTR_RE = re.compile(r"\b(STR|CON|DEX|APP|POW|SIZ|INT|EDU|LUCK)\s*[:：]?\s*(\d{1,3})", re.I)


def parse_attrs_block(text: str) -> dict[str, int]:
    """从 <attrs>STR 60 CON 50 …</attrs> 里抽出购点方案。"""
    m = re.search(r"<attrs>(.*?)(?:</attrs>|$)", text or "", re.S | re.I)
    body = m.group(1) if m else ""
    out: dict[str, int] = {}
    for mm in _ATTR_RE.finditer(body):
        out[mm.group(1).upper()] = int(mm.group(2))
    return out


AUDIT_VERDICTS = ("通过", "质疑", "拿掉")


def parse_audit_rows(text: str, tag: str = "audit") -> list[dict[str, str]]:
    """解析审卡/裁决输出：`判定 | 座位 | 东西 | 话`。"""
    rows: list[dict[str, str]] = []
    m = re.search(rf"<{tag}>(.*?)(?:</{tag}>|$)", text or "", re.S | re.I)
    for raw in (m.group(1) if m else "").splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if not line or "|" not in line and "｜" not in line:
            continue
        parts = [p.strip() for p in re.split(r"[|｜]", line)]
        if len(parts) < 3 or parts[0] not in AUDIT_VERDICTS:
            continue
        rows.append({"verdict": parts[0], "seat": parts[1], "item": parts[2],
                     "note": parts[3] if len(parts) > 3 else ""})
    return rows


def parse_age(text: str) -> int:
    """从 <age>34</age> 里抽出年龄，退路是「34岁」这种写法。"""
    for pat in (r"<age>\s*(\d{1,3})\s*</age>", r"(\d{2,3})\s*岁"):
        m = re.search(pat, text or "", re.I)
        if m:
            try:
                v = int(m.group(1))
                if 10 <= v <= 99:
                    return v
            except ValueError:
                pass
    return 0


def parse_spine(text: str) -> list[str]:
    """从 <spine> 里抽出守秘人抄下来的场景 id。

    这是「大纲不能偏」的校验依据：它必须和模组实际的场景 id 对得上。
    """
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*•0123456789.、) ").strip()
        if not line or line.startswith("#"):
            continue
        token = re.split(r"[\s，,。；;：:（(【\[]+", line)[0].strip().strip("」』\"'")
        if token and token not in out:
            out.append(token)
    return out


def _tagged(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)(?:</{tag}>|$)", text or "", re.S | re.I)
    return (m.group(1) if m else "").strip()


def parse_pl_output(text: str, *, do_lint: bool = True) -> ChannelOutput:
    out = ChannelOutput(raw=text or "")
    tags = split_tags(text)
    for k in ("think", "act", "ooc", "mem", "free", "roll", "recall"):
        setattr(out, k, tags.get(k, ""))
    out.rolls = parse_rolls(out.roll)
    out.recalls = parse_recalls(out.recall)

    body = out.ooc or ""
    for m in CHECK_LINE.finditer(body):
        line = m.group(0)
        cm = CHECK_RE.match(line.strip())
        if not cm:
            continue
        skill = cm.group(1).strip()
        if not skill or len(skill) > 12:
            continue
        diff_cn = (cm.group(2) or "常规").strip()
        out.checks.append(CheckRequest(
            skill=skill,
            difficulty=DIFFICULTY_MAP.get(diff_cn, "regular"),
            bonus=1 if diff_cn == "奖励" else 0,
            penalty=1 if diff_cn == "惩罚" else 0,
        ))

    if do_lint:
        out.lint = lint(text)
    return out


def parse_kp_output(text: str, *, do_lint: bool = True) -> ChannelOutput:
    out = ChannelOutput(raw=text or "")
    tags = split_tags(text)
    for k in ("narr", "ooc", "secret", "state", "free", "think", "mem",
              "roll", "recall"):
        setattr(out, k, tags.get(k, ""))
    out.rolls = parse_rolls(out.roll)
    out.recalls = parse_recalls(out.recall)

    for raw_line in (out.state or "").splitlines():
        line = raw_line.strip().lstrip("-*•").strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        kind = parts[0].strip().lower().rstrip(":：")
        rest = parts[1].strip() if len(parts) > 1 else ""
        if kind == "note":
            out.directives.append(Directive("note", [rest]))
        elif kind == "advance_scene":
            out.directives.append(Directive("advance_scene", [rest]))
        else:
            tokens = rest.split(None, 1)
            target = tokens[0] if tokens else ""
            payload = tokens[1].strip() if len(tokens) > 1 else ""
            out.directives.append(Directive(kind, [target, payload]))

    # 守秘人的叙述以前完全不过哨兵：<narr> 根本不在旧正则的认识范围内，
    # 掉进 free 里也没人查。现在按 KP 的通道扫，<state> 那些指令自动跳过。
    if do_lint:
        out.lint = lint(text, channels=KP_CHANNELS)
    return out


def _skill_in_purpose(purpose: str, char: dict[str, Any]) -> tuple[str, int] | None:
    """从一句「用途」里认出是哪个技能检定。

    认得出来就说明这是一次技能检定，该走引擎的成功等级判定，
    而不是当成一记没头没脑的 1d100。
    """
    text = purpose or ""
    if not text:
        return None
    for s in char.get("skills") or []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip()
        if len(name) >= 2 and name in text:
            return name, int(s.get("value") or 0)
    return None


# ══════════════════════════════════════════════════════════════ 座位运行时

def chatter_with_sentinel(agent: Any, msgs: list[dict[str, Any]],
                          channels: tuple[str, ...]) -> str:
    """守秘人插一句话时的清洗（车卡桌边用）。

    以前这一整段是漏的：`PLAgent.chargen_chat` 和 `KPAgent.chargen_chat`
    都直接 `split_tags` 取 <ooc> 就交出去了，破折号、「你们呢」、
    摆依据全都不拦。守秘人那句带破折号的场面话就是这么发到桌上的。

    命中就静默重写一次，还脏就退回空串——车卡桌边少一句比脏一句好。
    """
    res = agent._call(msgs, phase="chargen_chat")
    text = (split_tags(res.text).get("ooc") or "").strip()
    rep = lint(text, channels=channels)
    if not rep.dirty:
        return text

    agent.stats.repairs += 1
    try:
        res2 = agent._call(
            list(msgs) + [
                {"role": "assistant", "content": res.text},
                {"role": "user", "content": prompts.build_repair_message(
                    rep.reason_text(), res.text)},
            ],
            phase="chargen_chat")
    except LLMError:
        return ""
    text2 = (split_tags(res2.text).get("ooc") or "").strip()
    if lint(text2, channels=channels).dirty:
        return ""
    return text2


@dataclass
class SeatStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    errors: int = 0
    last_error: str = ""
    repairs: int = 0
    tool_passes: int = 0     # 工具轮次数：几轮里"先掷/先回忆，再重说一遍"

    def add(self, res: LLMResult) -> None:
        self.calls += 1
        self.prompt_tokens += res.prompt_tokens
        self.completion_tokens += res.completion_tokens
        self.cached_tokens += res.cached_tokens


class BaseAgent:
    kind = "?"

    def __init__(self, seat: dict[str, Any], options: dict[str, Any],
                 session_id: str, seed: int | None = None) -> None:
        self.seat = dict(seat)
        self.seat_id = seat.get("seat_id", "seat")
        self.display_name = seat.get("display_name", self.seat_id)
        self.options = dict(options or {})
        # 规则按详细度**分层组装**：默认只带判定核心，
        # 战斗/理智/车卡等分节在真正用得上的时候才注入。见 engine/rules.py
        self.rules_detail = str(self.options.get("rules_detail") or "lean").lower()
        self.rules = rules_mod.compose(self.rules_detail)
        # 术语触发式注入：整局每个词只解释一次，见 engine/glossary.py
        self.glossary = glossary_mod.GlossaryInjector()
        self.session_id = session_id
        # 骰子内核由回合管理器注入；AI 只能通过它申请掷骰，碰不到随机源
        self.kernel: Any = None

        # ★ 跨周目**玩家层**记忆。
        #   角色记忆（self.memory）每局清零；玩家记忆（self.card）跨模组、跨站位累积。
        #   今天当 KP 的汤圆，下一局可能去当调查员——但她的梗和旧账都还在。
        prof = seat.get("profile") or {}
        self.player_id = player_memory.safe_id(
            prof.get("player_id") or prof.get("player_name") or self.seat_id)
        self.card = player_memory.PlayerCard.load_or_create(
            self.player_id,
            display_name=prof.get("player_name") or self.display_name,
            table_voice=prof.get("table_voice", ""),
            playstyle=prof.get("playstyle", ""),
        )
        self.recalled_this_session: list[str] = []
        self.client: BaseClient = make_client(seat, seed=seed)
        self.messages: list[dict[str, str]] = []
        self.stats = SeatStats()
        self.last_output: ChannelOutput | None = None

    # -------------------------------------------------- 底层调用

    def _call(self, messages: list[dict[str, str]], *, phase: str = "play") -> LLMResult:
        res = self.client.chat(
            messages,
            temperature=self.seat.get("temperature", 0.85),
            max_tokens=self.seat.get("max_tokens", 1400),
            mock_phase=phase,
        )
        # 机械清洗。破折号、Markdown 加粗、桌边话的句尾句号，这几条没有歧义，
        # 代码一定修得对，交给模型重写既花钱又不保证。这里是所有模型输出的
        # 唯一收口点，PL、KP、车卡、研读、审卡全都过这一道。
        # 机器通道（state/roll/recall）一个字都不碰，那里面是指令和骰点。
        res.text = clean_output(res.text)
        self.stats.add(res)
        return res

    def _trim(self, keep_pairs: int) -> None:
        """只保留最近 N 轮逐字历史。远期事实已由 L2 编年史接管，丢得起。"""
        if keep_pairs <= 0 or len(self.messages) <= 1 + keep_pairs * 2:
            return
        head = self.messages[0]
        tail = self.messages[-(keep_pairs * 2):]
        self.messages = [head] + tail

    def token_report(self) -> dict[str, Any]:
        return {
            "seat_id": self.seat_id,
            "display_name": self.display_name,
            "kind": self.kind,
            "model": self.seat.get("model", ""),
            "provider": self.seat.get("provider", ""),
            "calls": self.stats.calls,
            "prompt_tokens": self.stats.prompt_tokens,
            "completion_tokens": self.stats.completion_tokens,
            "cached_tokens": self.stats.cached_tokens,
            "errors": self.stats.errors,
            "last_error": self.stats.last_error,
            "repairs": self.stats.repairs,
            "tool_passes": self.stats.tool_passes,
        }

    # -------------------------------------------------- 工具轮：掷骰 + 回忆

    def _parse(self, text: str) -> ChannelOutput:
        return parse_pl_output(text)

    def _roll_visibility(self) -> str:
        """自己的 <roll> 是明骰还是暗骰。玩家掷骰全桌看得见，守秘人默认暗骰。"""
        return "public"

    def kernel_do_roll(self, expr: str, purpose: str,
                       visibility: str) -> dict[str, Any]:
        """通过**引擎**掷一次骰。AI 永远碰不到随机源，只能申请。"""
        if getattr(self, "kernel", None) is None:
            return {}
        info = self.kernel.do_roll(self.display_name, expr, purpose, source=self.kind)
        info["visibility"] = visibility
        return info

    def _tool_pass(self, out: ChannelOutput, res: LLMResult,
                   phase: str = "play") -> ChannelOutput:
        """工具轮：**在同一次生成里**拿到骰子点数与回忆细节。

        为什么要两段：模型没法自己掷骰（引擎独占随机源），但它又必须在
        叙述里用上真实点数。所以让它先写一版「想掷什么的稿」，
        引擎立刻掷出来塞回去，它再照着实际点数把这一轮重说一遍——
        于是守秘人**不用等到下一轮**才知道自己掷了多少。

        几个取舍：
          · 只有它真的掷骰或回忆时才多花一次调用；平时零额外开销。
          · 不依赖服务商的 function calling 支持，任何 OpenAI 兼容端点都能用。
          · 掷骰与回忆**合并成同一次续写**，不各来一轮。
          · 续写后的那一版不许再喊工具，避免来回打转。
        """
        rolls = list(getattr(out, "rolls", []) or [])
        opens = [d for d in out.directives if d.kind == "openroll"]
        wants_recall = list(getattr(out, "recalls", []) or [])
        if wants_recall and not self.memory.tree:
            wants_recall = []
            out.notes.append("想回忆点什么，但脑子里还什么都没有。")
        if not rolls and not opens and not wants_recall:
            out.rolls = []
            out.recalls = []
            return out

        blocks: list[str] = []
        lines: list[str] = []

        if wants_recall:
            details = self.memory.recall(wants_recall)
            out.recall_results = details
            hits = [d["term"] for d in details if d.get("ok")]
            if hits:
                self.recalled_this_session.extend(hits)
            blocks.append(self.memory.render_recall(details))

        vis = self._roll_visibility()
        char = self.seat.get("character") or {}
        for expr, purpose in rolls:
            # 模型有时候把技能检定写成 <roll>1d100 | 侦查 39</roll>，
            # 绕过了引擎的判定，于是成功等级只能它自己猜，
            # 真实反馈里就把「83 对 39」猜成了大失败。
            # 只要认得出这是哪个技能，就按真检定走，等级由引擎算。
            skill = _skill_in_purpose(purpose, char)
            if skill and expr.strip().lower() in ("1d100", "d100", "d100<=", "1d100<=1d100"):
                res = self.kernel.do_check(
                    self.display_name, skill[0], skill[1], source="AI 自己发起")
                info = {"summary": res.text(), **res.to_dict()}
            else:
                info = self.kernel_do_roll(expr, purpose, vis)
            if info:
                out.roll_results.append(info)
                lines.append(info["summary"])

        for d in opens:
            expr = (d.arg(1) or "1d100").strip()
            purpose = (d.arg(2) or "").strip() or "守秘人公开掷骰"
            info = self.kernel_do_roll(expr, purpose, "public")
            if info:
                out.open_rolls.append(info)
                lines.append(info["summary"])

        # 已经掷掉了，就从指令里摘干净，别让引擎再掷一遍
        out.directives = [d for d in out.directives if d.kind != "openroll"]
        out.recalls = []
        out.rolls = []

        if lines:
            blocks.append("【你刚掷出来的】（**这就是最终结果，改不了**）\n"
                          + "\n".join(f"· {s}" for s in lines))
        if not blocks:
            return out

        self.messages.append({"role": "assistant", "content": res.text})
        self.messages.append({"role": "user",
                              "content": "\n\n".join(blocks) + "\n\n" + prompts.TOOL_CONTINUE})
        try:
            res2 = self._call(self.messages, phase=phase)
            out2 = self._parse(res2.text)
            out2.usage = res2
            if out2.act or out2.think or out2.ooc or out2.narr or out2.secret:
                out2.rolls = []              # 不许递归
                out2.recalls = []
                out2.recall_results = out.recall_results
                out2.roll_results = out.roll_results
                out2.open_rolls = out.open_rolls
                out2.tool_passes = 1
                out2.notes = list(out.notes)
                self.stats.tool_passes += 1
                return out2
            out.notes.append("拿到结果后没能重新组织出内容，按原样继续。")
        except LLMError as e:
            out.notes.append(f"续写失败（{e.message}），按原样继续。")
        finally:
            del self.messages[-2:]
        return out

    def _chat_once(self, system: str, user: str, phase: str) -> str:
        """一次性的桌边对话（不进正式历史）。散场茶话会用。"""
        try:
            res = self._call([{"role": "system", "content": system},
                              {"role": "user", "content": user}], phase=phase)
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return ""
        return (split_tags(res.text).get("ooc") or "").strip()

    def teatime(self, module_text: str, digest: str = "") -> dict[str, Any]:
        """散场茶话会：模组解禁之后，大家坐下来聊这局。"""
        ooc = self._chat_once(
            prompts.build_teatime_system(self.seat),
            prompts.build_teatime_user(module_text, digest), "teatime")
        return {"ok": bool(ooc), "ooc": ooc}

    def save_memory(self) -> None:
        self.memory.save(self.session_id)

    # -------------------------------------------------- 复盘（跨周目记忆的生长点）

    def retrospective(self, digest: str, module_title: str = "",
                      fate: str = "") -> dict[str, Any]:
        """一局结束，让 AI 以**玩家口吻**写下这一局留在自己身上的东西。

        没有这一步，跨周目记忆就永远长不出来——它是玩家卡唯一的增量来源。
        注意：**守秘人也要复盘**。今天当 KP 的人，明天可能就是玩家，
        他这一局对同桌人的观感同样该留下来。
        """
        prior = "；".join(self.recalled_this_session[:4])
        msgs = [
            {"role": "system",
             "content": prompts.build_retrospective_system(
                 self.seat, self.card.render_system_block())},
            {"role": "user",
             "content": prompts.build_retrospective_user(
                 digest, self.seat.get("character") or {}, prior)},
        ]
        try:
            res = self._call(msgs, phase="retro")
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return {"ok": False, "error": e.message}

        retro = player_memory.parse_retro_block(res.text)
        changes = self.card.absorb(
            retro, self.session_id, character=self.seat.get("character"),
            module_title=module_title, fate=fate,
        )
        if not any(c.session_id == self.session_id for c in self.card.characters):
            self.card.absorb({}, self.session_id,
                             character=self.seat.get("character"),
                             module_title=module_title, fate=fate)
        self.card.save()
        tags = split_tags(res.text)
        return {"ok": True, "changes": changes, "raw": res.text,
                "ooc": (tags.get("ooc") or "").strip(),
                "card": self.card.to_dict()}


# ---------------------------------------------------------------- PL Agent

class PLAgent(BaseAgent):
    kind = "PL"

    def __init__(self, seat: dict[str, Any], options: dict[str, Any],
                 session_id: str, seed: int | None = None,
                 setting: str = "") -> None:
        super().__init__(seat, options, session_id, seed=seed)   # 已含跨周目玩家卡
        self.memory = memory_mod.MemoryCard.for_pl(seat, session_id)
        self.memory.model_backend = seat.get("model", "")
        # 模组的时代背景。角色的用词跟着它走，玩家层（括号里）不受影响。
        self.setting = setting
        self._rebuild_system()

    def _rebuild_system(self) -> None:
        """重建 system 提示（身份恒定不变，但玩家层记忆会跨局增长）。"""
        self.messages = [{
            "role": "system",
            "content": prompts.build_pl_system(
                self.seat, self.rules, self.card.render_system_block(),
                setting=self.setting),
        }]

    # -------------------------------------------------- 车卡

    # -------------------------------------------------- 车卡桌上的讨论

    def _chargen_table_system(self, briefing: str) -> str:
        # 讨论阶段只要「车卡预算」这一节，不需要整张技能基础值表——省 token
        return prompts.build_chargen_table_system(
            self.seat, rules_mod.compose(self.rules_detail, ("chargen",)), briefing)

    def _chargen_table_call(self, briefing: str, user: str) -> dict[str, Any]:
        msgs = [{"role": "system", "content": self._chargen_table_system(briefing)},
                {"role": "user", "content": user}]
        try:
            res = self._call(msgs, phase="chargen_chat")
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return {"ok": False, "error": e.message, "pitch": "", "ooc": ""}
        tags = split_tags(res.text)
        pitch = (tags.get("pitch") or "").strip()
        ooc = (tags.get("ooc") or "").strip()

        # 车卡桌边以前整段不过哨兵：破折号、「你们呢」、摆依据都能摆到桌上。
        # 这里补上，命中就静默重写一次，还脏就这一句不说。
        rep = lint(ooc, channels=PL_CHANNELS)
        if rep.dirty:
            self.stats.repairs += 1
            try:
                res2 = self._call(
                    msgs + [
                        {"role": "assistant", "content": res.text},
                        {"role": "user", "content": prompts.build_repair_message(
                            rep.reason_text(), res.text)},
                    ],
                    phase="chargen_chat")
                ooc2 = (split_tags(res2.text).get("ooc") or "").strip()
                ooc = "" if lint(ooc2, channels=PL_CHANNELS).dirty else ooc2
            except LLMError:
                ooc = ""

        return {"ok": True, "pitch": pitch, "ooc": ooc, "raw": res.text}

    def chargen_pitch(self, briefing: str, attrs: dict[str, int],
                      method: str = "roll",
                      budget: dict[str, int] | None = None) -> dict[str, Any]:
        """第一轮：只说想玩什么，**不写卡**。顺便定年龄（会影响属性）。"""
        res = self._chargen_table_call(
            briefing, prompts.build_chargen_pitch_user(
                attrs, chargen.derive(attrs), method, budget))
        if res.get("ok"):
            res["age"] = parse_age(res.get("raw", ""))
            res["attrs"] = parse_attrs_block(res.get("raw", ""))
        return res

    def chargen_chat(self, briefing: str, chatter: list[str],
                     own: str = "") -> dict[str, Any]:
        """后面的商量轮：只接话，想改主意才再写一行 <pitch>。"""
        return self._chargen_table_call(
            briefing, prompts.build_chargen_chat_user(chatter, own))

    def chargen_defend(self, char: dict[str, Any],
                       issues: list[dict[str, str]]) -> dict[str, Any]:
        """被审卡质疑了，申辩一下。玩家都会尽力保住自己带的东西。"""
        try:
            res = self._call([
                {"role": "system", "content": prompts.build_pl_defend_system(self.seat)},
                {"role": "user", "content": prompts.build_pl_defend_user(char, issues)},
            ], phase="defend")
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return {"ok": False, "error": e.message, "ooc": "", "mem": ""}
        tags = split_tags(res.text)
        return {"ok": True, "ooc": (tags.get("ooc") or "").strip(),
                "mem": (tags.get("mem") or "").strip(), "raw": res.text}

    def run_chargen(self, attrs: dict[str, int], briefing: str = "",
                    chatter: list[str] | None = None) -> dict[str, Any]:
        """让 AI 按规则书**自己车一张卡**。骰子是引擎掷的，技能点预算由引擎校验。

        `briefing` 是守秘人给全桌的赛前简报（不含剧透）。
        它只是建议——PL 完全可以不理，各车各的本来也是跑团的乐趣。

        **技能点是唯一交给 AI 算的数字，所以这里卡得很死**（见 docs/车卡规范.md）：
          1. 车完先按规范逐条核
          2. 有违规就把它自己写的卡 + 违规清单一起还回去，重车一次
          3. 不管第二版写成什么样，引擎都按确定性算法裁一遍
        进场的卡一定是合法的。不是"警告一下但照样开跑"。
        """
        derived = chargen.derive(attrs)
        # 车卡时才补上「技能基础值表 + 技能点预算」——游玩阶段完全不注入
        chargen_rules = rules_mod.compose(self.rules_detail, ("chargen", "skills"))
        sys_msg = {"role": "system",
                   "content": prompts.build_chargen_system(self.seat, chargen_rules,
                                                           briefing)}
        usr_text = (prompts.build_chargen_finalize_user(attrs, derived, list(chatter))
                    if chatter else prompts.build_chargen_user(attrs, derived))

        def parse(text: str) -> tuple[dict[str, Any], str]:
            tags_ = split_tags(text)
            sheet_text = tags_.get("sheet") or ""
            if not sheet_text:
                m = re.search(r"<sheet>(.*?)(?:</sheet>|$)", text, re.S | re.I)
                sheet_text = m.group(1) if m else ""
            if not sheet_text.strip():
                return {}, (tags_.get("ooc") or "").strip()
            try:
                loaded = yaml.safe_load(sheet_text)
            except Exception:  # noqa: BLE001
                return {}, (tags_.get("ooc") or "").strip()
            return (loaded if isinstance(loaded, dict) else {}), \
                (tags_.get("ooc") or "").strip()

        try:
            res = self._call([sys_msg, {"role": "user", "content": usr_text}],
                             phase="chargen")
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return {"ok": False, "error": e.message}

        text = res.text
        sheet, ooc = parse(text)
        if not sheet.get("skills"):
            return {"ok": False, "error": "车卡输出里没有技能表。", "raw": text[:800]}

        # ---- 第一步：按规范核 ----
        audit = chargen.audit_sheet(sheet, attrs, sheet.get("occupation"))
        violations = list(audit["violations"])

        # ---- 第二步：有问题就回炉一次（只给一次，省钱也防死循环）----
        if violations and int(self.options.get("chargen_retry", 1)) > 0:
            self.stats.repairs += 1
            retry_user = prompts.build_chargen_retry_user(
                attrs, derived, str(sheet.get("occupation") or ""), text,
                violations, audit["budget"])
            try:
                res2 = self._call([
                    sys_msg,
                    {"role": "user", "content": usr_text},
                    {"role": "assistant", "content": text.strip()},
                    {"role": "user", "content": retry_user},
                ], phase="chargen")
                sheet2, ooc2 = parse(res2.text)
                if sheet2.get("skills"):
                    audit2 = chargen.audit_sheet(sheet2, attrs,
                                                 sheet2.get("occupation"))
                    # 只有"确实变干净了"才采纳，否则保留原稿（免得越改越远）
                    if len(audit2["violations"]) < len(violations):
                        sheet, text, ooc = sheet2, res2.text, ooc2 or ooc
                        audit, violations = audit2, list(audit2["violations"])
                        res = res2
            except LLMError as e:
                self.stats.errors += 1
                self.stats.last_error = e.message

        # ---- 第三步：引擎按意向缩放。进场的卡必须合法 ----
        fixed_sheet, repairs, plan = chargen.allocate(sheet, attrs,
                                                      sheet.get("occupation"))
        character = chargen.build_character_from_sheet(attrs, fixed_sheet)
        after = chargen.audit_sheet(fixed_sheet, attrs, fixed_sheet.get("occupation"))
        # ★ warnings 只放**修完之后还成立的事**（比如"还剩 12 点没用"）。
        #   修复前的违规走 violations 单独一个字段，界面不许拿它当红字报警。
        #   踩过的坑：卡明明是合法的（388/388），座位卡上却飘着"技能点超支"，
        #   用户以为引擎没管。原因就是这里把修之前的清单塞进了 warnings。
        return {"ok": True, "sheet": fixed_sheet, "character": character,
                "warnings": list(after["notes"]),
                "violations": violations, "repairs": repairs,
                "spent": after["spent"], "budget": after["budget"],
                "left_occ": plan.get("left_occ"),
                "left_interest": plan.get("left_interest"),
                "ooc": ooc, "raw": text,
                "attrs": attrs, "derived": derived,
                "usage": res}

    def adopt_character(self, character: dict[str, Any],
                        profile: dict[str, Any] | None = None) -> None:
        """把车好的卡落到座位与记忆上，并重建 system 提示词。"""
        self.seat["character"] = character
        if profile:
            self.seat["profile"] = {**(self.seat.get("profile") or {}), **profile}
        self.memory.character_sheet = dict(character)
        self.memory.situation["inventory"] = list(character.get("inventory") or [])
        self.memory.situation["conditions"] = list(character.get("conditions") or [])
        # 车出角色之后，桌上叫的就是**角色的名字**了。
        # 名字是 AI 自己取的，不是引擎预置的（见 config.make_pl_seat）。
        nm = str(character.get("name") or "").strip()
        if nm:
            self.display_name = nm
            self.seat["display_name"] = nm
        self._rebuild_system()

    @property
    def player_handle(self) -> str:
        """桌面上怎么称呼**这个玩家本人**（网名简称）。"""
        prof = self.seat.get("profile") or {}
        return str(prof.get("player_name") or self.seat_id)

    def table_label(self) -> str:
        """给界面看的称呼。

        车卡阶段（还没有角色）：就是网名——因为这会儿坐在这儿的确实是玩家本人。
        车出角色之后：`角色名（网名）`——既认得出是谁在演，也认得出演的是谁。
        """
        nm = str((self.seat.get("character") or {}).get("name") or "").strip()
        handle = self.player_handle
        if not nm:
            return handle
        return f"{nm}（{handle}）" if handle and handle != nm else nm

    # -------------------------------------------------- 入戏锚定

    def anchor(self, opening: str, time_block: str = "") -> ChannelOutput | None:
        """开局前让 AI 用自己的正式通道写一小段，作为它的第一条 assistant 历史。

        这一条历史同时干三件事：定妆、给模型自己的合规输出当 few-shot、
        让后续所有轮次都落在"续写"而不是"回答"上。
        """
        pc = (self.seat.get("character") or {}).get("name", "你的调查员")
        trial = list(self.messages) + [
            {"role": "user",
             "content": prompts.build_anchoring_user(
                 opening, pc, time_block,
                 inner_voice=bool(self.options.get("deepseek_inner_voice", True)))}
        ]
        try:
            res = self._call(trial, phase="anchor")
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return None

        out = parse_pl_output(res.text)
        out.usage = res
        content = out.raw.strip()
        # 只把模型自己的输出放进历史，锚定用的那句"指令"丢弃——
        # 历史里不该留下任何祈使句。
        self.messages.append({"role": "assistant", "content": content})
        self.last_output = out

        if out.act:
            self.memory.situation["current_objective"] = (
                self.memory.situation.get("current_objective") or "")
        return out

    # -------------------------------------------------- 一个回合

    def act(self, *, narr: str, scene_id: str = "", others: list[dict[str, Any]] | None = None,
            dice_lines: list[str] | None = None, engine_notes: list[str] | None = None,
            extra: str = "", unknown: list[str] | None = None,
            time_block: str = "", phrasing_note: str = "") -> ChannelOutput:
        mem_block = self.memory.render_for_prompt(
            scene_id=scene_id, query=narr,
            topk=int(self.options.get("memory_topk", 10)),
        )
        # 术语触发式注入：叙述里真的出现了这些词，才给一句话定义；
        # 同一个词整局只解释一次。比整节重发规则便宜一个数量级。
        gl = self.glossary.render(narr, extra)
        if gl:
            mem_block += "\n\n" + gl
        if extra.strip():
            mem_block += "\n\n" + extra.strip()

        # 跨周目：跟当前情境对得上的老梗、在场的旧相识。
        # 用触发式而非全量注入——整本"玩家回忆录"每轮重发太贵，也没必要。
        recalled = self.card.recall(
            "\n".join([narr, extra]),
            others=[o.get("display_name", "") for o in (others or [])],
            limit=2,
        )
        if recalled:
            self.recalled_this_session.extend(recalled)
            mem_block += ("\n\n[你自己想起的旧事]\n"
                          + "\n".join(f"- {r}" for r in recalled))

        world = prompts.build_world_message(
            narr=narr, memory_block=mem_block, others=others, dice_lines=dice_lines,
            unknown=unknown, time_block=time_block, phrasing_note=phrasing_note,
        )
        self.messages.append({"role": "user", "content": world})

        res = self._call(self.messages, phase="play")
        out = parse_pl_output(res.text)
        out.usage = res

        # 动作写太长就是在写小说。玩家是打字跑团，一段动作几十个字就够。
        if out.lint is not None:
            out.lint.hits.extend(too_long(out.act, ACT_SOFT_LIMIT))

        # 元层哨兵：命中就做一次静默重写。重写仍然失败则保留清洗后的文本。
        max_retry = int(self.options.get("linter_retry", 1))
        if out.lint and out.lint.dirty and max_retry > 0:
            self.stats.repairs += 1
            self.messages.append({"role": "assistant", "content": res.text})
            self.messages.append({
                "role": "user",
                "content": prompts.build_repair_message(out.lint.reason_text(), res.text),
            })
            try:
                res2 = self._call(self.messages, phase="play")
                out2 = parse_pl_output(res2.text)
                out2.usage = res2
                out2.repaired = True
                if out2.act or out2.think:
                    out = out2
                else:
                    out.notes.append("重写后仍不合格式，已按清洗结果保留。")
            except LLMError:
                out.notes.append("重写调用失败，已按清洗结果保留。")
            finally:
                # 无论重写成功与否，都不把那两条补救消息留在正式历史里
                self.messages = self.messages[:-2]

        if out.lint and out.lint.dirty:
            out.notes.append("元层哨兵命中：" + out.lint.reason_text())

        # ★ 工具轮：模型先说想掷什么骰子 / 想回忆什么，引擎立刻给出结果，
        #   它再带着真实点数把这一轮重说一遍——所以不用等下一轮。
        out = self._tool_pass(out, res, phase="play")

        final_text = out.raw.strip()
        self.messages.append({"role": "assistant", "content": final_text})
        self._trim(int(self.options.get("history_keep", 12)))

        # 记忆回写
        delta = memory_mod.parse_mem_block(out.mem)
        if delta:
            changes = self.memory.apply_delta(delta, turn=0, scene=scene_id)
            out.notes.extend(changes)
        if engine_notes:
            out.notes.extend(engine_notes)

        self.last_output = out
        return out

    def note_engine_event(self, text: str, kind: str = "event", scene: str = "") -> None:
        """引擎事件（拿到 handout、受伤、场景切换）直接写入 L2 并进历史。"""
        self.memory.add(kind, text, scene=scene)

    # -------------------------------------------------- 桌边插话

    def table_talk(self, talk_lines: list[str], pc_state: str = "",
                   addressed_by: str = "") -> str:
        """只说话、不动手的一轮。产出纯 <ooc> 桌边对话。"""
        msgs = [
            {"role": "system", "content": prompts.build_table_talk_system(self.seat)},
            {"role": "user", "content": prompts.build_table_talk_user(
                talk_lines, pc_state, addressed_by)},
        ]
        try:
            res = self._call(msgs, phase="table")
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return ""
        text = (split_tags(res.text).get("ooc") or "").strip()
        # 哨兵：桌边话也不该冒出助手腔
        if lint(text).dirty:
            self.stats.repairs += 1
            return ""
        return text

    def append_table_talk(self, reply: str) -> None:
        """把插话补进本轮自己的记录里，下一轮才记得自己说过。

        追加到已有的 assistant 消息里，而不是新开一条——
        连着两条 assistant 对部分接口不友好，而且语义上本来就是同一轮。
        """
        reply = (reply or "").strip()
        if not reply or not self.messages:
            return
        for i in range(len(self.messages) - 1, -1, -1):
            m = self.messages[i]
            if m.get("role") != "assistant":
                continue
            content = m.get("content", "")
            if re.search(r"</ooc>", content, re.I):
                content = re.sub(r"</ooc>", reply + "\n</ooc>", content,
                                 count=1, flags=re.I)
            elif re.search(r"<ooc>", content, re.I):
                content = content.rstrip() + f"\n{reply}"
            else:
                content = content.rstrip() + f"\n<ooc>\n{reply}\n</ooc>"
            m["content"] = content
            return

    def save_memory(self) -> None:
        self.memory.save(self.session_id)


# ---------------------------------------------------------------- KP Agent

class KPAgent(BaseAgent):
    kind = "KP"

    def __init__(self, seat: dict[str, Any], options: dict[str, Any],
                 session_id: str, module_title: str = "", premise: str = "",
                 module_brief: str = "", seed: int | None = None,
                 setting: str = "") -> None:
        super().__init__(seat, options, session_id, seed=seed)
        # 守秘人要裁定后果，常驻多带战斗与理智两节
        self.rules = rules_mod.compose(
            "standard" if self.rules_detail == "lean" else self.rules_detail)
        self.memory = memory_mod.MemoryCard.for_kp(seat, session_id)
        self.memory.model_backend = seat.get("model", "")
        self.module_title = module_title
        self.premise = premise
        # 叙述的时代语域。模组写现代就说现代话，写未来就说未来的，不默认 1920 年代。
        self.setting = setting
        self.messages = [{
            "role": "system",
            "content": prompts.build_kp_system(
                seat, self.rules, module_title, premise, module_brief,
                player_block=self.card.render_system_block(max_memes=1),
                setting=self.setting),
        }]

    def _parse(self, text: str) -> ChannelOutput:
        return parse_kp_output(text)

    def _roll_visibility(self) -> str:
        """守秘人自己的骰子默认是**暗骰**：只有他和导演看得到。

        想要明骰（全桌都看见）就在 <state> 里写 openroll。
        """
        return "secret"

    def update_brief(self, module_brief: str) -> None:
        """车卡结束后，把全班调查员的资料补进 system 并重建提示词。

        时序：守秘人要在车卡**之前**先出简报，那时玩家还没有角色；
        等大家车完卡，再把「本桌有哪些调查员、他们的背景是什么」补进去。
        """
        self.messages = [{
            "role": "system",
            "content": prompts.build_kp_system(
                self.seat, self.rules, self.module_title, self.premise, module_brief,
                player_block=self.card.render_system_block(max_memes=1),
                setting=self.setting),
        }]

    def chargen_briefing(self, module_brief: str, secret_terms: list[str],
                         module_title: str, player_count: int,
                         extra_note: str = "") -> dict[str, Any]:
        """开局前给全桌写一份**不剧透**的简报。

        用独立的临时消息，不进正式历史——它属于车卡阶段，不属于跑团过程。
        """
        user = prompts.build_briefing_user(module_title, player_count)
        if extra_note.strip():
            user += "\n\n" + extra_note.strip()
        msgs = [
            {"role": "system",
             "content": prompts.build_briefing_system(self.seat, module_brief,
                                                      secret_terms)},
            {"role": "user", "content": user},
        ]
        try:
            res = self._call(msgs, phase="brief")
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return {"ok": False, "error": e.message, "brief": ""}

        tags = split_tags(res.text)
        brief = (tags.get("brief") or "").strip()
        if not brief:
            m = re.search(r"<brief>(.*?)(?:</brief>|$)", res.text, re.S | re.I)
            brief = (m.group(1) if m else res.text).strip()
        return {"ok": bool(brief), "brief": brief, "raw": res.text, "usage": res}

    def chargen_chat(self, briefing: str, chatter: list[str]) -> dict[str, Any]:
        """车卡讨论时守秘人也坐在桌边，可以插一句。"""
        msgs = [
            {"role": "system",
             "content": prompts.build_kp_chargen_chat_system(self.seat, briefing)},
            {"role": "user", "content": prompts.build_kp_chargen_chat_user(chatter)},
        ]
        try:
            ooc = chatter_with_sentinel(self, msgs, KP_CHANNELS)
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return {"ok": False, "error": e.message, "ooc": ""}
        return {"ok": True, "ooc": ooc}

    def study_module(self, module_brief: str, roster_block: str,
                     scene_ids: list[str], module_title: str,
                     player_count: int, extra_note: str = "") -> dict[str, Any]:
        """开局前的功课：读模组，写开场日 / 大纲 / 理解 / 扩展 / 彩蛋。"""
        user = prompts.build_module_study_user(module_title, player_count)
        if extra_note.strip():
            user += "\n\n" + extra_note.strip()
        msgs = [
            {"role": "system",
             "content": prompts.build_module_study_system(
                 self.seat, module_brief, roster_block, scene_ids)},
            {"role": "user", "content": user},
        ]
        try:
            res = self._call(msgs, phase="study")
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return {"ok": False, "error": e.message}

        out = {k: _tagged(res.text, k)
               for k in ("clock", "spine", "understanding", "expansion", "eggs")}
        out["ok"] = bool(out["understanding"] or out["spine"])
        out["spine_ids"] = parse_spine(out["spine"])
        out["raw"] = res.text
        return out

    def teatime(self, talk: list[str], digest: str = "",
                study: dict[str, Any] | None = None) -> dict[str, Any]:
        ooc = self._chat_once(
            prompts.build_kp_teatime_system(self.seat, study),
            prompts.build_kp_teatime_user(talk, study), "teatime_kp")
        return {"ok": bool(ooc), "ooc": ooc}

    def audit_sheets(self, setting: str, party_text: str,
                     module_title: str) -> dict[str, Any]:
        """审卡：看一遍所有人的卡，挑该问的问。"""
        try:
            res = self._call([
                {"role": "system", "content": prompts.build_kp_audit_system(
                    self.seat, setting, party_text)},
                {"role": "user", "content": prompts.build_kp_audit_user(module_title)},
            ], phase="audit")
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return {"ok": False, "error": e.message, "rows": [], "ooc": ""}
        return {"ok": True, "rows": parse_audit_rows(res.text, "audit"),
                "ooc": (split_tags(res.text).get("ooc") or "").strip(),
                "raw": res.text}

    def rule_sheets(self, setting: str, talk: list[str]) -> dict[str, Any]:
        """对玩家的申辩做裁决。"""
        try:
            res = self._call([
                {"role": "system", "content": prompts.build_kp_verdict_system(
                    self.seat, setting)},
                {"role": "user", "content": prompts.build_kp_verdict_user(talk)},
            ], phase="verdict")
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return {"ok": False, "error": e.message, "rows": [], "ooc": ""}
        return {"ok": True, "rows": parse_audit_rows(res.text, "verdict"),
                "ooc": (split_tags(res.text).get("ooc") or "").strip(),
                "raw": res.text}

    def study_report(self, module_brief: str, module_title: str) -> dict[str, Any]:
        """研读室：一上来先出一份**通读报告**（给导演看的，不用藏）。"""
        try:
            res = self._call([
                {"role": "system",
                 "content": prompts.build_kp_study_digest_system(self.seat, module_brief)},
                {"role": "user",
                 "content": prompts.build_kp_study_digest_user(module_title)},
            ], phase="study_room")
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return {"ok": False, "error": e.message, "report": "", "ooc": ""}
        tags = split_tags(res.text)
        report = _tagged(res.text, "report")
        return {"ok": bool(report), "report": report,
                "ooc": (tags.get("ooc") or "").strip(), "raw": res.text}

    def study_answer(self, module_brief: str, study: dict[str, Any] | None,
                     talk: list[dict[str, str]], question: str) -> dict[str, Any]:
        """研读室：回答导演关于这个模组的追问。"""
        try:
            res = self._call([
                {"role": "system",
                 "content": prompts.build_kp_study_chat_system(
                     self.seat, module_brief, study)},
                {"role": "user",
                 "content": prompts.build_kp_study_chat_user(talk, question)},
            ], phase="study_room")
        except LLMError as e:
            self.stats.errors += 1
            self.stats.last_error = e.message
            return {"ok": False, "error": e.message, "answer": ""}
        tags = split_tags(res.text)
        ans = (tags.get("ooc") or "").strip() or res.text.strip()
        return {"ok": True, "answer": ans, "raw": res.text}

    def opening(self, scene_text: str = "", scene_title: str = "",
                handouts: list[str] | None = None,
                time_block: str = "") -> ChannelOutput:
        """开场。模组情报已在 system 里（享受前缀缓存），这里只交代开场这一幕。"""
        parts = ["[现在开始]"]
        if time_block.strip():
            parts.append(time_block.strip())
        if scene_title:
            parts.append(f"开场场景：{scene_title}")
        if scene_text.strip():
            parts.append(f"[这一场景的原文]\n{scene_text.strip()}")
        if handouts:
            parts.append("[你手上可用的 handout 文件]\n" + "、".join(handouts)
                         + "\n（用 <state> 里的 grant_handout 指令发给玩家，"
                           "没发出去的玩家看不到。）")
        parts.append("写出开场叙述。")
        parts.append(prompts.KP_TURN_CUE)

        self.messages.append({"role": "user", "content": "\n\n".join(parts)})
        res = self._call(self.messages, phase="play")
        out = parse_kp_output(res.text)
        out.usage = res
        self.messages.append({"role": "assistant", "content": res.text.strip()})
        self.last_output = out
        return out

    def respond(self, *, actions: list[dict[str, Any]], dice_lines: list[str] | None = None,
                scene_text: str = "", engine_notes: list[str] | None = None,
                table_talk: list[str] | None = None,
                time_block: str = "", pc_names: list[str] | None = None) -> ChannelOutput:
        world = prompts.build_kp_world_message(
            actions=actions, dice_lines=dice_lines, scene_text=scene_text,
            engine_notes=engine_notes, table_talk=table_talk, time_block=time_block,
        )
        # 守秘人同样按术语触发补课——它要说"困难成功""临时性疯狂"这类词时更该用准
        blob = "\n".join([
            " ".join(str(a.get("act", "")) + str(a.get("ooc", "")) for a in actions),
            " ".join(dice_lines or []),
            " ".join(table_talk or []),
        ])
        gl = self.glossary.render(blob)
        if gl:
            world += "\n\n" + gl
        self.messages.append({"role": "user", "content": world})
        res = self._call(self.messages, phase="play")
        out = parse_kp_output(res.text)
        out.usage = res

        # 守秘人专用的两条硬拦。模型自己看不出这是越权，
        # 在它看来「他问了一句」只是顺手的转场，所以只能引擎查。
        if out.lint is not None:
            out.lint.hits.extend(kp_overreach(out.narr, pc_names or []))
            out.lint.hits.extend(narr_too_long(out.narr))

        # 语域哨兵：叙述里冒出「不是A而是B」、破折号、Markdown 加粗这类
        # 书面腔，就静默重写一次。守秘人的叙述是玩家整晚盯着的东西，
        # 这些毛病放在这里比放在 PL 那边更显眼。
        max_retry = int(self.options.get("linter_retry", 1))
        if out.lint and out.lint.dirty and max_retry > 0:
            self.stats.repairs += 1
            self.messages.append({"role": "assistant", "content": res.text})
            self.messages.append({
                "role": "user",
                "content": prompts.build_repair_message(out.lint.reason_text(), res.text),
            })
            try:
                res2 = self._call(self.messages, phase="play")
                out2 = parse_kp_output(res2.text)
                out2.usage = res2
                out2.repaired = True
                if out2.narr or out2.secret:
                    out = out2
                    res = res2
                else:
                    out.notes.append("重写后仍不合格式，已按清洗结果保留。")
            except LLMError:
                out.notes.append("重写调用失败，已按清洗结果保留。")
            finally:
                # 补救用的两条消息不进正式历史
                self.messages = self.messages[:-2]

        if out.lint and out.lint.dirty:
            out.notes.append("语域哨兵命中：" + out.lint.reason_text())

        # 守秘人的工具轮：掷骰与回忆都在**同一次生成**里拿到结果
        out = self._tool_pass(out, res, phase="play")
        self.messages.append({"role": "assistant", "content": (out.raw or "").strip()})
        self._trim(6)
        self.last_output = out
        return out

    def note_engine_event(self, text: str, kind: str = "event", scene: str = "") -> None:
        self.memory.add(kind, text, scene=scene, when=self.memory.now)

    def save_memory(self) -> None:
        self.memory.save(self.session_id)

    # -------------------------------------------------- 省钱：历史里的旧 secret

    def _trim(self, keep_pairs: int) -> None:
        super()._trim(keep_pairs)
        self._strip_old_secrets()

    def _strip_old_secrets(self) -> None:
        """`<secret>` 每轮都会**完整重写**，所以历史里只留最近一条就够。

        守秘人的 secret 通常三四百字，一轮一条，留 6 轮就是两千多字——
        这是守秘人侧最大的一笔无谓开销，抹掉它不影响任何行为。
        """
        idxs = [i for i, m in enumerate(self.messages) if m.get("role") == "assistant"]
        for i in idxs[:-1]:
            content = self.messages[i].get("content", "")
            if "<secret>" not in content:
                continue
            cleaned = re.sub(r"<secret>.*?(?:</secret>|$)", "",
                             content, flags=re.S | re.I).strip()
            self.messages[i]["content"] = cleaned or "（略）"
