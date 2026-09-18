"""COC 第七版规则知识 —— **分层按需注入**，不做无脑全量塞。

成本常识：system 提示是**每个座位、每一轮**都要重发一遍的。
一份 1500 字的速查表，乘上「5 个座位 × 40 轮」就是 30 万字符的纯开销。
所以规则按"什么时候真的用得上"分三层：

  Tier 0  RULES_CORE      常驻（约 250 字）——判定核心，PL 每轮做决定真正需要的
  Tier 1  分节按需          战斗 / 理智 / 恢复 / 车卡，只在相关时刻注入
  Tier 2  retrieve_rules()  规则书 PDF 全文检索，问到才查

车卡用的技能基础值表、技能点预算这些，**只进车卡那一次调用**，
游玩阶段完全不注入——因为每个 PL 每轮都会拿到自己的技能表，
它不需要知道"侦查的基础值是 25"。
"""

from __future__ import annotations

from . import config as cfgmod

# ══════════════════════════════════════════════ Tier 0 · 常驻核心

RULES_CORE = """\
# 判定要点（其余术语按需给出，不用背）
- 掷 d100 与技能值比，**点数越小越好**。
- 成功等级永远按**原始技能值**算，不因难度改变：
  掷出 1 → 大成功；≤技能÷5 → 极难成功；≤技能÷2 → 困难成功；
  ≤技能 → 常规成功；掷出 100（技能<50 时 96-100）→ 大失败；其余失败。
- 难度只是达标线：困难要求至少困难成功，极难要求至少极难成功。
- 奖励骰取十位小、惩罚骰取十位大；对抗先比成功等级再比技能值。
- 幸运可以事后花，1 点换 1 点，用来刚好达标。"""

# ══════════════════════════════════════════════ Tier 1 · 按需分节

RULES_COMBAT = """\
# 战斗
- 先攻按 DEX 从高到低；一轮 = 移动 + 一次动作。
- 近战：格斗检定，目标可以选择闪避或反击。射击：射击检定，未瞄准有惩罚骰。
- 伤害 = 武器伤害骰 + 伤害加值，有护甲先扣护甲。
- 单次伤害 ≥ 最大 HP 的一半 → 重伤，需体质检定避免昏迷。
- HP 归零 → 濒死，每轮体质检定，失败即死亡。"""

RULES_SANITY = """\
# 理智
- 目睹超自然事物 → 理智检定（d100 对抗当前 SAN）。
- 损失表达式形如「0/1d4」「1/1d6+1」：成功损失前面的，失败损失后面的。
- 单次损失 ≥5 点 → 临时性疯狂，出现疯狂发作。
- 一天内累计损失 ≥ 当前 SAN 的五分之一 → 不定性疯狂。
- SAN 归零 → 永久疯狂，角色退场。"""

RULES_RECOVERY = """\
# 恢复
- 自然恢复：每 24 小时回 1 点 HP；重伤者需体质检定成功。
- 急救：可稳定濒死、回复 1 点 HP。医学：长期护理显著加快恢复。
- 魔法点：每小时恢复 1 点。"""

RULES_CHARGEN = """\
# 车卡（引擎会逐条校验，不合规的卡会被打回）
- 属性：STR/CON/DEX/APP/POW/LUCK = 3d6×5；SIZ/INT/EDU = (2d6+6)×5。
- 派生：HP=(CON+SIZ)÷10；MP=POW÷5；SAN=POW；MOV 通常 8。
- 伤害加值 DB 由 STR+SIZ 决定：≤64 为 -2；≤84 为 -1；≤124 为 0；
  ≤164 为 +1d4；≤204 为 +1d6；再往上每多 80 加 1d6。
- **两笔技能点分开记**：职业技能点（多数职业 = EDU×4）**只能**花在本职技能上；
  兴趣技能点 = INT×2，任何技能都能点。两笔不能互相挪用。
- **卡上写的是最终值**，不是投入的点数。花掉多少 = 最终值 − 该项基础值。
  母语的基础值 = EDU，闪避 = DEX÷2，克苏鲁神话 = 0，其余见技能基础值表。
- **单项不得超过 90**。车卡阶段没有例外。
- **技能不得低于它的基础值**（侦查基础 25，就不能写 20）。
- **克苏鲁神话必须是 0**，车卡阶段不分配。
- 信用评级是职业技能之一，**必须落在职业允许的区间内**
  （如医生 30–80、记者 9–30），点数从职业点里出。
- 点数不必花完，但绝不能超。"""

RULES_SKILL_BASE = """\
# 技能基础值（未列出的按 1%）
会计5 人类学1 估价5 考古学1 取悦15 攀爬20 计算机5 信用评级0 乔装5
闪避=DEX÷2 汽车驾驶20 电气维修10 电子学1 话术5 格斗25 射击20 急救30
历史5 恐吓15 跳跃20 母语=EDU 法律5 图书馆利用20 聆听20 锁匠1 机械维修10
医学1 博物学10 导航10 神秘学5 操作重型机械1 说服10 精神分析1 心理学10
骑术5 科学1 妙手10 侦查25 潜行20 生存10 游泳20 投掷20 追踪10"""

SECTION_MAP = {
    "combat": RULES_COMBAT,
    "sanity": RULES_SANITY,
    "recovery": RULES_RECOVERY,
    "chargen": RULES_CHARGEN,
    "skills": RULES_SKILL_BASE,
}

# 靠关键词自动判断这一轮该不该补注入某个分节
AUTO_TRIGGERS = {
    "combat": ("攻击", "开枪", "射击", "格斗", "挥拳", "砍", "先攻", "开打", "战斗", "中弹"),
    "sanity": ("理智", "SAN", "疯狂", "发疯", "恐惧", "不可名状", "克苏鲁", "直视"),
    "recovery": ("治疗", "急救", "包扎", "休息", "养伤", "恢复"),
}


# ══════════════════════════════════════════════ 组装

def compose(detail: str = "lean", sections: tuple[str, ...] | list[str] = (),
            query: str = "") -> str:
    """按详细度与场景组装规则文本。

    detail:
      lean     —— 只有判定核心（默认，最省）
      standard —— 核心 + 战斗 + 理智
      full     —— 再加恢复与用户自备规则
    sections: 强制追加的分节名
    query:    给了就按关键词自动追加相关分节
    """
    detail = (detail or "lean").lower()
    chosen: list[str] = [RULES_CORE]
    picked: set[str] = set()

    if detail == "standard":
        picked.update(("combat", "sanity"))
    elif detail == "full":
        picked.update(("combat", "sanity", "recovery"))

    for name in sections:
        if name in SECTION_MAP:
            picked.add(name)

    if query:
        for name, keys in AUTO_TRIGGERS.items():
            if any(k in query for k in keys):
                picked.add(name)

    for name in ("combat", "sanity", "recovery", "chargen", "skills"):
        if name in picked:
            chosen.append(SECTION_MAP[name])

    if detail == "full":
        extra = _user_rules()
        if extra:
            chosen.append(extra)

    return "\n\n".join(chosen)


def addendum(query: str, detail: str = "lean") -> str:
    """只返回**因当前情况临时需要**的分节（不含核心——核心已常驻在 system 里）。

    例如这一轮的叙述里出现了"开枪"，就把战斗规则临时附在世界消息里，
    下一轮没有战斗就自动不带。这样既保证"懂规则"，又不必每轮全量重发。
    """
    if detail in ("standard", "full"):
        return ""                      # 这些分节已经常驻，不必重复注入
    picked: list[str] = []
    for name, keys in AUTO_TRIGGERS.items():
        if any(k in (query or "") for k in keys):
            picked.append(SECTION_MAP[name])
    return "\n\n".join(picked)


def _read_text(path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=enc).strip()
        except Exception:
            continue
    return ""


def _user_rules(max_chars: int = 8000) -> str:
    """用户在 data/rules/ 根目录自备的规则（不含 full/ 全文目录）。"""
    rdir = cfgmod.data_root() / "rules"
    if not rdir.is_dir():
        return ""
    parts: list[str] = []
    used = 0
    for p in sorted(rdir.iterdir(), key=lambda x: x.name):
        if not p.is_file() or p.suffix.lower() not in (".md", ".txt"):
            continue
        text = _read_text(p)
        if not text:
            continue
        remain = max_chars - used
        if remain <= 0:
            break
        chunk = text[:remain]
        if len(text) > remain:
            chunk += " …（后略）"
        parts.append(f"# （自备规则）{p.stem}\n{chunk}")
        used += len(chunk)
    return "\n\n".join(parts)


def load_rules(detail: str = "lean") -> str:
    """给引擎外部调用的兼容入口（app 层用它拿默认规则文本）。"""
    return compose(detail)


# ══════════════════════════════════════════════ Tier 2 · 全文检索

def retrieve_rules(query: str, k: int = 3, window: int = 1400) -> list[dict]:
    """在 `data/rules/full/`（规则书 PDF 抽取的全文）里检索相关页。

    用于"这条规则原文到底怎么写的"这类按需查证，不必把整本书塞进每轮提示词。
    """
    import re

    full = cfgmod.data_root() / "rules" / "full"
    if not full.is_dir() or not (query or "").strip():
        return []
    terms = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", query)
    if not terms:
        return []

    scored: list[tuple[int, str, int, str]] = []
    for p in sorted(full.glob("*.txt")):
        text = _read_text(p)
        if not text:
            continue
        for chunk in text.split("[[page ")[1:]:
            num_str, _, body = chunk.partition("]]")
            try:
                page = int(num_str.strip())
            except ValueError:
                continue
            score = sum(body.count(t) for t in terms)
            if score:
                scored.append((score, p.stem, page, body.strip()))

    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, book, page, body in scored[:k]:
        excerpt = body[:window]
        if len(body) > window:
            excerpt += " …（后略）"
        out.append({"book": book, "page": page, "score": score, "text": excerpt})
    return out


def has_full_text() -> bool:
    full = cfgmod.data_root() / "rules" / "full"
    return full.is_dir() and any(full.glob("*.txt"))


# ══════════════════════════════════════════════ 成本估算

def estimate_tokens(text: str) -> int:
    """粗略字数→token 估算（中英混排）。只用于界面上给个量级。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk * 0.7 + other * 0.3)
