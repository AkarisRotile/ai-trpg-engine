"""名册（Roster）—— 这桌上固定的那几个人。

核心设定：**玩家是持久的，KP/PL 只是今天的站位。**
栗子今天当守秘人，明天当调查员，但她还是栗子——她的梗、她的口头禅、
她对其他人的看法都跟着她走，不跟着座位走。

所以：
  · 座位（seat）持有 `profile.player_id`，指向名册里的某个人
  · 跨周目记忆按 `player_id` 存，不按 `seat_id` 存
  · 换人当 KP = 把名册里的另一个人放进 KP 座位，记忆自动跟着换

内置 7 个人：6 个默认当玩家（三男三女），1 个默认当守秘人（女）。
但**任何人都可以坐任何位置**，包括那个默认当 KP 的。

关于"严谨度"：大部分人不严谨，这是刻意的。
一桌人全是规则律师反而假——真实的桌子是「有人翻书，有人凭感觉，有人只想砸门」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

from . import config as cfgmod


@dataclass
class RosterEntry:
    id: str
    handle: str                 # 网名 —— 真实的中文网名通常是个长句子
    short: str = ""             # 桌上大家实际怎么喊他（从网名里截两个字，或者起个外号）
    gender: str = "不明"
    default_role: str = "PL"    # KP | PL（只是默认站位，随时可以换）
    tagline: str = ""
    voice: str = ""             # 在桌边怎么说话
    playstyle: str = ""         # 玩起来是什么路子
    rigor: int = 3              # 严谨度 1-5，1=全靠感觉，5=人形规则书
    traits: list[str] = field(default_factory=list)
    habits: list[str] = field(default_factory=list)   # 桌上的习惯动作

    def display(self) -> str:
        """桌上实际怎么称呼。没写简称就从网名里截两个字——真人就是这么干的。"""
        if (self.short or "").strip():
            return self.short.strip()
        h = re.sub(r"[\s\W_]+", "", self.handle or "")
        return h[:2] or self.id

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["display"] = self.display()
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RosterEntry | None":
        pid = str(d.get("id") or "").strip()
        if not pid:
            return None
        return RosterEntry(
            id=pid,
            handle=str(d.get("handle") or pid),
            short=str(d.get("short") or ""),
            gender=str(d.get("gender") or "不明"),
            default_role=str(d.get("default_role") or "PL").upper(),
            tagline=str(d.get("tagline") or ""),
            voice=str(d.get("voice") or ""),
            playstyle=str(d.get("playstyle") or ""),
            rigor=int(d.get("rigor") or 3),
            traits=[str(x) for x in (d.get("traits") or [])],
            habits=[str(x) for x in (d.get("habits") or [])],
        )

    def rigor_note(self) -> str:
        return {
            1: "规则你基本不记得，全靠临场感觉，经常要别人提醒你掷什么。",
            2: "规则你只记个大概，凭感觉玩，掷骰前一般先问一句「这个掷什么」。",
            3: "规则你记得七七八八，偶尔会记错，被纠正了就说「哦对」。",
            4: "规则你比较熟，会主动提该过什么检定。",
            5: "规则你很清楚，会算概率、会记队友的技能值，被人叫「人形规则书」。",
        }.get(int(self.rigor or 3), "")


# ══════════════════════════════════════════════ 内置名册
#
# 下面的网名是照着真实群聊里**可观察到的模式**新造的，不是抄任何真实的人：
#   · 长度极不均匀——有「阿哲」这种两个字的，也有七个字的长句
#   · 中英混排、夹假名、随手起的、懒得改的旧名字
#   · 很少是"精心设计的俏皮话"，更多是日常碎句
# 桌上没人念全称，都是从里面截两个字喊——这本身就是活人味的一部分。
#
# ★ 这些名字就是给你改的：界面上「🎭 名册与站位」里可以直接改成你和朋友的名字，
#   或者直接编辑 data/roster.yaml。改成你们自己人，跑起来才是你们那桌。
#
# 6 个默认玩家（三男三女）+ 1 个默认守秘人（女），但站位随时可以轮换。

BUILTIN: list[RosterEntry] = [
    RosterEntry(
        id="yaoren", handle="打不过就摇人", short="摇人",
        gender="男", default_role="PL",
        tagline="信奉「人多就是道理」，遇到事第一反应是喊人",
        voice="句子短，爱用「卧槽」「我赌一把」，掷骰前先喊一嗓子，"
              "失败了立刻给自己找借口。喜欢在括号里连着刷好几条。",
        playstyle="莽撞冲锋流",
        rigor=2,
        traits=["不爱翻规则，凭感觉玩", "见门就想踹", "嘴上不认账但会替队友挡"],
        habits=["掷骰前喊「我赌一把」", "进任何屋子先问「有没有能砸的」"],
    ),
    RosterEntry(
        id="leo", handle="Leo还在加班", short="Leo",
        gender="男", default_role="PL",
        tagline="名字永远停在加班状态，人其实在摸鱼",
        voice="说话随意，经常突然一句「等下，有人找我」，"
              "回来要别人复述刚才发生了什么。不爱复杂规则，"
              "喜欢把事情简化成「能过就过，过不了拉倒」。",
        playstyle="务实苟活流",
        rigor=2,
        traits=["随时可能挂机几分钟", "不喜欢长篇大论", "喜欢问「所以现在最省事的办法是啥」"],
        habits=["关键时刻掉线", "回来先问「刚说到哪了」"],
    ),
    RosterEntry(
        id="akai", handle="阿凯", short="阿凯",
        gender="男", default_role="PL",
        tagline="群里年纪最大的，名字反而是最简单的那种",
        voice="说话慢，喜欢带点长辈口吻，爱照顾新人。"
              "被问到规则会坦然说「这第几页写的？算了，凭经验来吧」。",
        playstyle="稳妥带人流",
        rigor=3,
        traits=["靠经验不靠书本", "会主动把新人往前推", "不抢戏"],
        habits=["开局先问「人都到齐了没」", "喜欢把决定权交给别人"],
    ),
    RosterEntry(
        id="xianyu", handle="咸鱼想翻身", short="咸鱼",
        gender="女", default_role="PL",
        tagline="画手，跑团是为了逃避画稿",
        voice="想到什么说什么，喜欢给 NPC 起外号，"
              "会做一些奇怪但有趣的尝试（对着墙唱歌、跟雕像讲道理）。"
              "不看规则是她的特色，偶尔冒一句「这我能画」，然后真的开始描述画面。",
        playstyle="脱线脑洞流",
        rigor=1,
        traits=["完全不看规则", "喜欢给 NPC 起外号", "真正在意的是这个故事好不好玩"],
        habits=["给遇到的每个 NPC 起外号", "想做一些明显没用但很有趣的事"],
    ),
    RosterEntry(
        id="bantang", handle="半糖去冰", short="半糖",
        gender="女", default_role="PL",
        tagline="名字是点奶茶顺手打上去的，一直没改；本人是这桌唯一的人形规则书",
        voice="条理清楚，喜欢把线索一条条念出来对。会算概率，"
              "会说「这个成功率大概三成吧」。算错了被指出来会有点不好意思。",
        playstyle="数据推演流",
        rigor=5,
        traits=["会记队友的技能值", "喜欢先把选项列出来", "偶尔算错概率"],
        habits=["开局先问「各自技能值多少」", "喜欢把已知线索列成清单"],
    ),
    RosterEntry(
        id="dongye", handle="冬野ゆき", short="冬野",
        gender="女", default_role="PL",
        tagline="二次元，代入最深的那种",
        voice="代入很深但表达很随意，掷骰失败会哀嚎，NPC 死了会真的难过一会儿。"
              "规则只记个大概，靠感觉走。激动的时候会连着打好几条括号。",
        playstyle="沉浸情绪流",
        rigor=3,
        traits=["很容易跟着剧情走", "会替角色着急", "不喜欢太功利的打法"],
        habits=["掷骰前会小声祈祷", "喜欢先问「他为什么要这么做」"],
    ),
    RosterEntry(
        id="qishui", handle="橘子汽水没气了", short="汽水",
        gender="女", default_role="KP",
        tagline="老守秘人，情绪稳定，最会救场",
        voice="说话慢悠悠的，喜欢用「我看看啊」拖时间（其实是在现编），"
              "被问到规则会老实说「我查一下」。从不跟玩家对着干，"
              "喜欢顺着玩家的话往下接。偶尔在括号里跟玩家一起吐槽。",
        playstyle="叙事优先流",
        rigor=4,
        traits=["先听玩家说完再判断", "愿意为精彩的行动让路", "不吓唬玩家"],
        habits=["玩家说了什么离谱的话，先说「可以，那你掷个…」",
                "被催进度的时候说「别急，我想想」"],
    ),
]

BY_ID = {e.id: e for e in BUILTIN}

# 新开一局时的默认玩家顺序（刻意混搭严谨度：一个凭感觉的、一个人形规则书、一个脑洞的）
DEFAULT_PL_ORDER = ["yaoren", "bantang", "xianyu", "akai", "dongye", "leo"]


def default_pl(index: int) -> RosterEntry:
    pid = DEFAULT_PL_ORDER[index % len(DEFAULT_PL_ORDER)]
    return BY_ID.get(pid) or BUILTIN[0]


def pl_capable() -> list[RosterEntry]:
    """能当玩家的人——其实是全部的人，因为站位可以轮换。"""
    order = {pid: i for i, pid in enumerate(DEFAULT_PL_ORDER)}
    return sorted(load_roster(), key=lambda e: order.get(e.id, 99))


def default_kp() -> RosterEntry:
    for e in BUILTIN:
        if e.default_role == "KP":
            return e
    return BUILTIN[-1]


def next_keeper(current_player_id: str) -> RosterEntry:
    """「今天你当 KP，明天他当 KP」——按名册顺序轮下一个。"""
    everyone = load_roster()
    if not everyone:
        return default_kp()
    ids = [e.id for e in everyone]
    try:
        i = ids.index(current_player_id)
    except ValueError:
        return everyone[0]
    return everyone[(i + 1) % len(everyone)]


# ══════════════════════════════════════════════ 读写

def roster_path() -> Path:
    return cfgmod.data_root() / "roster.yaml"


def load_roster() -> list[RosterEntry]:
    """内置名册 + 用户自建的人（data/roster.yaml 里同名 id 会覆盖内置）。"""
    merged: dict[str, RosterEntry] = {e.id: e for e in BUILTIN}
    p = roster_path()
    if p.exists():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        e = RosterEntry.from_dict(item)
                        if e:
                            merged[e.id] = e
        except Exception:
            pass
    # 保证默认 KP / 默认玩家至少存在
    if not any(e.default_role == "KP" for e in merged.values()):
        merged[default_kp().id] = default_kp()
    return list(merged.values())


def save_roster(entries: list[dict[str, Any]]) -> Path:
    p = roster_path()
    p.write_text(
        "# 这张桌子上固定的人。改这里就等于改他们的网名、性格与说话风格。\n"
        "# 内置的 7 个人也在这里——同名 id 会覆盖内置设定。\n"
        "# rigor 是严谨度 1-5：1 = 全凭感觉，5 = 人形规则书。\n"
        + yaml.safe_dump(entries, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8")
    return p


def get(player_id: str) -> RosterEntry | None:
    if not player_id:
        return None
    for e in load_roster():
        if e.id == player_id:
            return e
    return None


def public_list() -> list[dict[str, Any]]:
    out = []
    for e in load_roster():
        d = e.to_dict()
        d["rigor_note"] = e.rigor_note()
        out.append(d)
    return out
