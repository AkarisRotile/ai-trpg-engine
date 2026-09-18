"""元层哨兵（Meta Linter）。

兜底机制：即使对策 1-5 全部就位，模型偶尔仍会滑回助手人格。
这里做**输出后检测 + 一次静默重写**，再失败就裁剪并在 UI 标记。

注意：只用正向的"重写指令"去修，不用"不许……"去否定——
否定式指令会激活被否定的概念。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# (正则, 人类可读的原因)
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

# 助手腔开场白，直接剥离
STRIP_PREFIX = re.compile(
    r"^\s*(好的|当然|没问题|明白|收到|了解|那么|嗯)[，,。！!：:\s]*", re.M
)
FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


@dataclass
class LintHit:
    reason: str
    snippet: str
    channel: str


@dataclass
class LintReport:
    cleaned: str
    hits: list[LintHit] = field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return bool(self.hits)

    def reason_text(self) -> str:
        return "；".join(sorted({h.reason for h in self.hits}))


def _iter_channels(text: str):
    """产出 (通道名, 通道内容)。未包裹在标签里的文字归入 'free'。"""
    tag_re = re.compile(r"<(think|act|ooc|mem)>(.*?)</\1>", re.S | re.I)
    pos = 0
    for m in tag_re.finditer(text):
        if m.start() > pos:
            yield "free", text[pos:m.start()]
        yield m.group(1).lower(), m.group(2)
        pos = m.end()
    if pos < len(text):
        yield "free", text[pos:]


def lint(text: str, channels: tuple[str, ...] = ("act", "ooc", "free")) -> LintReport:
    """扫描输出中的元层痕迹。默认只扫 <act>/<ooc> 与标签外文字。

    <think> 是角色的内心，允许出现"他是不是在骗我"这类推理口吻，故默认不扫。
    """
    hits: list[LintHit] = []
    for ch, body in _iter_channels(text):
        if ch not in channels:
            continue
        for pattern, reason in META_PATTERNS:
            for m in re.finditer(pattern, body, re.I | re.M):
                snippet = body[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")
                hits.append(LintHit(reason=reason, snippet=snippet.strip(), channel=ch))
    cleaned = STRIP_PREFIX.sub("", text)
    cleaned = FENCE.sub("", cleaned).strip()
    return LintReport(cleaned=cleaned, hits=hits)


def build_repair_instruction(hits: list[LintHit]) -> str:
    """给模型的重写指令——只描述目标语域，不重复被禁的概念本身。"""
    lines = ["刚才那段记录里混进了台面外的话。", "重写一遍，只保留这些："]
    lines.append("- <act>：以你的身份、第一人称、现在时，写你尝试做什么、说出口什么。")
    lines.append("- <ooc>：只有在需要动用能力或装备时才写，电报体，例如：检定申请 聆听")
    lines.append("- <think>：你心里的念头，用你自己的口吻。")
    lines.append("- <mem>：需要记住的新事实，没有就留空。")
    lines.append("直接从 <think> 开始。")
    return "\n".join(lines)
