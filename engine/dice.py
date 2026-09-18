"""COC 第七版骰子与判定内核。

设计要点：**引擎独占全部随机性与全部数值**。
模型只能"申请检定"和"叙述结果"，不能宣告成败、不能改数。
这既是防作弊，也是消灭模型"作者人格"的结构性手段——
当模型发现自己决定不了结果时，它就只剩"行动"这一条路可走。
"""

from __future__ import annotations

import random
import re
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Any

# ---------------------------------------------------------------- 常量

LEVEL_ORDER = ["fumble", "fail", "regular", "hard", "extreme", "critical"]
LEVEL_RANK = {"fumble": 0, "fail": 1, "regular": 2, "hard": 3, "extreme": 4, "critical": 5}
LEVEL_CN = {
    "fumble": "大失败",
    "fail": "失败",
    "regular": "常规成功",
    "hard": "困难成功",
    "extreme": "极难成功",
    "critical": "大成功",
}
DIFFICULTY_CN = {"regular": "常规", "hard": "困难", "extreme": "极难"}
REQUIRED_RANK = {"regular": 2, "hard": 3, "extreme": 4}

# 常用技能的默认基准值（COC7 基础值），用于自动补全技能表
BASE_SKILLS: dict[str, int] = {
    "会计": 5, "人类学": 1, "估价": 5, "考古学": 1, "取悦": 15, "攀爬": 20,
    "计算机使用": 5, "信用评级": 0, "克苏鲁神话": 0, "乔装": 5, "闪避": 25,
    "汽车驾驶": 20, "电气维修": 10, "电子学": 1, "话术": 5, "格斗": 25,
    "射击": 20, "急救": 30, "历史": 5, "恐吓": 15, "跳跃": 20, "母语": 0,
    "法律": 5, "图书馆利用": 20, "聆听": 20, "锁匠": 1, "机械维修": 10,
    "医学": 1, "博物学": 10, "导航": 10, "神秘学": 5, "操作重型机械": 1,
    "说服": 10, "精神分析": 1, "心理学": 10, "骑术": 5, "科学": 1,
    "妙手": 10, "侦查": 25, "潜行": 20, "生存": 10, "游泳": 20,
    "投掷": 20, "追踪": 10, "潜水": 1, "爆破": 1, "读唇": 1, "催眠": 1,
    "信用": 0, "闪避": 25,
}


# ---------------------------------------------------------------- 基础掷骰

def _roll_d100(rng: random.Random, bonus: int = 0, penalty: int = 0) -> tuple[int, list[int], int]:
    """掷 d100。奖励骰取十位最小值，惩罚骰取十位最大值，个位骰共用。"""
    ones = rng.randint(0, 9)
    extra = abs(bonus - penalty)
    tens = [rng.randint(0, 9) for _ in range(1 + extra)]
    if bonus > penalty:
        t = min(tens)
    elif penalty > bonus:
        t = max(tens)
    else:
        t = tens[0]
    value = t * 10 + ones
    if value == 0:
        value = 100  # 00 + 0 读作 100
    return value, tens, ones


def roll_expr(expr: str, rng: random.Random | None = None, db: str = "0") -> tuple[int, str]:
    """掷形如 "1d8+2" / "2d6" / "1d10+db" 的表达式，返回 (总值, 明细文本)。"""
    rng = rng or random
    expr = (expr or "0").strip().lower().replace(" ", "")
    if expr == "db":
        expr = db or "0"
    total = 0
    parts = re.findall(r"([+-]?)(\d*)d(\d+)|([+-]?\d+)", expr)
    details: list[str] = []
    if not parts:
        return 0, "0"
    for sign, count, faces, flat in parts:
        if faces:
            n = int(count) if count else 1
            f = int(faces)
            rolls = [rng.randint(1, f) for _ in range(n)]
            sub = sum(rolls)
            total += -sub if sign == "-" else sub
            details.append(f"{'-' if sign == '-' else ''}{n}d{f}[{'+'.join(map(str, rolls))}]")
        elif flat:
            v = int(flat)
            total += v
            details.append(f"{v:+d}" if v >= 0 else str(v))
    return total, " ".join(details)


def damage_bonus(str_: int, siz: int) -> str:
    """按 COC7 由 STR+SIZ 推导伤害加值。"""
    s = str_ + siz
    if s <= 64:
        return "-2"
    if s <= 84:
        return "-1"
    if s <= 124:
        return "0"
    if s <= 164:
        return "1d4"
    if s <= 204:
        return "1d6"
    extra = (s - 205) // 80
    return f"{2 + extra}d6"


def mov_rate(str_: int, dex: int, siz: int) -> int:
    if str_ < siz and dex < siz:
        return 7
    if str_ > siz and dex > siz:
        return 9
    return 8


# ---------------------------------------------------------------- 检定

@dataclass
class CheckResult:
    skill: str
    skill_value: int
    difficulty: str = "regular"
    roll: int = 0
    tens: list[int] = field(default_factory=list)
    ones: int = 0
    level: str = "fail"
    bonus: int = 0
    penalty: int = 0

    @property
    def level_cn(self) -> str:
        return LEVEL_CN[self.level]

    @property
    def rank(self) -> int:
        return LEVEL_RANK[self.level]

    @property
    def succeeded(self) -> bool:
        return self.rank >= REQUIRED_RANK.get(self.difficulty, 2)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["level_cn"] = self.level_cn
        d["difficulty_cn"] = DIFFICULTY_CN.get(self.difficulty, self.difficulty)
        d["success"] = self.succeeded
        d["text"] = self.text()
        return d

    def text(self) -> str:
        extra = ""
        if self.bonus:
            extra += f"（{self.bonus} 个奖励骰）"
        if self.penalty:
            extra += f"（{self.penalty} 个惩罚骰）"
        diff = "" if self.difficulty == "regular" else f"·{DIFFICULTY_CN[self.difficulty]}"
        verdict = self.level_cn if self.succeeded else f"{self.level_cn}（未达标）"
        return (
            f"{self.skill}{diff}检定{extra}：掷出 {self.roll:02d} / 目标 {self.skill_value} "
            f"→ {verdict}"
        )


def check(
    skill: str,
    skill_value: int,
    difficulty: str = "regular",
    bonus: int = 0,
    penalty: int = 0,
    rng: random.Random | None = None,
) -> CheckResult:
    """执行一次 d100 检定。成功等级永远以**原始技能值**为准，难度只决定达标线。"""
    rng = rng or random
    skill_value = max(0, int(skill_value))
    roll, tens, ones = _roll_d100(rng, bonus, penalty)

    if roll == 1:
        level = "critical"
    elif skill_value > 0 and roll <= skill_value // 5:
        level = "extreme"
    elif skill_value > 0 and roll <= skill_value // 2:
        level = "hard"
    elif roll <= skill_value:
        level = "regular"
    elif roll == 100 or (skill_value < 50 and roll >= 96):
        level = "fumble"
    else:
        level = "fail"

    return CheckResult(
        skill=skill, skill_value=skill_value, difficulty=difficulty,
        roll=roll, tens=tens, ones=ones, level=level, bonus=bonus, penalty=penalty,
    )


def opposed(a: CheckResult, b: CheckResult) -> str:
    """对抗检定：先比成功等级，再比技能值。返回 'a' / 'b' / 'tie'。"""
    if a.rank != b.rank:
        return "a" if a.rank > b.rank else "b"
    if a.succeeded and b.succeeded:
        if a.skill_value != b.skill_value:
            return "a" if a.skill_value > b.skill_value else "b"
        return "tie"
    return "tie"


def san_loss(expr: str, rng: random.Random | None = None) -> tuple[int, str]:
    """理智损失表达式 "0/1d4" 或 "1/1d6+1"，返回失败分支的损失与明细。"""
    rng = rng or random
    parts = str(expr).split("/")
    fail_expr = parts[-1] if parts else "0"
    value, detail = roll_expr(fail_expr, rng)
    return max(0, value), detail


def success_san_loss(expr: str, rng: random.Random | None = None) -> tuple[int, str]:
    rng = rng or random
    parts = str(expr).split("/")
    ok_expr = parts[0] if len(parts) > 1 else "0"
    value, detail = roll_expr(ok_expr, rng)
    return max(0, value), detail


# ---------------------------------------------------------------- 骰子日志

@dataclass
class DiceRecord:
    turn: int
    actor: str
    kind: str          # check | damage | san | opposed | raw
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)


class DiceKernel:
    """会话级的骰子内核。**全场唯一能产生随机数的地方。**

    ── 为什么随机源要用 CSPRNG ──
    这个项目明确拒绝「骰子服务于剧情」。要做到这一点，光靠提示词说
    「不要改结果」是不够的，必须在结构上让模型**够不着**骰子：
      · 随机数由 Python 的 `secrets.SystemRandom`（操作系统熵池）产生，
        不是 `random.Random` 那种可被推导的梅森旋转
      · 模型只能提交「掷什么」的申请，永远拿不到也改不了出目
      · 每一次掷骰都记进日志：序号、表达式、**每一颗骰子的原始点数**、时间、发起人
    界面上的骰子面板直接展示这份日志，所以「是不是真随机」是可以被查的。

    只有在自动化测试里才会显式给 seed，那时记录里会标 `fair=False`。
    """

    def __init__(self, seed: int | None = None) -> None:
        if seed is None:
            self.rng: random.Random = secrets.SystemRandom()
            self.fair = True
        else:
            self.rng = random.Random(seed)
            self.fair = False
        self.log: list[DiceRecord] = []
        self.turn = 0
        self._seq = 0

    # -------------------------------------------------- 基础

    def set_turn(self, turn: int) -> None:
        self.turn = turn

    def _record(self, actor: str, kind: str, summary: str,
                payload: dict[str, Any], source: str = "") -> DiceRecord:
        self._seq += 1
        payload = {**payload, "seq": self._seq, "at": time.time(),
                   "fair": self.fair, "turn": self.turn, "source": source}
        rec = DiceRecord(turn=self.turn, actor=actor, kind=kind,
                         summary=summary, payload=payload)
        self.log.append(rec)
        del self.log[:-500]
        return rec

    # -------------------------------------------------- 检定

    def do_check(self, actor: str, skill: str, value: int, **kw) -> CheckResult:
        res = check(skill, value, rng=self.rng, **kw)
        self._record(actor, "check", res.text(),
                     {**res.to_dict(), "actor": actor}, kw.pop("source", ""))
        return res

    def do_damage(self, actor: str, target: str, expr: str, db: str = "0",
                  source: str = "") -> dict[str, Any]:
        total, detail = roll_expr(expr, self.rng, db=db)
        total = max(0, total)
        summary = f"{target} 受到 {total} 点伤害（{expr} → {detail}）"
        payload = {"actor": actor, "target": target, "expr": expr,
                   "detail": detail, "total": total, "summary": summary}
        self._record(actor, "damage", summary, payload, source)
        return payload

    def do_san(self, actor: str, expr: str, source: str = "") -> dict[str, Any]:
        fail_val, fail_detail = san_loss(expr, self.rng)
        ok_val, ok_detail = success_san_loss(expr, self.rng)
        payload = {"actor": actor, "expr": expr, "fail_loss": fail_val,
                   "fail_detail": fail_detail, "success_loss": ok_val,
                   "success_detail": ok_detail}
        self._record(actor, "san",
                     f"{actor} 的理智损失表达式 {expr}（成功 {ok_val} / 失败 {fail_val}）",
                     payload, source)
        return payload

    # -------------------------------------------------- 通用掷骰工具

    def do_roll(self, actor: str, expr: str, purpose: str = "",
                source: str = "ai") -> dict[str, Any]:
        """AI 自行发起的任意掷骰。

        这就是那个"内置骰子工具"：模型在输出里写 <roll>1d8+2 | 伤害</roll>，
        引擎真的掷，把结果和**每一颗骰子的原始点数**一起回给它。
        模型永远接触不到随机源，也就不可能让出目服从剧情。
        """
        expr = (expr or "1d100").strip() or "1d100"
        total, detail = roll_expr(expr, self.rng)
        summary = f"{purpose or '掷骰'}：{expr} → {total}"
        if detail:
            summary += f"（{detail}）"
        payload = {"actor": actor, "expr": expr, "purpose": purpose,
                   "total": total, "detail": detail, "summary": summary,
                   "source": source}
        self._record(actor, "roll", summary, payload, source)
        return payload

    def do_raw(self, actor: str, expr: str, purpose: str = "",
               source: str = "") -> dict[str, Any]:
        return self.do_roll(actor, expr, purpose, source or "engine")

    # -------------------------------------------------- 审计

    def audit(self) -> dict[str, Any]:
        """给界面看的骰子审计摘要。"""
        by_kind: dict[str, int] = {}
        for r in self.log:
            by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
        return {
            "total": len(self.log),
            "fair": self.fair,
            "entropy": "操作系统熵池 (secrets.SystemRandom)" if self.fair
                       else "固定种子（仅用于测试）",
            "by_kind": by_kind,
        }

    def recent(self, limit: int = 40) -> list[dict[str, Any]]:
        return [{"turn": r.turn, "actor": r.actor, "kind": r.kind,
                 "summary": r.summary, **{k: v for k, v in r.payload.items()
                                          if k in ("seq", "expr", "total", "detail",
                                                   "purpose", "fair")}}
                for r in self.log[-limit:]]


def fairness_probe(kernel: DiceKernel, sides: int = 6, times: int = 6000) -> dict[str, Any]:
    """均匀性自检：掷很多次，看每个面出现频率是不是接近理论值。

    这是「拒绝骰子服务于剧情」最直接的证据——如果出目可以被摆布，
    分布就会偏。
    """
    counts = Counter()
    for _ in range(times):
        total, _ = roll_expr(f"1d{sides}", kernel.rng)
        counts[total] += 1
    expected = times / sides
    chi2 = sum((counts.get(f, 0) - expected) ** 2 / expected
               for f in range(1, sides + 1))
    worst = max(abs(counts.get(f, 0) - expected) / expected
                for f in range(1, sides + 1))
    return {"sides": sides, "times": times, "counts": dict(sorted(counts.items())),
            "expected": expected, "chi2": round(chi2, 2),
            "max_deviation": round(worst, 4)}
