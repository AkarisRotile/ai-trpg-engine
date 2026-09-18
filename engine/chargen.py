"""调查员生成：预设法 + 随机掷骰法。

COC7 属性生成：STR/CON/DEX/APP/POW = 3d6×5，SIZ/INT = (2d6+6)×5，EDU = (2d6+6)×5。
派生：HP=(CON+SIZ)//10，MP=POW//5，SAN=POW，DB 由 STR+SIZ 推出。
"""

from __future__ import annotations

import random
import re
from typing import Any

from .dice import BASE_SKILLS, damage_bonus, mov_rate

# ---------------------------------------------------------------- 预设调查员
# 每位预设都自带差异化 playstyle，避免多个 PL 人设趋同（这是常见塌陷点）。

PRESET_INVESTIGATORS: list[dict[str, Any]] = [
    {
        "name": "艾德温·卡特",
        "occupation": "民俗学者",
        "age": 34,
        "gender": "男",
        "attributes": {"STR": 45, "CON": 50, "DEX": 60, "APP": 55,
                       "POW": 75, "SIZ": 55, "INT": 80, "EDU": 85, "LUCK": 65},
        "skills": [("图书馆利用", 75), ("神秘学", 65), ("历史", 60), ("母语", 85),
                   ("聆听", 50), ("侦查", 45), ("说服", 40), ("心理学", 40),
                   ("闪避", 30), ("信用评级", 40)],
        "inventory": ["黄铜怀表", "速记笔记本与碳素笔", "小型手电筒",
                      "一本翻烂的《金枝》", "阿卡姆-密斯卡托尼克大学借阅证"],
        "backstory": "在密斯卡托尼克大学任教，专攻新英格兰地区的民间传说。"
                     "三年前整理一批从敦威治收购的旧信时，读到过不该读的东西，"
                     "此后对『没有解释的现象』既恐惧又无法移开视线。",
        "playstyle": "慎重解密流",
        "player_name": "老周",
        "table_voice": "说话慢、爱较真，凡事要先问清楚再动手。"
                       "口头禅是「等一下，我确认个事」，会把已知线索一条条念出来对。"
                       "掷骰前先算概率，掷完不管好坏都要点评一句。",
        "traits": ["对未知事物极度警惕", "重视队友协作", "倾向在安全距离搜集情报",
                   "习惯把一切写进笔记本"],
    },
    {
        "name": "玛格丽特·霍尔",
        "occupation": "私家侦探",
        "age": 41,
        "gender": "女",
        "attributes": {"STR": 55, "CON": 60, "DEX": 70, "APP": 60,
                       "POW": 60, "SIZ": 55, "INT": 70, "EDU": 70, "LUCK": 55},
        "skills": [("侦查", 70), ("心理学", 60), ("聆听", 55), ("射击", 55),
                   ("潜行", 50), ("话术", 50), ("闪避", 45), ("锁匠", 40),
                   ("法律", 35), ("母语", 70)],
        "inventory": ["点三八左轮（6 发）", "侦探执照", "一组开锁工具",
                      "扁酒壶", "拍立得相机"],
        "backstory": "波士顿警局干了十二年，因为在某桩案子上坚持『凶手不是人』"
                     "而被劝退。现在自己挂牌营业，客户多是些不愿意报警的人。",
        "playstyle": "主动出击流",
        "player_name": "阿凛",
        "table_voice": "急性子，句子很短，爱催促别人「走啊」「磨蹭啥」。"
                       "爱吐槽，尤其爱吐槽队友的馊主意，但真出事第一个冲上去。"
                       "不爽的时候会直接说「这破房子我受够了」。",
        "traits": ["先动手再解释", "不信任何人给的第一版说法", "对枪械有职业性依赖",
                   "嘴很硬但会护着队友"],
    },
    {
        "name": "塞缪尔·奥克",
        "occupation": "医生",
        "age": 52,
        "gender": "男",
        "attributes": {"STR": 40, "CON": 55, "DEX": 55, "APP": 50,
                       "POW": 65, "SIZ": 60, "INT": 75, "EDU": 90, "LUCK": 50},
        "skills": [("医学", 75), ("急救", 70), ("心理学", 60), ("科学", 60),
                   ("精神分析", 55), ("博物学", 50), ("母语", 90), ("说服", 40),
                   ("侦查", 35), ("闪避", 30)],
        "inventory": ["出诊皮包（含吗啡、绷带、手术刀）", "听诊器",
                      "一副老花镜", "圣公会祈祷书（他自己也说不清为什么带着）"],
        "backstory": "在阿卡姆开了二十年诊所，见过太多『医学解释不了』的临终呓语。"
                     "他从不跟人争辩这世上有没有鬼，只是每次都把尸体检查得格外仔细。",
        "playstyle": "冷静支援流",
        "player_name": "阿澈",
        "table_voice": "话少，习惯用「嗯」「行」开头，但冷不丁冒一句毒舌会让人笑出来。"
                       "不抢戏，别人吵的时候他去做事。偶尔会突然聊起跟当前场景无关的东西（脱线）。",
        "traits": ["先救人再问为什么", "说话克制、不轻易下结论",
                   "对超自然现象表现出职业性的麻木", "是全队的理智锚点"],
    },
    {
        "name": "陈默",
        "occupation": "记者",
        "age": 28,
        "gender": "男",
        "attributes": {"STR": 50, "CON": 55, "DEX": 65, "APP": 65,
                       "POW": 55, "SIZ": 50, "INT": 75, "EDU": 80, "LUCK": 60},
        "skills": [("母语", 80), ("图书馆利用", 65), ("话术", 60), ("取悦", 55),
                   ("侦查", 50), ("心理学", 50), ("聆听", 45), ("闪避", 35),
                   ("摄影", 40), ("历史", 30)],
        "inventory": ["徕卡 III 相机与 6 卷胶卷", "记者证", "采访本",
                      "一副扑克牌（用来跟人搭话）", "半包骆驼香烟"],
        "backstory": "给《阿卡姆广告人报》写社会版，专挑别人不愿意写的题材。"
                     "主编说他『有新闻鼻子但没有脑子』——他本人把这当成褒奖。",
        "playstyle": "社交试探流",
        "player_name": "小满",
        "table_voice": "贫嘴，爱开玩笑，越紧张话越多，用玩笑盖恐惧。"
                       "喜欢给 NPC 起外号，喜欢吐槽 KP「你是不是又想搞我们」。"
                       "轮到自己掷骰会喊「来来来」然后大叫。",
        "traits": ["习惯先跟人聊三句再问正事", "对细节有职业性的敏感",
                   "好奇心得不到满足比危险更让他难受", "会记录一切"],
    },
    {
        "name": "伊莎贝拉·罗西",
        "occupation": "古董商",
        "age": 37,
        "gender": "女",
        "attributes": {"STR": 45, "CON": 50, "DEX": 60, "APP": 70,
                       "POW": 60, "SIZ": 50, "INT": 70, "EDU": 75, "LUCK": 55},
        "skills": [("估价", 70), ("历史", 60), ("图书馆利用", 55), ("说服", 55),
                   ("母语", 75), ("会计", 45), ("侦查", 40), ("妙手", 35),
                   ("闪避", 35), ("信用评级", 50)],
        "inventory": ["放大镜", "一小盒盐（她坚持说是『老规矩』）",
                      "账本与现金夹", "一把拆信刀", "祖父留下的一枚圣克里斯托弗徽章"],
        "backstory": "在波士顿经营一家只做熟客生意的古董铺，见过太多东西"
                     "『从某个地方来，又往某个地方去』。她的经验是：价钱可以谈，来历不能问。",
        "playstyle": "交易与交涉流",
        "player_name": "阿樱",
        "table_voice": "精打细算，爱把选项列成一二三，喜欢问「代价是什么」。"
                       "会认真记账，也会在括号里提醒队友「你那个手电筒电池不多了」。"
                       "迷信，坚持某些规矩不能破。",
        "traits": ["凡事先谈条件", "对物品的年代与来历有近乎迷信的敏感",
                   "绝不空手离开任何一间屋子", "相信规矩和禁忌是有用的"],
    },
]


def preset(i: int) -> dict[str, Any]:
    return PRESET_INVESTIGATORS[i % len(PRESET_INVESTIGATORS)]


# ---------------------------------------------------------------- 随机生成

FIRST_NAMES = ["亨利", "艾丽丝", "托马斯", "薇拉", "乔治", "海伦",
               "亚瑟", "多萝西", "沃尔特", "露丝", "弗兰克", "伊迪丝"]
LAST_NAMES = ["布莱克", "沃德", "芬尼根", "马什", "怀特", "克罗夫特",
              "兰德尔", "奥姆斯比", "迪瓦恩", "霍尔", "卡特", "毕晓普"]
OCCUPATIONS = ["记者", "私家侦探", "医生", "教授", "古董商", "神职人员",
               "刑警", "作家", "摄影师", "护士", "律师", "退役军官"]


def _skill_pack(occupation: str) -> list[tuple[str, int]]:
    """按职业给一个合理的技能包。未列出的职业走通用包。"""
    packs: dict[str, list[tuple[str, int]]] = {
        "记者": [("母语", 75), ("图书馆利用", 60), ("话术", 55), ("心理学", 45),
                 ("侦查", 45), ("聆听", 40)],
        "私家侦探": [("侦查", 65), ("心理学", 55), ("聆听", 50), ("射击", 50),
                     ("锁匠", 35), ("潜行", 45)],
        "医生": [("医学", 70), ("急救", 65), ("心理学", 55), ("科学", 55),
                 ("母语", 80), ("精神分析", 45)],
        "教授": [("图书馆利用", 70), ("母语", 85), ("历史", 55), ("神秘学", 45),
                 ("说服", 45), ("心理学", 40)],
        "古董商": [("估价", 65), ("历史", 55), ("图书馆利用", 50), ("说服", 50),
                   ("会计", 45), ("妙手", 30)],
        "神职人员": [("说服", 60), ("心理学", 50), ("母语", 75), ("历史", 45),
                     ("聆听", 40), ("信用评级", 40)],
        "刑警": [("射击", 60), ("侦查", 60), ("恐吓", 50), ("法律", 45),
                 ("聆听", 45), ("闪避", 40)],
        "作家": [("母语", 85), ("图书馆利用", 60), ("心理学", 50), ("历史", 45),
                 ("神秘学", 35), ("说服", 40)],
        "摄影师": [("摄影", 70), ("侦查", 55), ("母语", 60), ("潜行", 45),
                   ("攀爬", 40), ("聆听", 40)],
        "护士": [("急救", 70), ("医学", 50), ("心理学", 55), ("聆听", 45),
                 ("母语", 60), ("说服", 45)],
        "律师": [("法律", 75), ("说服", 65), ("母语", 80), ("会计", 50),
                 ("图书馆利用", 50), ("心理学", 40)],
        "退役军官": [("射击", 65), ("格斗", 55), ("闪避", 50), ("攀爬", 45),
                     ("恐吓", 45), ("母语", 60)],
    }
    return packs.get(occupation, [("母语", 65), ("侦查", 45), ("聆听", 45),
                                  ("心理学", 40), ("说服", 40), ("闪避", 35)])


def generate_investigator(rng: random.Random | None = None,
                          occupation: str | None = None) -> dict[str, Any]:
    rng = rng or random
    occ = occupation or rng.choice(OCCUPATIONS)
    attrs = roll_attributes(rng)
    name = f"{rng.choice(FIRST_NAMES)}·{rng.choice(LAST_NAMES)}"
    return finalize({
        "name": name,
        "occupation": occ,
        "age": rng.randint(24, 55),
        "gender": rng.choice(["男", "女"]),
        "attributes": attrs,
        "skills": _skill_pack(occ),
        "inventory": ["随身衣物", "钱包与证件", "一支手电筒"],
        "backstory": f"一名{occ}，因为一封说不清来历的信来到了这里。",
        "playstyle": rng.choice(["慎重解密流", "主动出击流", "冷静支援流",
                                 "社交试探流", "交易与交涉流"]),
        "player_name": rng.choice(PLAYER_NAMES),
        "table_voice": rng.choice(TABLE_VOICES),
        "traits": [],
    })


PLAYER_NAMES = ["老周", "阿凛", "阿澈", "小满", "阿樱", "大熊", "阿棠", "小鹿", "老K", "阿岚"]

TABLE_VOICES = [
    "说话随意，爱用短句，想到什么说什么，偶尔跑题聊别的。",
    "话不多但一开口就切中要害，喜欢用「嗯」开头。",
    "贫嘴，爱开玩笑，紧张的时候话更多，会用玩笑盖住害怕。",
    "爱较真，喜欢把线索一条条列出来对，掷骰前先算概率。",
    "急性子，爱催促队友，吐槽多但很讲义气。",
]


# ---------------------------------------------------------------- 引擎掷属性

def roll_attributes(rng: random.Random | None = None) -> dict[str, int]:
    """属性由**引擎**掷——骰子不能交给模型，否则车卡就成了编数。"""
    rng = rng or random
    return {
        "STR": rng.randint(3, 18) * 5,
        "CON": rng.randint(3, 18) * 5,
        "DEX": rng.randint(3, 18) * 5,
        "APP": rng.randint(3, 18) * 5,
        "POW": rng.randint(3, 18) * 5,
        "SIZ": (rng.randint(2, 12) + 6) * 5,
        "INT": (rng.randint(2, 12) + 6) * 5,
        "EDU": (rng.randint(2, 12) + 6) * 5,
        "LUCK": rng.randint(3, 18) * 5,
    }


def derive(attrs: dict[str, int]) -> dict[str, Any]:
    return {
        "HP": (attrs.get("CON", 50) + attrs.get("SIZ", 50)) // 10,
        "MP": attrs.get("POW", 50) // 5,
        "SAN": attrs.get("POW", 50),
        "DB": damage_bonus(attrs.get("STR", 50), attrs.get("SIZ", 50)),
        "MOV": mov_rate(attrs.get("STR", 50), attrs.get("DEX", 50), attrs.get("SIZ", 50)),
    }


def base_of(skill_name: str, attrs: dict[str, int]) -> int:
    """技能的基础值——车卡校验用它来算"花掉了多少点"。"""
    if skill_name == "母语":
        return int(attrs.get("EDU", 50))
    if skill_name == "闪避":
        return int(attrs.get("DEX", 50)) // 2
    if skill_name == "克苏鲁神话":
        return 0
    return int(BASE_SKILLS.get(skill_name, 1))


def budget(attrs: dict[str, int]) -> int:
    return int(attrs.get("EDU", 0)) * 4 + int(attrs.get("INT", 0)) * 2


def validate_sheet(sheet: dict[str, Any], attrs: dict[str, int]) -> list[str]:
    """按规则书校验 AI 车出来的卡。返回警告列表（空 = 合规）。"""
    warns: list[str] = []
    if not str(sheet.get("name") or "").strip():
        warns.append("卡上没有名字")
    if not str(sheet.get("occupation") or "").strip():
        warns.append("卡上没有职业")

    raw = sheet.get("skills") or {}
    if not isinstance(raw, dict):
        return warns + ["skills 不是键值对，无法解析"]
    if len(raw) < 6:
        warns.append(f"只写了 {len(raw)} 项技能，正常一张卡至少 8 项")

    spent = 0
    for name, value in raw.items():
        try:
            v = int(value)
        except (TypeError, ValueError):
            warns.append(f"「{name}」的数值不是数字")
            continue
        if name == "克苏鲁神话":
            warns.append("车卡阶段不该分配克苏鲁神话技能")
            continue
        if v > 90:
            warns.append(f"「{name}」{v}% 超过了 90 的合理上限")
        spent += max(0, v - base_of(name, attrs))

    cap = budget(attrs)
    if spent > cap:
        warns.append(f"技能点超支：花掉 {spent} 点，预算只有 {cap} 点")

    credit = sheet.get("credit")
    if credit is not None:
        try:
            c = int(credit)
            if not 0 <= c <= 99:
                warns.append(f"信用评级 {c} 超出 0-99 范围")
        except (TypeError, ValueError):
            warns.append("信用评级不是数字")
    return warns


def build_character_from_sheet(attrs: dict[str, int], sheet: dict[str, Any]) -> dict[str, Any]:
    """把 AI 车好的卡合成为引擎使用的角色结构。"""
    raw = sheet.get("skills") or {}
    skills: list[tuple[str, int]] = []
    if isinstance(raw, dict):
        for name, value in raw.items():
            try:
                skills.append((str(name), int(value)))
            except (TypeError, ValueError):
                continue
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "name" in item:
                try:
                    skills.append((str(item["name"]), int(item.get("value", 0))))
                except (TypeError, ValueError):
                    continue

    if "信用评级" not in {s[0] for s in skills} and sheet.get("credit") is not None:
        try:
            skills.append(("信用评级", int(sheet["credit"])))
        except (TypeError, ValueError):
            pass

    inv = sheet.get("inventory") or []
    if isinstance(inv, str):
        inv = [x.strip() for x in re.split(r"[、,，\n]", inv) if x.strip()]

    try:
        age = int(sheet.get("age") or 30)
    except (TypeError, ValueError):
        age = 30

    return finalize({
        "name": str(sheet.get("name") or "无名调查员").strip(),
        "occupation": str(sheet.get("occupation") or "流浪者").strip(),
        "age": age,
        "gender": str(sheet.get("gender") or "不明").strip(),
        "attributes": dict(attrs),
        "skills": skills,
        "inventory": [str(x).strip() for x in inv if str(x).strip()],
        "backstory": str(sheet.get("backstory") or "").strip(),
        "conditions": [],
    })


# ---------------------------------------------------------------- 购点与年龄

ATTR_ORDER = ["STR", "CON", "DEX", "APP", "POW", "SIZ", "INT", "EDU"]


def pointbuy_warnings(attrs: dict[str, int], total: int = 480,
                      lo: int = 40, hi: int = 90) -> list[str]:
    """校验 COC7 购点分配。幸运不参与购点，永远是单独掷的 3d6×5。"""
    warns: list[str] = []
    got = {k: int(attrs.get(k) or 0) for k in ATTR_ORDER}
    for k, v in got.items():
        if v < lo or v > hi:
            warns.append(f"{k} {v} 超出 {lo}-{hi} 的范围")
    s = sum(got.values())
    if s > total:
        warns.append(f"购点超支：用了 {s} 点，预算 {total} 点")
    elif s < total - 20:      # 允许留一点不花完，但不能空着
        warns.append(f"购点没用完：只用了 {s} 点，预算 {total} 点")
    return warns


# COC7 年龄补正（7e 规则书）
#   (下限, 上限, STR+SIZ 合计, STR+CON+DEX 合计, APP, EDU 增强检定次数, EDU 直扣)
AGE_BANDS: list[tuple[int, int, int, int, int, int, int]] = [
    (15, 19, -5, 0, 0, 0, -5),
    (20, 39, 0, 0, 0, 1, 0),
    (40, 49, 0, -5, -5, 2, 0),
    (50, 59, 0, -10, -10, 3, 0),
    (60, 69, 0, -20, -15, 4, 0),
    (70, 79, 0, -40, -20, 4, 0),
    (80, 89, 0, -80, -25, 4, 0),
]


def _spread(attrs: dict[str, int], keys: list[str], amount: int) -> None:
    """把一笔减值摊到几个属性上，摊不完的从最后一个继续扣（最低扣到 1）。"""
    left = abs(int(amount))
    if left <= 0:
        return
    i = 0
    while left > 0 and i < len(keys) * 40:
        k = keys[i % len(keys)]
        if attrs.get(k, 1) > 1:
            attrs[k] -= 1
            left -= 1
        i += 1
        if all(attrs.get(x, 1) <= 1 for x in keys):
            break


def age_adjust(attrs: dict[str, int], age: int,
               rng: random.Random | None = None) -> tuple[dict[str, int], list[str]]:
    """按 COC7 的年龄补正调整属性，返回 (新属性, 说明)。

    EDU 增强检定：掷 d100 > 当前 EDU 就 +1d10（上限 99）。
    这是规则书里"上了年纪的人更有学问、但身子骨更差"的那条。
    """
    rng = rng or random
    out = dict(attrs)
    notes: list[str] = []
    band = next((b for b in AGE_BANDS if b[0] <= int(age) <= b[1]), None)
    if band is None:
        return out, [f"年龄 {age} 不在年龄补正表里，未做调整"]

    lo, hi, str_siz, str_con_dex, app, checks, edu_flat = band
    if str_siz:
        _spread(out, ["STR", "SIZ"], str_siz)
        notes.append(f"{lo}-{hi} 岁：STR 与 SIZ 合计 {str_siz}")
    if str_con_dex:
        _spread(out, ["STR", "CON", "DEX"], str_con_dex)
        notes.append(f"{lo}-{hi} 岁：STR/CON/DEX 合计 {str_con_dex}")
    if app:
        out["APP"] = max(1, out.get("APP", 1) + app)
        notes.append(f"APP {app}")
    if edu_flat:
        out["EDU"] = max(1, out.get("EDU", 1) + edu_flat)
        notes.append(f"EDU {edu_flat}（太年轻，还没念完书）")

    gained = 0
    for _ in range(checks):
        if rng.randint(1, 100) > out.get("EDU", 0):
            gain = rng.randint(1, 10)
            out["EDU"] = min(99, out.get("EDU", 0) + gain)
            gained += gain
    if checks:
        notes.append(f"EDU 增强检定 {checks} 次" + (f"，共 +{gained}" if gained else "，都没过"))

    out["LUCK"] = int(attrs.get("LUCK") or out.get("LUCK") or 50)
    return out, notes


# ---------------------------------------------------------------- 规范化

def finalize(char: dict[str, Any]) -> dict[str, Any]:
    """补全派生数值（HP/MP/SAN/DB/MOV/闪避）并把技能表转成字典列表。"""
    a = dict(char.get("attributes") or {})
    for k in ("STR", "CON", "DEX", "APP", "POW", "SIZ", "INT", "EDU", "LUCK"):
        a.setdefault(k, 50)
    a["HP"] = int(a.get("HP") or (a["CON"] + a["SIZ"]) // 10)
    a["MAXHP"] = int(a.get("MAXHP") or a["HP"])
    a["MP"] = int(a.get("MP") or a["POW"] // 5)
    a["MAXMP"] = int(a.get("MAXMP") or a["MP"])
    a["SAN"] = int(a.get("SAN") or a["POW"])
    a["MAXSAN"] = int(a.get("MAXSAN") or 99 - 0)
    a["DB"] = a.get("DB") or damage_bonus(a["STR"], a["SIZ"])
    a["MOV"] = int(a.get("MOV") or mov_rate(a["STR"], a["DEX"], a["SIZ"]))

    skills_in = char.get("skills") or []
    skills: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in skills_in:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            nm, val = str(item[0]), int(item[1])
        elif isinstance(item, dict):
            nm, val = str(item.get("name", "")), int(item.get("value", 0))
        else:
            continue
        if nm and nm not in seen:
            skills.append({"name": nm, "value": val})
            seen.add(nm)
    # 若未列闪避，按 DEX//2 补上（COC7 闪避基础值）
    if "闪避" not in seen:
        skills.append({"name": "闪避", "value": a["DEX"] // 2})

    out = dict(char)
    out["attributes"] = a
    out["skills"] = skills
    out.setdefault("inventory", [])
    out.setdefault("conditions", [])
    out.setdefault("playstyle", "均衡稳健流")
    out.setdefault("traits", [])
    return out


def skill_value(char: dict[str, Any], name: str) -> int:
    """查技能值：技能表 → 基础值表 → 属性名（如 STR）→ 默认 5。"""
    name = (name or "").strip()
    for s in char.get("skills") or []:
        if isinstance(s, dict) and s.get("name") == name:
            return int(s.get("value", 0))
    if name in BASE_SKILLS:
        return BASE_SKILLS[name]
    attrs = char.get("attributes") or {}
    for key in (name.upper(), name):
        if key in attrs:
            return int(attrs[key])
    return 5
