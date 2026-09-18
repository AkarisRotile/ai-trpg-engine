"""从**用户那张 COC7 空白卡**里读车卡规则。

这张卡本身就是一个计算器（`COC7空白卡CY22.4 Plus.xlsx`）：

- `职业列表`：224 个职业，每个都写明了
    D 列 信用评级区间（`30-70`）
    E 列 职业属性（`教育×2＋敏捷×2`，给人看的）
    F 列 技能点公式（`=EDU*2+MAX(DEX*2,APP*2)`，真公式）
- `本职技能`：一张按职业标好本职技能的矩阵（★ / ☆ / ☯ / ⊙ / ※）
- `人物卡` 技能表把点数分成 **职业列(N) + 兴趣列(P)**，
  `J50` 会自己算出并显示「剩余职业点=… 剩余兴趣点=…」

所以**权威源是这张卡，不是引擎里另写一张表**。
引擎自己那份 `OCCUPATION_SPECS` 只在读不到模板时兜底——
而且它一开始就猜错了好几处（记者其实是「教育×2＋外貌或敏捷×2」，
本职技能里有闪避、恐吓、取悦、说服、估价）。

有了这个，引擎算出来的预算和卡上算出来的能对得上，
用户打开卡看到的就是同一套数字。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config as cfgmod

OCC_SHEET = "职业列表"
SKILL_SHEET = "本职技能"
MARKS = ("★", "☆", "☯", "⊙", "※")

_PLACEHOLDER = re.compile(r"[①②③④⑤⑥⑦⑧⑨]$")
_PUNCT = re.compile(r"[：:、,，.。\s（）()]+")
# 公式求值只放行这些东西，别的一律拒绝
_SAFE_EXPR = re.compile(r"^[0-9A-Za-z_+\-*/().,\s]*$")
# 卡上的公式只会用到这几个函数（Excel 写法 → Python 实现）
_FUNCS: dict[str, Any] = {
    "MAX": max, "MIN": min,
    "SUM": lambda *a: sum(a),
    "INT": int, "ROUND": round,
}
_FUNC_OK = set(_FUNCS)


def norm_skill(name: str) -> str:
    """技能名归一化：去掉序号占位符与标点，好让卡和引擎对得上。"""
    return _PUNCT.sub("", _PLACEHOLDER.sub("", (name or "").strip()))


def template_path() -> Path | None:
    for p in (cfgmod.data_root() / "templates" / "COC7空白卡.xlsx",
              cfgmod.app_root() / "COC7空白卡CY22.4 Plus.xlsx"):
        if p.exists():
            return p
    return None


@dataclass
class Occupation:
    name: str
    index: int
    credit: tuple[int, int] = (0, 99)
    formula_text: str = ""
    formula: str = ""
    skills: tuple[str, ...] = field(default_factory=tuple)

    def points(self, attrs: dict[str, int]) -> int:
        """按卡上的公式算职业点。算不出来就退回 EDU×4。"""
        v = eval_formula(self.formula, attrs)
        return int(v) if v is not None else int(attrs.get("EDU", 0)) * 4


def eval_formula(expr: str, attrs: dict[str, int]) -> int | None:
    """求值卡上那种小公式：`=EDU*4`、`=EDU*2+MAX(DEX*2,APP*2)`。

    刻意做得很窄：只放行属性名、数字和几个函数，别的一律不认。
    模板是我们自己带的文件，但仍然不该无脑 eval。
    """
    if not expr:
        return None
    e = str(expr).strip().lstrip("=")
    if not e or not _SAFE_EXPR.match(e):
        return None
    # 属性名换成数字（长的先换，避免 EDU 被 DU 之类截断）
    for key in sorted(attrs, key=len, reverse=True):
        e = re.sub(rf"\b{re.escape(key)}\b", str(int(attrs.get(key) or 0)), e)
    # 还剩下的标识符必须是白名单里的函数
    for ident in set(re.findall(r"[A-Za-z_]\w*", e)):
        if ident.upper() not in _FUNC_OK:
            return None
    env: dict[str, Any] = {"__builtins__": {}, **_FUNCS}
    try:
        val = eval(e, env)                      # noqa: S307 —— 上面已经严格白名单过了
        return int(val)
    except Exception:
        return None


def _parse_credit(text: str, default: tuple[int, int] = (0, 99)) -> tuple[int, int]:
    nums = re.findall(r"\d+", str(text or ""))
    if len(nums) >= 2:
        lo, hi = int(nums[0]), int(nums[1])
        return (lo, hi) if lo <= hi else (hi, lo)
    if len(nums) == 1:
        return (0, int(nums[0]))
    return default


@lru_cache(maxsize=1)
def occupation_table() -> dict[str, Occupation]:
    """读 `职业列表`。读不到就返回空表（调用方自己兜底）。"""
    tpl = template_path()
    if tpl is None:
        return {}
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(tpl), data_only=False, read_only=True)
        try:
            if OCC_SHEET not in wb.sheetnames:
                return {}
            ws = wb[OCC_SHEET]
            out: dict[str, Occupation] = {}
            for row in ws.iter_rows(min_row=3, max_row=260,
                                    min_col=1, max_col=6, values_only=True):
                idx, name, _custom, credit, ftext, formula = (list(row) + [None] * 6)[:6]
                nm = str(name or "").strip()
                if idx is None or not nm:
                    continue
                out[nm] = Occupation(
                    name=nm, index=int(idx),
                    credit=_parse_credit(str(credit or "")),
                    formula_text=str(ftext or "").strip(),
                    formula=str(formula or "").strip())
            return out
        finally:
            wb.close()
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _skill_matrix() -> dict[str, tuple[str, ...]]:
    """读 `本职技能` 矩阵：职业名 → 本职技能名（已归一化）。"""
    tpl = template_path()
    if tpl is None:
        return {}
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(tpl), data_only=True, read_only=True)
        try:
            if SKILL_SHEET not in wb.sheetnames:
                return {}
            ws = wb[SKILL_SHEET]
            rows = list(ws.iter_rows(min_row=1, max_row=110, min_col=1,
                                     max_col=min(ws.max_column or 1, 900),
                                     values_only=True))
            if len(rows) < 8:
                return {}
            header = rows[1]                     # 第 2 行 = 职业名
            cols: dict[int, str] = {}
            for ci, val in enumerate(header):
                nm = str(val or "").strip()
                if nm and ci > 0:
                    cols[ci] = nm
            out: dict[str, list[str]] = {nm: [] for nm in cols.values()}
            for r in rows[7:]:
                skill = str(r[0] or "").strip() if r else ""
                if not skill:
                    continue
                for ci, nm in cols.items():
                    mark = str(r[ci] or "").strip() if ci < len(r) else ""
                    if mark in MARKS:
                        out[nm].append(norm_skill(skill))
            return {k: tuple(dict.fromkeys(v)) for k, v in out.items()}
        finally:
            wb.close()
    except Exception:
        return {}


def find_occupation(name: str) -> Occupation | None:
    """按名字找职业：先精确、再包含、最后去掉标点再试。

    模型写的职业名和卡上那张表对不上的时候很多（「刑警」在卡里叫「警察」），
    所以这里容错一点；真找不到就返回 None，由调用方兜底。
    """
    nm = (name or "").strip()
    if not nm:
        return None
    table = occupation_table()
    if not table:
        return None
    if nm in table:
        return _with_skills(table[nm])
    norm = norm_skill(nm)
    for k, v in table.items():
        if norm and norm == norm_skill(k):
            return _with_skills(v)
    # 包含关系：取名字最接近的那个
    cands = [(len(k), v) for k, v in table.items()
             if norm and (norm in norm_skill(k) or norm_skill(k) in norm)]
    if cands:
        return _with_skills(min(cands, key=lambda x: x[0])[1])
    return None


def _with_skills(occ: Occupation) -> Occupation:
    if occ.skills:
        return occ
    sk = _skill_matrix().get(occ.name)
    return Occupation(occ.name, occ.index, occ.credit, occ.formula_text,
                      occ.formula, tuple(sk or ()))


def all_occupation_names() -> list[str]:
    return sorted(occupation_table().keys())


def describe_occupation(occ: Occupation, attrs: dict[str, int]) -> str:
    """给提示词用的一句话：「记者（信用评级 9-30，职业点 = 教育×2＋外貌或敏捷×2 = 260）」"""
    lo, hi = occ.credit
    return (f"{occ.name}（信用评级 {lo}–{hi}；"
            f"职业点 = {occ.formula_text or '教育×4'} = {occ.points(attrs)}）")
