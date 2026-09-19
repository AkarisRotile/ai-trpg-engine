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

    # 把话头推给全桌。一晚上问两次就够烦了。
    # 只认这几个很死的写法，「谁要」这种太常见的不碰，免得误伤。
    (r"你们呢|你们谁要|你们怎么看|大家怎么看|各位怎么看|有没有人要", "把话头推给全桌"),

    # 前置声明。想说什么直接说，不用先报一句「我要说什么」。
    (r"(先说好|提前交底|丑话说在前|提醒一句|提醒一下|说句实话|我明说)[，,：:]",
     "前置声明"),

    # 比喻。打字跑团没人为了一个比喻停下来想半天。
    # 只认书面味很重的这几个，「好像」「感觉像」这种日常说法不碰。
    (r"仿佛|宛如|好似|恍若|犹如|宛若", "比喻"),
]

# 数字后面跟着单位的是正常说事（9月18日、158的身高、3年），不算报账。
_NUM_TOKEN = re.compile(r"\d+(?!\s*[年月日号钟岁个分秒元块斤米尺页条只人台%])")
# 区间写法要放过：88——58——90、1925—1930、100--200
_RANGE_MARK = ("—", "–", "--")


def _check_number_pile(body: str, need: int = 3, gap: int = 8) -> str:
    """三个以上数字挤在一小段里，就是报账单。

    这条本来写成一条正则，结果在「1925—1930」上只匹配到 1925，
    `{2,}` 根本没生效，误伤了一片。正则塞太多逻辑没法查，
    改成显式函数，一眼看得出在干什么，也好测。
    """
    t = body or ""
    hits = [m.start() for m in _NUM_TOKEN.finditer(t)]
    if len(hits) < need:
        return ""
    for i in range(len(hits) - need + 1):
        first, last = hits[i], hits[i + need - 1]
        if (last - first) > gap * (need - 1) + 6:
            continue
        seg = t[first:last]
        if any(mark in seg for mark in _RANGE_MARK):
            continue
        return "数字报账单"
    return ""


# 有些毛病一条正则写不清楚，写成函数更好懂也更好测。
EXTRA_CHECKS = (_check_number_pile,)

# 动作和叙述的长度上限。超了就是在写小说，不是在跑团。
ACT_SOFT_LIMIT = 260
NARR_SOFT_LIMIT = 380

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

# 说话的动词。用来抓「守秘人替调查员开口」。
_SAY_VERB = r"(?:说|问|答|道|喊|笑|开口|嘟囔|嘀咕|接话|应了|回他)"

# 守秘人一轮叙述的长度上限。超了就是在念小说，不是在跑团。
NARR_SOFT_LIMIT = 380

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
        for check in EXTRA_CHECKS:
            reason = check(body)
            if reason:
                hits.append(LintHit(reason=reason, snippet=body[:40].replace("\n", " "),
                                    channel=ch, kind="style"))
    cleaned = STRIP_PREFIX.sub("", text)
    cleaned = FENCE.sub("", cleaned).strip()
    return LintReport(cleaned=cleaned, hits=hits)


# ---------------------------------------------------------------- 机械清洗

# 这几条没有歧义，代码修得比模型稳，所以不重写，直接改。
_DASH_FIX = re.compile(r"(?<!\d)——(?!\d)|(?<!\d)—(?!\d)|(?<!\d)--(?!\d)")
_MD_BOLD_FIX = re.compile(r"\*\*([^*\n]{1,60})\*\*")
# 桌边的话不拿句号收尾。真人打字不带句号。
_TAIL_PERIOD = re.compile(r"[。\.]+\s*$")


def soft_clean(text: str, *, chat: bool = False) -> str:
    """把能机械修的毛病当场修掉，不指望模型重写。

    为什么要这么做：破折号和 Markdown 加粗没有歧义，代码一定能修对，
    而让模型重写一次既花钱又不保证修好。用户的原话是「现实说话
    不可能有人说得出破折号这种东西」，那就别给它露脸的机会。

    `chat=True` 用于桌边的话，额外去掉句尾的句号。
    """
    t = text or ""
    # 破折号前面已经有逗号的时候，别换成两个逗号
    t = re.sub(r"[，,]\s*(?<!\d)——(?!\d)", "，", t)
    t = _DASH_FIX.sub("，", t)
    t = _MD_BOLD_FIX.sub(r"\1", t)
    t = drop_antithesis(t)
    t = re.sub(r"。{2,}", "。", t)
    if chat:
        # 真人打字不带句号：语料里三万条消息，带句号的只占 0.6%。
        # 模型拿句号断句的地方，在真人那儿就是另起一条消息。
        # 所以这里不是删掉，是断开。
        t = t.replace("。", "\n")
        t = re.sub(r"\n{2,}", "\n", t)
        # 收尾括号别被挤到自己一行
        t = re.sub(r"\n+\s*([）)])", r"\1", t)
        t = _TAIL_PERIOD.sub("", t.rstrip())
    return t


def clean_output(text: str) -> str:
    """按通道清洗模型输出。

    只动 think/act/ooc/mem/narr/secret 这些「人话」通道，
    state/roll/recall 一个字都不碰，里面是指令和骰点，
    改坏了比不改更糟。
    """
    def repl(m: re.Match) -> str:
        tag = m.group(1)
        if tag.lower() in MACHINE_CHANNELS:
            return m.group(0)
        body = soft_clean(m.group(2), chat=tag.lower() == "ooc")
        return f"<{tag}>{body}</{tag}>"

    return _TAG_RE.sub(repl, text or "")


def kp_overreach(narr: str, pc_names: list[str]) -> list[LintHit]:
    """守秘人有没有替玩家说话。

    最硬的一个迹象：把引号里的话安在某个调查员头上。
    这是越权里最严重的一种，玩家没写过的台词一个字都不该有。

    只能靠引擎查：在模型自己看来，「他问了一句」是再自然不过的转场，
    它不觉得自己在替人做决定。
    """
    hits: list[LintHit] = []
    body = narr or ""
    for name in pc_names or []:
        name = (name or "").strip()
        if len(name) < 2:
            continue
        esc = re.escape(name)
        pat = re.compile(
            rf"[\u300c\u201c\"][^\u300c\u300d\u201c\u201d\n]{{0,40}}[\u300d\u201d\"]\s*{esc}\s*{_SAY_VERB}"
            rf"|{esc}\s*{_SAY_VERB}[：:，,]?\s*[\u300c\u201c\"]")
        for m in pat.finditer(body):
            snip = body[max(0, m.start() - 12):m.end() + 12].replace("\n", " ")
            hits.append(LintHit(reason=f"守秘人替{name}说话", snippet=snip.strip(),
                                channel="narr", kind="overreach"))
    return hits


def too_long(text: str, limit: int, *, channel: str = "act",
             what: str = "动作", kind: str = "style") -> list[LintHit]:
    """一段话是不是太长了。超了就是在写小说，不是在跑团。"""
    body = (text or "").strip()
    if len(body) <= limit:
        return []
    return [LintHit(reason=f"{what}太长（{len(body)} 字，上限 {limit}）",
                    snippet=body[:40], channel=channel, kind=kind)]


def narr_too_long(narr: str, limit: int = NARR_SOFT_LIMIT) -> list[LintHit]:
    """守秘人的叙述太长了。一轮写几百字，玩家会跳着看，埋的线索全白费。"""
    return too_long(narr, limit, channel="narr", what="叙述", kind="overreach")


def drop_antithesis(text: str) -> str:
    """把「不是A，而是B」这类句式机械地削成 B。

    这一家子用户点名要杜绝。既然重写一次它还可能再犯，
    就由代码兜底：留着后半句，把前面那个否定靶子删掉。
    后半句本身是完整的，删完读起来仍然通顺。
    """
    t = text or ""
    t = re.sub(r"(?:这)?(?:不是|并非)[^，。！？\n]{1,18}[，,]\s*(?:而是|是|这是)\s*", "", t)
    t = re.sub(r"与其说[^，。！？\n]{1,18}[，,]\s*不如说\s*", "", t)
    t = re.sub(r"(?:问题)?不在于[^，。！？\n]{1,18}[，,]\s*而在于\s*", "", t)
    return t


def build_repair_instruction(hits: list[LintHit]) -> str:
    """给模型的重写指令。

    只描述目标语域，不重复被禁的概念本身。把「不许写破折号」再说一遍，
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
    if any(h.kind == "overreach" for h in hits):
        lines.append("- <narr> 只写世界这一侧发生了什么。")
        lines.append("- 调查员的动作、对白、念头，他们没写过的就不存在，一个字都不要替他们写。")
        lines.append("- 别复述他们刚做过的事。")
        lines.append("- 短。一轮一百到两百字就够，写到三百就该停。")
    lines.append("直接从第一个标签开始。")
    return "\n".join(lines)
