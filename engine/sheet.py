"""把角色卡写进 COC7 的 Excel 空白卡。

用的是 `COC7空白卡CY22.4 Plus.xlsx`（已复制到 `data\\templates\\`）。

这张卡是**公式驱动**的，所以填法有讲究：

  · 属性输入格在 `人物卡!U3/AA3/AG3`（STR/DEX/POW）、`U5/AA5/AG5`（CON/APP/EDU）、
    `U7/AA7/AG7`（SIZ/INT/幸运）。生命、理智、魔法、MOV、伤害加值
    都是这九个格子的**派生公式**，不用我们算。
  · 技能表分左右两块，每行拆成四段点数：
    初始(J) + 成长(L) + 职业(N) + 兴趣(P) = 成功率(R，公式求和)。
    所以正确的填法是**把点数拆回这几列**，而不是硬写一个最终值——
    这样打开卡的人一眼能看见这 70 点里哪些是职业点、哪些是兴趣点。
  · 名字→行号的对照表**从模板本身读**，不写死在代码里，
    换一版空白卡（只要列结构不变）也能用。

openpyxl 不计算公式；填完之后用 Excel / WPS 打开，派生值会自己算出来。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from . import chargen
from . import config as cfgmod

SHEET_NAME = "人物卡"

# 属性输入格（公式的源头）
ATTR_CELLS = {
    "STR": "U3", "DEX": "AA3", "POW": "AG3",
    "CON": "U5", "APP": "AA5", "EDU": "AG5",
    "SIZ": "U7", "INT": "AA7", "LUCK": "AG7",
}
# 角色信息
INFO_CELLS = {
    "name": "E3",        # 姓名
    "player": "E4",      # 玩家
    "occupation": "E5",  # 职业
    "age": "E6",         # 年龄
    "residence": "E7",   # 住地
    "gender": "M6",      # 性别
    "birthplace": "M7",  # 故乡
    "era": "M4",         # 时代
}
# 技能表两块：名称列 / 初始 / 成长 / 职业 / 兴趣
SKILL_BLOCKS = [
    {"name": "F", "base": "J", "grow": "L", "occ": "N", "interest": "P"},
    {"name": "AB", "base": "AF", "grow": "AH", "occ": "AJ", "interest": "AL"},
]
SKILL_ROW_FIRST = 16
SKILL_ROW_LAST = 49

# 背景故事区（人物卡右下）
BACKGROUND_CELLS = {
    "appearance": "W61",
    "beliefs": "W63",
    "important_person": "W65",
    "meaningful_place": "W67",
    "treasured_possession": "W69",
    "traits": "W71",
}
BACKGROUND_LABELS = {
    "appearance": "形象描述", "beliefs": "思想与信念", "important_person": "重要之人",
    "meaningful_place": "意义非凡之地", "treasured_possession": "宝贵之物",
    "traits": "特质",
}

_PLACEHOLDER = re.compile(r"[①②③④⑤⑥⑦⑧⑨]$")
_PUNCT = re.compile(r"[：:、,，.。\s（）()]+")


def template_path() -> Path | None:
    """找一个可用的空白卡模板。"""
    for p in (cfgmod.data_root() / "templates" / "COC7空白卡.xlsx",
              cfgmod.app_root() / "COC7空白卡CY22.4 Plus.xlsx"):
        if p.exists():
            return p
    return None


def _norm_skill(name: str) -> str:
    return _PUNCT.sub("", _PLACEHOLDER.sub("", (name or "").strip()))


def _read_sheet_layout(tpl: Path) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """从模板里读出「技能名 → (块序号, 行号)」和可用的空位。

    不写死行号：换一版卡、或者用户自己加了技能行，只要列结构不变就还能用。
    """
    import openpyxl
    wb = openpyxl.load_workbook(str(tpl), data_only=False, read_only=False)
    ws = wb[SHEET_NAME]
    mapping: dict[str, tuple[int, int]] = {}
    free: list[str] = []
    for bi, blk in enumerate(SKILL_BLOCKS):
        col = blk["name"]
        for row in range(SKILL_ROW_FIRST, SKILL_ROW_LAST + 1):
            raw = ws[f"{col}{row}"].value
            label = str(raw).strip() if raw is not None else ""
            if not label:
                continue
            key = _norm_skill(label)
            if key:
                mapping.setdefault(key, (bi, row))
            # 带序号或带"："的是可替换的空位（技艺①、格斗：、自定义技能…）
            if _PLACEHOLDER.search(label) or label.endswith(("：", ":")) \
                    or "自定义" in label:
                free.append(f"{bi}:{row}:{label}")
    wb.close()
    return mapping, free


def _split_points(total: int, base: int, is_occupation: bool) -> dict[str, int]:
    """把「最终值」拆回 初始/职业/兴趣 三段。

    职业本行技能的点数进「职业」列，其余进「兴趣」列——这样卡上能看出
    哪些点是职业给的、哪些是自己兴之所至加的。
    """
    delta = max(0, int(total) - int(base))
    if is_occupation:
        return {"base": base, "occ": delta, "interest": 0, "grow": 0}
    return {"base": base, "occ": 0, "interest": delta, "grow": 0}


def write_sheet(character: dict[str, Any], out_path: Path, *,
                player_name: str = "", era: str = "1920 年代",
                residence: str = "", background: dict[str, str] | None = None,
                template: Path | None = None) -> Path:
    """按角色数据生成一张填好的 Excel 卡。"""
    import openpyxl

    tpl = template or template_path()
    if tpl is None:
        raise FileNotFoundError(
            "找不到空白卡模板。请把 COC7 空白卡 xlsx 放到 data\\templates\\COC7空白卡.xlsx")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tpl, out_path)

    wb = openpyxl.load_workbook(str(out_path), data_only=False)
    ws = wb[SHEET_NAME]
    attrs = character.get("attributes") or {}

    for key, cell in ATTR_CELLS.items():
        if attrs.get(key) is not None:
            ws[cell] = int(attrs[key])

    ws[INFO_CELLS["name"]] = str(character.get("name") or "")
    ws[INFO_CELLS["player"]] = player_name
    ws[INFO_CELLS["occupation"]] = str(character.get("occupation") or "")
    try:
        ws[INFO_CELLS["age"]] = int(character.get("age") or 30)
    except (TypeError, ValueError):
        pass
    ws[INFO_CELLS["era"]] = era
    if residence:
        ws[INFO_CELLS["residence"]] = residence
    gender = str(character.get("gender") or "")
    if gender:
        ws[INFO_CELLS["gender"]] = gender

    # ---- 技能：拆回 职业/兴趣 两列 ----
    mapping, free_slots = _read_sheet_layout(tpl)
    used_rows: set[tuple[int, int]] = set()
    occ_pack = {_norm_skill(n) for n, _ in
                chargen._skill_pack(str(character.get("occupation") or ""))}
    free_iter = iter(free_slots)

    for item in (character.get("skills") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        try:
            value = int(item.get("value") or 0)
        except (TypeError, ValueError):
            continue
        if not name:
            continue

        slot = mapping.get(_norm_skill(name))
        if slot is not None and slot in used_rows:
            slot = None
        if slot is None:
            for cand in free_iter:                 # 找一个空位塞自定义技能
                bi, row, _label = cand.split(":")
                slot = (int(bi), int(row))
                if slot not in used_rows:
                    break
            else:
                continue
        bi, row = slot
        used_rows.add(slot)

        blk = SKILL_BLOCKS[bi]
        if slot not in mapping.values() or mapping.get(_norm_skill(name)) != slot:
            ws[f"{blk['name']}{row}"] = name   # 自定义技能要把名字也写上

        base = chargen.base_of(name, attrs)
        pts = _split_points(value, base, _norm_skill(name) in occ_pack)
        ws[f"{blk['base']}{row}"] = pts["base"]
        ws[f"{blk['grow']}{row}"] = pts["grow"]
        ws[f"{blk['occ']}{row}"] = pts["occ"]
        ws[f"{blk['interest']}{row}"] = pts["interest"]

    # ---- 背景 ----
    bg = background or {}
    if character.get("backstory") and not bg.get("beliefs"):
        bg = {**bg, "beliefs": str(character["backstory"])[:400]}
    inv = character.get("inventory") or []
    if inv and not bg.get("treasured_possession"):
        bg = {**bg, "treasured_possession": "、".join(str(x) for x in inv[:6])}
    for key, cell in BACKGROUND_CELLS.items():
        if bg.get(key):
            ws[cell] = str(bg[key])[:800]

    wb.save(str(out_path))
    wb.close()
    return out_path


def dump_character_yaml(character: dict[str, Any], out_path: Path,
                        player_name: str = "") -> Path:
    """同一张卡同时存一份结构化 YAML，方便程序读取和人工核对。"""
    import yaml
    attrs = character.get("attributes") or {}
    data = {
        "玩家": player_name,
        "角色": {k: character.get(k) for k in
                 ("name", "occupation", "age", "gender", "backstory")},
        "属性": attrs,
        "技能": character.get("skills") or [],
        "随身物品": character.get("inventory") or [],
        "背景": character.get("background") or {},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8")
    return out_path
