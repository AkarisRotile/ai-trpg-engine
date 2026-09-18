"""调查员生成：预设法 + 随机掷骰法。

COC7 属性生成：STR/CON/DEX/APP/POW = 3d6×5，SIZ/INT = (2d6+6)×5，EDU = (2d6+6)×5。
派生：HP=(CON+SIZ)//10，MP=POW//5，SAN=POW，DB 由 STR+SIZ 推出。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
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


# ══════════════════════════════════════════════════════════════ 车卡规范
#
# 车卡是整个引擎里**唯一一处让 AI 自己算数字**的地方：属性是引擎掷的、
# 骰子是引擎掷的、伤害是引擎算的，只有技能点分配交给了 AI。
# 而模型算加法非常不可靠——它会给自己多分几十点、把技能顶到 95%，
# 而且自己不觉得有问题。所以规矩必须由代码兜住。
#
# 完整规范见 docs/车卡规范.md；这里和那份文档必须一致。

SKILL_CAP = 90          # 车卡阶段单项上限（规则书：创建时不得超过 90）
CREDIT = "信用评级"

# 本职技能表里没有、但永远算"本职"的几项（母语是所有职业的共同底子）
_COMMON_OCC_SKILLS = ("母语",)


@dataclass(frozen=True)
class OccupationSpec:
    """一个职业的车卡参数。

    formula 是职业技能点的算法：`(("EDU", 4),)` 表示 EDU×4；
    刑警这类有两个属性的是 `(("EDU", 2), ("DEX", 2))`。
    credit 是这个职业允许的信用评级区间，CR 的点数从职业点里出。
    """
    name: str
    formula: tuple[tuple[str, int], ...] = (("EDU", 4),)
    credit: tuple[int, int] = (0, 99)
    skills: tuple[str, ...] = ()


# 表里没有的职业退回 EDU×4 / CR 0-99，并且跳过"本职技能"那两条检查——
# 无从判断哪些算本职，硬判会冤枉人。
OCCUPATION_SPECS: dict[str, OccupationSpec] = {
    "记者": OccupationSpec("记者", credit=(9, 30), skills=(
        "母语", "图书馆利用", "话术", "心理学", "侦查", "聆听", "摄影", "历史")),
    "私家侦探": OccupationSpec(
        "私家侦探", formula=(("EDU", 2), ("DEX", 2)), credit=(9, 30), skills=(
            "侦查", "心理学", "聆听", "射击", "法律", "图书馆利用",
            "锁匠", "潜行", "话术", "会计")),
    "医生": OccupationSpec("医生", credit=(30, 80), skills=(
        "医学", "急救", "心理学", "科学", "母语", "精神分析",
        "生物学", "化学", "药学", "信用评级")),
    "教授": OccupationSpec("教授", credit=(20, 70), skills=(
        "图书馆利用", "母语", "历史", "神秘学", "说服", "心理学",
        "考古学", "人类学", "科学")),
    "古董商": OccupationSpec("古董商", credit=(30, 50), skills=(
        "估价", "历史", "图书馆利用", "说服", "会计", "妙手",
        "侦查", "话术", "信用评级")),
    "神职人员": OccupationSpec("神职人员", credit=(9, 60), skills=(
        "说服", "心理学", "母语", "历史", "聆听", "图书馆利用", "信用评级")),
    "刑警": OccupationSpec(
        "刑警", formula=(("EDU", 2), ("DEX", 2)), credit=(20, 50), skills=(
            "射击", "侦查", "恐吓", "法律", "聆听", "闪避", "心理学",
            "追踪", "格斗", "信用评级")),
    "作家": OccupationSpec("作家", credit=(9, 30), skills=(
        "母语", "图书馆利用", "心理学", "历史", "神秘学", "说服", "侦查")),
    "摄影师": OccupationSpec("摄影师", credit=(9, 30), skills=(
        "摄影", "侦查", "母语", "潜行", "攀爬", "聆听", "心理学", "化学")),
    "护士": OccupationSpec("护士", credit=(9, 30), skills=(
        "急救", "医学", "心理学", "聆听", "母语", "说服", "科学", "精神分析")),
    "律师": OccupationSpec("律师", credit=(30, 80), skills=(
        "法律", "说服", "母语", "会计", "图书馆利用", "心理学",
        "话术", "信用评级")),
    "退役军官": OccupationSpec(
        "退役军官", formula=(("EDU", 2), ("STR", 2)), credit=(20, 70), skills=(
            "射击", "格斗", "闪避", "攀爬", "恐吓", "母语", "急救",
            "导航", "信用评级")),
}


def occ_spec(occupation: str | None) -> OccupationSpec:
    """查职业参数。表里没有就给一个最保守的默认（并标记为"不认识"）。"""
    name = (occupation or "").strip()
    return OCCUPATION_SPECS.get(name) or OccupationSpec(name or "（未填职业）")


# 卡上的技能名和引擎里的写法不完全一样，对不上就会把本职技能判成非本职，
# 于是"兴趣点超支"满屏乱报。这张表把两边对齐。
SKILL_ALIASES: dict[str, str] = {
    "图书馆使用": "图书馆利用",
    "斗殴": "格斗",
    "手枪": "射击",
    "步枪": "射击",
    "霰弹枪": "射击",
    "冲锋枪": "射击",
    "机枪": "射击",
    "重武器": "射击",
    "撬锁": "锁匠",
    "开锁": "锁匠",
    "汽车驾驶": "汽车驾驶",
    "驾驶": "汽车驾驶",
    "自然学": "博物学",
    "领航": "导航",
    "信誉": "信用评级",
    "信用": "信用评级",
    "魅惑": "取悦",
    "闪避": "闪避",
    "计算机使用": "计算机",
    "电脑": "计算机",
}


def same_skill(a: str, b: str) -> bool:
    """两个技能名是不是同一个（跨卡/引擎两套命名）。"""
    na = _norm_skill_name(a)
    nb = _norm_skill_name(b)
    if not na or not nb:
        return False
    return na == nb or SKILL_ALIASES.get(na, na) == SKILL_ALIASES.get(nb, nb)


def _norm_skill_name(name: str) -> str:
    import re as _re
    return _re.sub(r"[：:、,，.。\s（）()①②③④⑤⑥⑦⑧⑨]+", "",
                   (name or "").strip())


def _is_occupation_skill(name: str, occ_skills: list[str]) -> bool:
    if not occ_skills:
        return True                      # 没有本职清单就不限制
    if name == CREDIT or name in _COMMON_OCC_SKILLS:
        return True
    return any(same_skill(name, s) for s in occ_skills)


def budget_split(attrs: dict[str, int], occupation: str | None = None) -> dict[str, Any]:
    """把预算拆成**两笔账**。

    职业技能点只能花在本职技能上，兴趣技能点随便花——这是规则书里
    最容易被模型忽略的一条，所以引擎自己算，不交给它。

    **优先用用户那张卡**（`职业列表` 里的公式、信用区间、本职技能矩阵），
    卡读不到才退回引擎里内置的那张表。卡才是权威：
    引擎一开始就猜错了好几处（记者其实是「教育×2＋外貌或敏捷×2」）。
    """
    name = (occupation or "").strip()
    from . import cardcalc

    occ = cardcalc.find_occupation(name) if name else None
    if occ is not None:
        return {
            "occ": int(occ.points(attrs)),
            "interest": int(attrs.get("INT", 0)) * 2,
            "total": int(occ.points(attrs)) + int(attrs.get("INT", 0)) * 2,
            "formula": occ.formula_text or "教育×4",
            "credit": [int(occ.credit[0]), int(occ.credit[1])],
            "occ_skills": list(occ.skills),
            "known_occupation": True,
            "occupation": occ.name,
            "source": "card",
        }

    spec = occ_spec(name)
    known = spec.name in OCCUPATION_SPECS
    if known:
        occ_points = sum(int(attrs.get(a, 0)) * m for a, m in spec.formula)
        formula_txt = " + ".join(f"{a}×{m}" for a, m in spec.formula)
    else:
        occ_points = int(attrs.get("EDU", 0)) * 4
        formula_txt = "EDU×4（这个职业不在表里，按通用公式算）"
    interest_points = int(attrs.get("INT", 0)) * 2
    return {
        "occ": int(occ_points),
        "interest": int(interest_points),
        "total": int(occ_points) + int(interest_points),
        "formula": formula_txt,
        "credit": list(spec.credit),
        "occ_skills": list(spec.skills) if known else [],
        "known_occupation": known,
        "occupation": spec.name,
        "source": "builtin" if known else "default",
    }


def _skill_items(sheet: dict[str, Any]) -> list[tuple[str, int]]:
    """把卡上的技能表读成 [(名字, 数值)]，坏值直接丢掉。"""
    raw = sheet.get("skills") or {}
    items: list[tuple[str, int]] = []
    seen: set[str] = set()
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        pairs = []
        for item in raw:
            if isinstance(item, dict) and "name" in item:
                pairs.append((item.get("name"), item.get("value", 0)))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((item[0], item[1]))
    else:
        pairs = []
    for name, value in pairs:
        nm = str(name or "").strip()
        if not nm or nm in seen:
            continue
        try:
            items.append((nm, int(float(value))))
        except (TypeError, ValueError):
            continue
        seen.add(nm)
    return items


def audit_sheet(sheet: dict[str, Any], attrs: dict[str, int],
                occupation: str | None = None) -> dict[str, Any]:
    """按规范逐条审。返回结构化结果，**既给回炉提示词用，也给界面用**。

    和旧版的区别：这里把"职业点/兴趣点"分开算，所以能抓到
    「拿职业点去点本职之外的技能」这种以前根本看不见的超支。
    """
    occ = (occupation or sheet.get("occupation") or "").strip()
    b = budget_split(attrs, occ)
    items = _skill_items(sheet)
    credit_val = None
    try:
        if sheet.get("credit") is not None:
            credit_val = int(float(sheet.get("credit")))
    except (TypeError, ValueError):
        credit_val = None

    violations: list[dict[str, Any]] = []
    notes: list[str] = []
    cap = SKILL_CAP

    if not str(sheet.get("name") or "").strip():
        violations.append({"code": "no_name", "detail": "卡上没有名字"})
    if not occ:
        violations.append({"code": "no_occ", "detail": "卡上没有职业"})
    if len(items) < 6:
        notes.append(f"只写了 {len(items)} 项技能，正常一张卡至少 8 项")

    spent_occ_skills = 0        # 花在本职技能上的点数
    spent_other = 0             # 花在本职之外的技能上的点数
    for name, value in items:
        base = base_of(name, attrs)
        if name == "克苏鲁神话":
            # 列在卡上、值为 0 是正常的（标准卡就有这一栏）；
            # 分配过点数才是违规。
            if value > 0:
                violations.append({"code": "mythos", "skill": name, "value": value,
                                   "detail": "车卡阶段不允许分配克苏鲁神话"})
            continue
        if value > cap:
            violations.append({"code": "over_cap", "skill": name, "value": value,
                               "cap": cap,
                               "detail": f"「{name}」{value}% 超过车卡上限 {cap}%"})
        if value < base:
            violations.append({"code": "below_base", "skill": name, "value": value,
                               "base": base,
                               "detail": f"「{name}」{value}% 低于它的基础值 {base}%"})
        alloc = max(0, min(value, cap) - base)
        is_occ = _is_occupation_skill(name, b["occ_skills"]) \
            if b["known_occupation"] else True
        if is_occ:
            spent_occ_skills += alloc
        else:
            spent_other += alloc

    spent = spent_occ_skills + spent_other

    # 信用评级
    lo, hi = b["credit"]
    if credit_val is None and CREDIT not in {n for n, _ in items}:
        violations.append({"code": "no_credit",
                           "detail": f"没有写信用评级（{occ or '这个职业'}要求 "
                                     f"{lo}–{hi}）"})
    elif credit_val is not None:
        if not (lo <= credit_val <= hi):
            violations.append({"code": "credit_range", "value": credit_val,
                               "range": [lo, hi],
                               "detail": f"信用评级 {credit_val} 不在 "
                                         f"{occ or '本职'}的 {lo}–{hi} 区间内"})

    if b["known_occupation"]:
        # **只查得动这一条**：花在本职之外的技能上的点数，只可能来自兴趣点。
        # 反过来说，「本职技能花掉多少」是**推不出来**的——兴趣点本来就可以
        # 点本职技能，所以没法从最终值反推哪笔账付的。硬判会冤枉人，
        # 这一条就不查了（要查得让 AI 额外申报每点的来源，代价大、还容易解析失败）。
        if spent_other > b["interest"]:
            violations.append({
                "code": "interest_over", "spent": spent_other,
                "budget": b["interest"],
                "detail": f"兴趣点超支：本职之外的技能花掉 {spent_other} 点，"
                          f"兴趣点只有 {b['interest']} 点"
                          f"（这些点数只可能来自兴趣点）"})

    if spent > b["total"]:
        violations.append({
            "code": "total_over", "spent": spent, "budget": b["total"],
            "detail": f"技能点超支：合计花掉 {spent} 点，"
                      f"预算只有 {b['total']} 点（职业 {b['occ']} + 兴趣 {b['interest']}）"})
    elif spent < b["total"]:
        notes.append(f"还剩 {b['total'] - spent} 点没用（不违规，想用满可以用满）")

    return {
        "ok": not violations,
        "violations": violations,
        "notes": notes,
        "spent": spent,
        "spent_occ_skills": spent_occ_skills,
        "spent_other": spent_other,
        "budget": b,
        "credit": credit_val,
        "skills": items,
        "cap": cap,
    }


def validate_sheet(sheet: dict[str, Any], attrs: dict[str, int],
                   occupation: str | None = None) -> list[str]:
    """按规范校验，返回人话警告列表（空 = 合规）。"""
    audit = audit_sheet(sheet, attrs, occupation)
    return [v["detail"] for v in audit["violations"]] + list(audit["notes"])


def allocate(sheet: dict[str, Any], attrs: dict[str, int],
             occupation: str | None = None) -> tuple[dict[str, Any], list[str],
                                                     dict[str, Any]]:
    """把 AI 想玩的技能**缩放到预算之内**，并算清每个点数出自哪本账。

    这里最要紧的一件事：**超支在结构上不可能发生。**

    以前是让 AI 直接输出"最终技能值"，还要求它自己把总数凑进预算——
    而模型算加法非常不稳（实测超支 60~90 点是常态）。于是引擎只能事后
    检测＋裁剪，把 AI 精心想的分配削成一堆没意义的小数字。

    现在的契约反过来了：**AI 写的是"我想要多少"，引擎负责把它变成"能有多少"**。
    两本账各自等比缩放（本职技能用职业点、其余用兴趣点），
    缩放之后再按最大余数法补整，保证总数刚好落在预算内。
    AI 的**相对意图**（哪个技能是主力、哪个只是点缀）完整保留，
    它不需要做任何算术——这跟骰子和时间一样，本来就不该交给它。

    返回 (卡, 修正说明, 分配表)。分配表直接交给 sheet.write_sheet。
    """
    occ = (occupation or sheet.get("occupation") or "").strip()
    b = budget_split(attrs, occ)
    fixes: list[str] = []

    # ---- 1. 读 AI 的意向：数字或优先级词都行 ----
    items = _skill_items(sheet)
    vals: dict[str, int] = {}
    for name, value in items:
        if name == "克苏鲁神话":
            vals[name] = 0
            continue
        base = base_of(name, attrs)
        vals[name] = max(base, min(int(value), SKILL_CAP))
    if not any(same_skill(n, "闪避") for n in vals):
        vals["闪避"] = base_of("闪避", attrs)

    # ---- 2. 信用评级：夹进职业区间，它固定吃职业点（不参与缩放）----
    # 区间上界也要服从 90 上限：有些职业卡里写的是 0–99（比如表里没有的职业），
    # 直接用就会冒出一个 99% 的信用评级，比车卡上限还高。
    lo, hi = b["credit"]
    hi = min(int(hi), SKILL_CAP)
    lo = min(int(lo), hi)
    cr_name = next((n for n in vals if same_skill(n, CREDIT)), CREDIT)
    raw_cr = vals.get(cr_name)
    if raw_cr is None:
        try:
            raw_cr = int(float(sheet.get("credit"))) \
                if sheet.get("credit") is not None else None
        except (TypeError, ValueError):
            raw_cr = None
    if raw_cr is None or not (lo <= int(raw_cr) <= hi):
        want = lo if raw_cr is None else min(max(int(raw_cr), lo), hi)
        if raw_cr is None:
            fixes.append(f"补上信用评级 {want}（{occ or '本职'}的区间是 {lo}–{hi}）")
        else:
            fixes.append(f"信用评级 {raw_cr} → {want}"
                         f"（{occ or '本职'}的区间是 {lo}–{hi}）")
        raw_cr = want
    vals[cr_name] = int(raw_cr)

    def is_occ_skill(name: str) -> bool:
        return _is_occupation_skill(name, b["occ_skills"]) \
            if b["known_occupation"] else True

    def want_of(name: str) -> int:
        """这个技能希望投入多少点（不含基础值）。信用评级不参与缩放。"""
        return max(0, vals[name] - base_of(name, attrs))

    # ---- 3. 两本账各自缩放 ----
    cr_want = want_of(cr_name)
    occ_others = [n for n in vals if n != cr_name and is_occ_skill(n)]
    others = [n for n in vals if n != cr_name and not is_occ_skill(n)]

    occ_room = max(0, b["occ"] - cr_want)          # 扣掉 CR 之后还剩多少职业点
    occ_demand = sum(want_of(n) for n in occ_others)
    int_room = b["interest"]
    int_demand = sum(want_of(n) for n in others)

    def scale(names: list[str], demand: int, room: int) -> None:
        if demand <= 0:
            return
        factor = min(1.0, room / demand)
        if factor >= 1.0:
            return
        raw = {n: want_of(n) * factor for n in names}
        got = _largest_remainder(raw, room)
        for n in names:
            base = base_of(n, attrs)
            newv = base + got.get(n, 0)
            if newv != vals[n]:
                vals[n] = newv
        fixes.append(f"{'职业' if names is occ_others else '兴趣'}点不够"
                     f"（想要 {demand}，只有 {room}），已等比缩放到预算内")

    scale(occ_others, occ_demand, occ_room)
    scale(others, int_demand, int_room)

    # 兴趣点还有富余的话，把被职业点挤下去的本职技能补回来（不超它原本的意愿）
    left_int = int_room - sum(want_of(n) for n in others)
    if left_int > 0 and occ_others:
        short = sorted(occ_others, key=lambda n: -want_of(n))
        for n in short:
            if left_int <= 0:
                break
            room_n = min(SKILL_CAP, vals[n] + left_int) - vals[n]
            if room_n > 0:
                take = min(room_n, left_int)
                vals[n] += take
                left_int -= take

    # ---- 4. 算清每个点出自哪本账 ----
    order = sorted(vals, key=lambda n: (0 if same_skill(n, CREDIT) else
                                        1 if same_skill(n, "母语") else 2, n))
    occ_left, int_left = b["occ"], b["interest"]
    plan: dict[str, dict[str, int]] = {}
    for name in order:
        base = base_of(name, attrs)
        need = vals[name] - base
        o = i = 0
        if is_occ_skill(name) or same_skill(name, CREDIT):
            o = min(need, occ_left)
            occ_left -= o
            need -= o
        i = min(need, int_left)
        int_left -= i
        need -= i
        if need > 0:                    # 理论上到不了这儿
            vals[name] -= need
            fixes.append(f"预算不够，「{name}」再降 {need} → {vals[name]}")
        plan[name] = {"base": base, "occ": o, "interest": i}

    used_occ = sum(p["occ"] for p in plan.values())
    used_int = sum(p["interest"] for p in plan.values())

    out_sheet = dict(sheet)
    out_sheet["skills"] = {n: vals[n] for n in sorted(vals)}
    out_sheet["credit"] = vals.get(cr_name, lo)
    out_sheet["occupation"] = occ or out_sheet.get("occupation") or "流浪者"
    report = {
        "occupation": out_sheet["occupation"],
        "budget": b,
        "credit": out_sheet["credit"],
        "credit_range": [lo, hi],
        "rows": plan,
        "used_occ": used_occ,
        "used_interest": used_int,
        "left_occ": b["occ"] - used_occ,
        "left_interest": b["interest"] - used_int,
        "scaled": bool(occ_demand > occ_room or int_demand > int_room),
    }
    return out_sheet, fixes, report


def _largest_remainder(weights: dict[str, float], total: int) -> dict[str, int]:
    """把 total 点按权重切开，整数、和刚好等于 total（最大余数法）。

    这么做是为了**确定性**：同样的输入必须得到同样的输出，
    不能因为浮点误差每次开团分出来的点数都不一样。
    """
    if total <= 0 or not weights:
        return {k: 0 for k in weights}
    s = sum(weights.values())
    if s <= 0:
        return {k: 0 for k in weights}
    exact = {k: v * total / s for k, v in weights.items()}
    out = {k: int(v) for k, v in exact.items()}
    short = total - sum(out.values())
    if short > 0:
        order = sorted(exact, key=lambda k: (-(exact[k] - int(exact[k])), k))
        for k in order[:short]:
            out[k] += 1
    return out


def repair_sheet(sheet: dict[str, Any], attrs: dict[str, int],
                 occupation: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """裁剪并规范化（只要卡和修正说明）。"""
    fixed, fixes, _plan = allocate(sheet, attrs, occupation)
    return fixed, fixes


def _repair_sheet_legacy(sheet: dict[str, Any], attrs: dict[str, int],
                         occupation: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """旧版按"总账"削的裁剪，留着做对照。"""
    occ = (occupation or sheet.get("occupation") or "").strip()
    b = budget_split(attrs, occ)
    items = _skill_items(sheet)
    fixes: list[str] = []

    # 1-3：逐项夹
    clamped: list[tuple[str, int]] = []
    for name, value in items:
        if name == "克苏鲁神话":
            if value != 0:
                fixes.append(f"「{name}」{value} → 0（车卡阶段不分配神话技能）")
            clamped.append((name, 0))
            continue
        base = base_of(name, attrs)
        v = value
        if v < base:
            fixes.append(f"「{name}」{v} → {base}（不得低于基础值）")
            v = base
        if v > SKILL_CAP:
            fixes.append(f"「{name}」{v} → {SKILL_CAP}（超过车卡上限 {SKILL_CAP}）")
            v = SKILL_CAP
        clamped.append((name, v))

    if not any(n == "闪避" for n, _ in clamped):
        clamped.append(("闪避", base_of("闪避", attrs)))

    # 4：信用评级
    lo, hi = b["credit"]
    have_credit = any(n == CREDIT for n, _ in clamped)
    raw_credit = sheet.get("credit")
    credit_val: int | None = None
    if have_credit:
        credit_val = dict(clamped)[CREDIT]
    elif raw_credit is not None:
        try:
            credit_val = int(float(raw_credit))
        except (TypeError, ValueError):
            credit_val = None
    if credit_val is None:
        credit_val = lo
        fixes.append(f"补上信用评级 {credit_val}（{occ or '本职'}的区间是 {lo}–{hi}，"
                     f"点数从职业点里出）")
    elif not (lo <= credit_val <= hi):
        fixed = min(max(credit_val, lo), hi)
        fixes.append(f"信用评级 {credit_val} → {fixed}（{occ or '本职'}的区间是 {lo}–{hi}）")
        credit_val = fixed
    clamped = [(n, v) for n, v in clamped if n != CREDIT]
    clamped.append((CREDIT, credit_val))

    # 5：削超支。
    #    注意是**两本分账各自削**——只按总账削的话，
    #    「职业点超了、兴趣点还剩一大半」这种最常见的情况根本削不到。
    vals: dict[str, int] = dict(clamped)

    def alloc_of(name: str) -> int:
        return max(0, vals[name] - base_of(name, attrs))

    def shave(candidates: list[str], excess: int) -> int:
        """从投得最多的开始削，削到刚好用完。信用评级不动（那是职业身份）。"""
        order = sorted([n for n in candidates if n != CREDIT],
                       key=lambda n: (-alloc_of(n), n))
        for name in order:
            if excess <= 0:
                break
            take = min(alloc_of(name), excess)
            if take <= 0:
                continue
            before = vals[name]
            vals[name] = before - take
            fixes.append(f"超支 {take} 点，从「{name}」{before} → {vals[name]}")
            excess -= take
        return excess

    occ_names = set(b["occ_skills"]) | set(_COMMON_OCC_SKILLS) | {CREDIT}
    if b["known_occupation"]:
        # 只有"本职之外的技能"这条查得动（见 audit_sheet 里的说明）
        others = [n for n in vals
                  if not _is_occupation_skill(n, b["occ_skills"])]
        over = sum(alloc_of(n) for n in others) - b["interest"]
        if over > 0:
            shave(others, over)
    # 兜底：总账
    over = sum(alloc_of(n) for n in vals) - b["total"]
    if over > 0:
        shave(list(vals), over)

    # 削完再核一遍；万一还有漏网的账，从总账再削一轮
    for _ in range(3):
        left = audit_sheet({"occupation": occ, "credit": vals.get(CREDIT),
                            "skills": vals}, attrs, occ)
        codes = {v["code"] for v in left["violations"]}
        if not (codes & {"total_over", "interest_over"}):
            break
        over = max(0, sum(alloc_of(n) for n in vals)
                   - (b["occ"] + b["interest"]))
        if over <= 0:
            break
        shave(list(vals), over)

    clamped = sorted(vals.items(), key=lambda x: x[0])
    out_sheet = dict(sheet)
    out_sheet["skills"] = {n: v for n, v in sorted(clamped, key=lambda x: x[0])}
    out_sheet["credit"] = credit_val
    out_sheet["occupation"] = occ or out_sheet.get("occupation") or "流浪者"
    return out_sheet, fixes


def repair_character(char: dict[str, Any], attrs: dict[str, int],
                     occupation: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """给已经成型的角色结构做同一套裁剪（技能表是 [{name,value}] 那种）。"""
    items = [(str(s.get("name", "")), int(s.get("value", 0)))
             for s in (char.get("skills") or []) if isinstance(s, dict)]
    sheet = {"name": char.get("name"), "occupation": occupation or char.get("occupation"),
             "credit": None, "skills": dict(items)}
    fixed, notes = repair_sheet(sheet, attrs, occupation)
    skills = [{"name": n, "value": v} for n, v in (fixed.get("skills") or {}).items()]
    out = dict(char)
    out["skills"] = skills
    if fixed.get("occupation"):
        out["occupation"] = fixed["occupation"]
    return out, notes


def build_character_from_sheet(attrs: dict[str, int], sheet: dict[str, Any]) -> dict[str, Any]:
    """把 AI 车好的卡合成为引擎使用的角色结构。

    **先裁剪再合成**：进场的卡一定合法，不存在"警告一下但照样开跑"。
    """
    sheet, _fixes = repair_sheet(sheet, attrs, sheet.get("occupation"))
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
