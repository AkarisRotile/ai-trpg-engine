"""术语触发式知识注入。

设计立场（这条比什么都省）：

  大模型本身就有 COC 的先验知识。它缺的不是"整节规则"，而是**术语的精确对齐**——
  比如"困难成功"到底是 ≤技能÷2 还是 ≤技能÷5、理智损失写成 0/1d4 时哪个是失败分支。
  所以正确做法不是每轮重发几节规则，而是：**这些词真的出现时，给一句话定义。**

成本对比（5 座位 × 40 轮）：
  整节注入   约 900 token/次 × 200 次 ≈ 18 万 token
  术语触发   约 60 token/次，且同一术语整局只解释一次 → 几千 token

术语表分两部分：
  · GLOSSARY        —— 引擎内建的权威定义（人工校对，短、准）
  · data/rules/glossary_extra.yaml —— 脚本从规则书挖出的候补 + 用户自加
    （由 tools/build_glossary.py 生成，取不到自动定义时不会污染内建表）
"""

from __future__ import annotations

from typing import Iterable

import yaml

from . import config as cfgmod

# ══════════════════════════════════════════════ 内建术语表
# 每条都压到一句话，够用即可——目的是对齐概念，不是复述规则书。

GLOSSARY: dict[str, str] = {
    # ---- 判定 ----
    "检定": "掷 d100 与技能值比较，点数越小越好。",
    "常规成功": "掷出的点数不超过技能值本身。",
    "困难成功": "掷出的点数不超过技能值的一半。",
    "极难成功": "掷出的点数不超过技能值的五分之一。",
    "大成功": "掷出 01，必定成功且效果最好。",
    "大失败": "掷出 100；技能值低于 50 时掷出 96-100 也算，后果最严重。",
    "奖励骰": "额外掷一个十位骰取其小，用于有利条件。",
    "惩罚骰": "额外掷一个十位骰取其大，用于不利条件。",
    "对抗检定": "双方各掷一次，先比成功等级，等级相同再比技能值高者胜。",
    "孤注一掷": "失败后换一种方式再试一次，但失败的后果会更严重。",
    "幸运消耗": "掷完之后花 1 点幸运可以把结果压低 1 点，用来刚好达标。",
    "达标线": "难度只决定最低要求：困难要至少困难成功，极难要至少极难成功。",

    # ---- 状态 ----
    "重伤": "单次受到最大生命值一半以上的伤害，需体质检定避免昏迷。",
    "濒死": "生命值降到 0，每轮需体质检定，失败即死亡。",
    "临时性疯狂": "单次损失 5 点以上理智，出现疯狂发作，持续数轮。",
    "不定性疯狂": "一天内累计损失当前理智的五分之一，需要长期治疗。",
    "疯狂发作": "疯狂时角色短暂失控，表现为逃跑、僵直、暴怒或失忆等。",
    "理智上限": "99 减去克苏鲁神话技能值；神话知识越多，上限越低。",

    # ---- 战斗 ----
    "先攻": "战斗轮按 DEX 从高到低决定行动顺序。",
    "闪避": "被近战攻击时用闪避检定对抗，成功则躲开。",
    "反击": "不闪避而是直接还手，用自己的格斗检定与对方对抗。",
    "瞄准": "花一轮瞄准，下一次射击获得奖励骰。",
    "连射": "一轮内多次射击，每次都要单独检定。",
    "护甲": "受到伤害时先扣掉的固定值。",
    "伤害加值": "由 STR+SIZ 推出的额外伤害骰，写在角色卡 DB 栏。",

    # ---- 理智与神话 ----
    "理智检定": "目睹超自然事物时掷 d100 对抗当前 SAN，失败损失更多。",
    "理智损失": "写成「成功损失/失败损失」，例如 0/1d4。",
    "魔法点": "施法消耗的资源，等于 POW÷5，每小时恢复 1 点。",
    "克苏鲁神话": "不参与车卡分配；每提升 1 点，理智上限就降 1 点。",
    "神话生物": "不属于人类认知范畴的存在，直视通常需要理智检定。",
    "旧日支配者": "克苏鲁这类古老神祇，通常无法被击败，只能被阻止。",

    # ---- 车卡 ----
    "职业点": "车卡时用于分配职业技能的点数，一般等于 EDU×4。",
    "兴趣点": "车卡时可自由分配的点数，等于 INT×2。",
    "信用评级": "职业技能之一，决定社会地位与可支配财力。",
    "生命值": "(CON+SIZ)÷10，角色能承受的伤害上限。",
    "移动力": "通常 8；STR 与 DEX 都低于 SIZ 时为 7，都高于时为 9。",

    # ---- 桌边通用 ----
    "守秘人": "主持游戏、描述世界与裁定后果的人，缩写 KP。",
    "调查员": "玩家操控的角色。本规则里不叫「玩家角色」，叫调查员。",
    "handout": "守秘人发给玩家的道具或文件，玩家看到的就是它的原文。",
}


def _load_extra() -> dict[str, str]:
    """加载脚本挖出/用户自备的补充术语。内建表优先，不被覆盖。"""
    p = cfgmod.data_root() / "rules" / "glossary_extra.yaml"
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        k, v = str(k).strip(), str(v).strip()
        if k and v and k not in GLOSSARY and len(v) <= 120:
            out[k] = v
    return out


def all_terms() -> dict[str, str]:
    merged = dict(_load_extra())
    merged.update(GLOSSARY)          # 内建覆盖补充
    return merged


# ══════════════════════════════════════════════ 触发器

class GlossaryInjector:
    """每个 Agent 一个。同一术语**整局只解释一次**，避免重复烧 token。"""

    def __init__(self, max_per_turn: int = 5) -> None:
        self.seen: set[str] = set()
        self.max_per_turn = max_per_turn
        self._terms: dict[str, str] | None = None

    @property
    def terms(self) -> dict[str, str]:
        if self._terms is None:
            self._terms = all_terms()
        return self._terms

    def notes_for(self, *texts: str, max_per_turn: int | None = None) -> list[str]:
        """扫描文本，返回本次需要补的术语定义（每局每词最多一次）。"""
        blob = "\n".join(t for t in texts if t)
        if not blob.strip():
            return []
        limit = self.max_per_turn if max_per_turn is None else max_per_turn

        hits: list[tuple[int, str]] = []
        for term in self.terms:
            if term in self.seen or term not in blob:
                continue
            # 长词优先，避免「理智检定」和「理智损失」互相抢位时乱序
            hits.append((len(term), term))
        hits.sort(key=lambda x: -x[0])

        picked: list[str] = []
        for _, term in hits:
            if len(picked) >= limit:
                break
            self.seen.add(term)
            picked.append(f"· 「{term}」，{self.terms[term]}")
        return picked

    def render(self, *texts: str, max_per_turn: int | None = None) -> str:
        notes = self.notes_for(*texts, max_per_turn=max_per_turn)
        if not notes:
            return ""
        return ("[这些词在这套规则里的准确意思]\n" + "\n".join(notes))

    def reset(self) -> None:
        self.seen.clear()
        self._terms = None

    def state(self) -> dict:
        return {"seen": sorted(self.seen)}

    def load_state(self, data: dict) -> None:
        self.seen = set((data or {}).get("seen") or [])


def notes_for(*texts: str, max_per_turn: int = 5) -> list[str]:
    """无状态版本，供脚本与测试使用。"""
    inj = GlossaryInjector(max_per_turn=max_per_turn)
    return inj.notes_for(*texts)


def coverage() -> dict[str, int]:
    extra = _load_extra()
    return {"builtin": len(GLOSSARY), "extra": len(extra), "total": len(GLOSSARY) + len(extra)}
