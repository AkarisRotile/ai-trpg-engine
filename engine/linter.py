"""元层哨兵（Meta Linter）。

兜底机制：即使提示词全部就位，模型偶尔仍会滑回助手人格，
或者滑回它自己的写作习惯（八股、破折号、摆依据）。
这里做**输出后检测 + 一次静默重写**，再失败就裁剪并在 UI 标记。

分两类毛病：

- `META_PATTERNS`：元层痕迹，模型忘了自己在桌边，开始自称 AI、把 KP 当对话对象。
- `STYLE_PATTERNS`：语域毛病。人还是那个人，但说话方式不像桌上的人。
  这些不是凭喜好定的——真人聊天语料里，一万多条消息没有一条带句号收尾，
  没有一条用破折号，也没有谁把理由列成一二三条讲给你听。

注意：只用正向的"重写指令"去修，不用"不许……"去否定——
否定式指令会激活被否定的概念。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 元层痕迹

META_PATTERNS: list[tuple[str, str]] = [
    (r"作为\s*(一[个位名])?\s*(PL|pl|玩家|AI|人工智能|助手|语言模型)", "元层自述"),
    (r"扮演(着)?(一[个位名])?", "出现『扮演』字样"),
    (r"我是(一个)?\s*(AI|人工智能|语言模型|助手)", "自称 AI"),
    (r"as an ai", "自称 AI"),
    (r"请问\s*(KP|守秘人|GM)", "把 KP 当成对话对象"),
    (r"(希望|请)\s*(KP|守秘人)\s*(能|给|告诉|允许)", "把 KP 当成对话对象"),
    (r"^\s*(好的|当然|没问题|明白|收到|了解)\s*[，,。!！]", "助手式开场"),
    (r"^\s*(好的|那么|现在)?\s*我(作为|以)", "元层自述"),
    (r"(接下来|下面|那么)\s*我(会|要|将|来)\s*(先|来)?", "元层预告"),
    (r"^\s*让我(来)?(先|先来|试着)?", "元层预告"),
    (r"(我的)?(行动|输出|回复|格式)(申请|如下|是|为)?\s*[:：]", "表格化元层"),
    (r"^\s*\[?(系统|system|ooc)\]?\s*[:：]", "元层"),
    (r"(需要我|要不要我|是否要我)\s*(继续|描述|输出)", "询问式元层"),
    (r"(等待|等待中)\s*(KP|守秘人|GM)\s*(的)?(回应|回复|判定)", "把 KP 当成对话对象"),
    (r"(作为|身为)\s*(一名|一个)?\s*(玩家|pl)", "元层自述"),
]

# ---------------------------------------------------------------- 语域毛病

# 每一条都对应用户明确点过名的毛病。加之前先问：
# 真人聊天里会不会出现这个？会，就别加。
STYLE_PATTERNS: list[tuple[str, str]] = [
    # 「不是 A，而是 B」全家。靠否定一个没人说过的靶子制造洞见，
    # 是社论和广告的口气。
    (r"不是[^，。！？\n]{1,18}[，,]\s*(而是|是)[^，。！？\n]{1,18}", "「不是A而是B」句式"),
    (r"并非[^，。！？\n]{1,18}[，,]\s*而是", "「并非A而是B」句式"),
    (r"与其说[^，。！？\n]{1,18}[，,]\s*不如说", "「与其说不如说」句式"),
    (r"不在于[^，。！？\n]{1,18}[，,]\s*而在于", "「不在于而在于」句式"),
    (r"这不是[^，。！？\n]{1,16}[，,]\s*这是", "「这不是A这是B」句式"),

    # 破折号。想停顿就断句，想转折就另起一句。
    # 两边挨着数字的不算，那是「88——58——90」这种区间写法。
    (r"(?<!\d)——(?!\d)|(?<!\d)—(?!\d)|(?<!\d)--(?!\d)", "破折号"),

    # Markdown 加粗当强调。真人打字不会加粗。
    (r"\*\*[^*\n]{1,40}\*\*", "Markdown 加粗"),

    # 撇清式表态。自嘲可以（「我废了」是好句子），
    # 撇清的差别在于人从场上退出去了。
    (r"别指望我|我不去|别催|谁挨打谁上|别找我|你们自己看着办", "撇清式表态"),

    # 摆事实讲依据。理由留在心里，没人边跑团边论证自己。
    (r"(理由|依据|原因是|原因是)[是为]?\s*[:：]", "摆依据"),
    (r"(第一|其一)[，,、]\s*[^，。\n]{2,}[，,]\s*(第二|其二)", "一二三条列依据"),
]

# 助手腔开场白，直接剥离
STRIP_PREFIX = re.compile(
    r"^\s*(好的|当然|没问题|明白|收到|了解|那么|嗯)[，,。！!：:\s]*", re.M
)
FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")

# 认得出来的通道。以前只认 PL 的四个标签，KP 的 <narr>/<secret>/<state>
# 全掉进 free 里，等于拿 PL 的规矩去量守秘人的叙述。
_TAGS = "think|act|ooc|mem|narr|secret|state|roll|recall"
_TAG_RE = re.compile(rf"<({_TAGS})>(.*?)</\1>", re.S | re.I)

# 机器通道：里面是指令或骰子结果，不该按人话的标准去挑毛病
MACHINE_CHANNELS = frozenset({"state", "roll", "recall"})

# 各角色的默认检查通道。
# PL 不扫 <think>：内心独白本来就允许「他是不是在骗我」这类推理口吻，
# 扫了会把它误判成元层痕迹，逼模型改写自己的心理活动。这是有意留的口子。
PL_CHANNELS = ("act", "ooc", "free")
KP_CHANNELS = ("narr", "ooc", "free")


@dataclass
class LintHit:
    reason: str
    snippet: str
    channel: str
    kind: str = "meta"          # "meta"（忘了自己在桌边）或 "style"（语域跑偏）


@dataclass
class LintReport:
    cleaned: str
    hits: list[LintHit] = field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return bool(self.hits)

    @property
    def meta_hits(self) -> list[LintHit]:
        return [h for h in self.hits if h.kind == "meta"]

    @property
    def style_hits(self) -> list[LintHit]:
        return [h for h in self.hits if h.kind == "style"]

    def reason_text(self) -> str:
        return "；".join(sorted({h.reason for h in self.hits}))


def _iter_channels(text: str):
    """产出 (通道名, 通道内容)。未包裹在标签里的文字归入 'free'。"""
    pos = 0
    for m in _TAG_RE.finditer(text):
        if m.start() > pos:
            yield "free", text[pos:m.start()]
        yield m.group(1).lower(), m.group(2)
        pos = m.end()
    if pos < len(text):
        yield "free", text[pos:]


def lint(text: str, channels: tuple[str, ...] = PL_CHANNELS) -> LintReport:
    """扫描输出里的元层痕迹与语域毛病。

    默认按 PL 的通道扫（<act>/<ooc> 与标签外文字）。
    守秘人那边传 `KP_CHANNELS`，让 <narr> 也进检查范围。

    <state>/<roll>/<recall> 是机器通道，永远跳过——
    里面写着 `advance 3h` 或者骰子点数，拿人话的标准去挑毛病没有意义。
    <think> 也不扫：内心独白允许推理口吻。
    """
    allowed = tuple(c for c in channels if c not in MACHINE_CHANNELS)
    hits: list[LintHit] = []
    for ch, body in _iter_channels(text):
        if ch not in allowed:
            continue
        for pattern, reason in META_PATTERNS:
            for m in re.finditer(pattern, body, re.I | re.M):
                snippet = body[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")
                hits.append(LintHit(reason=reason, snippet=snippet.strip(),
                                    channel=ch, kind="meta"))
        for pattern, reason in STYLE_PATTERNS:
            for m in re.finditer(pattern, body, re.M):
                snippet = body[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")
                hits.append(LintHit(reason=reason, snippet=snippet.strip(),
                                    channel=ch, kind="style"))
    cleaned = STRIP_PREFIX.sub("", text)
    cleaned = FENCE.sub("", cleaned).strip()
    return LintReport(cleaned=cleaned, hits=hits)


def build_repair_instruction(hits: list[LintHit]) -> str:
    """给模型的重写指令。

    只描述目标语域，不重复被禁的概念本身——把「不许写破折号」再说一遍，
    等于又把破折号放回它眼前。
    """
    lines = ["刚才那段记录里混进了台面外的话，或者说话的调子不对。", "重写一遍，只保留这些："]
    if any(h.kind == "meta" for h in hits):
        lines.append("- <act>/<narr>：以你的身份、第一人称、现在时，写你尝试做什么、说出口什么。")
        lines.append("- <ooc>：你在桌边说的话，用括号括起来，短、碎。")
        lines.append("- <think>：你心里的念头，用你自己的口吻。")
    if any(h.kind == "style" for h in hits):
        lines.append("- 短句，像人打字，不要书面腔。")
        lines.append("- 想停顿就把句子断掉，转折另起一句。")
        lines.append("- 别解释自己的道理，理由留在心里。")
        lines.append("- 语气自然一点，跟朋友说话那样。")
    lines.append("直接从第一个标签开始。")
    return "\n".join(lines)
