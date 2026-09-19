"""句式疲劳：跨人重复的句子开头。

和梗是同一类问题。模型每轮只看得到自己那份历史，
它不知道自己这句「我把椅子挪了挪」跟同桌另外两个人上一轮的句子一模一样。
三个人凑一块儿，就会变成一桌子「我把」。

所以这件事也只能引擎来做。它拿得到全桌的 act，模型拿不到。

**注意**：这里只**报事实**，不下指令。
world message 刻意不含祈使句，对助手下任务会立刻把它拉回助手人格。
所以给模型看的是「这一桌的句子开头撞成了这样」，不是「请你换个说法」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 断句。句号、问号、感叹号、换行都算。
_SENT = re.compile(r"[。！？!?\n]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")

# 只看前两个字。「我把」「我是」「他说」这种两字重复就是卡带。
OPEN_LEN = 2


@dataclass
class _Hit:
    who: str
    opener: str
    round: int


@dataclass
class OpenerWatcher:
    min_hits: int = 3
    window: int = 2
    show: int = 4
    _hits: list[_Hit] = field(default_factory=list)
    _round: int = 0

    def observe(self, who: str, text: str, round_no: int) -> None:
        """记下某个人这一段里每句话的开头。"""
        who = (who or "").strip()
        if not who or not (text or "").strip():
            return
        self._round = max(self._round, int(round_no))
        for seg in _SENT.split(text):
            seg = seg.strip(" ，,、；;：:「」『』“”\"'（）()")
            if len(seg) < OPEN_LEN + 2:
                continue
            head = seg[:OPEN_LEN]
            if len(_CJK.findall(head)) < OPEN_LEN:
                continue
            self._hits.append(_Hit(who, head, self._round))

    def note(self) -> str:
        """这一桌撞得最厉害的那几个开头。够不上就不报。"""
        recent = [h for h in self._hits if self._round - h.round <= self.window]
        # 窗口滚过去的丢掉，不然越攒越多
        self._hits = recent
        if len(recent) < self.min_hits:
            return ""

        count: dict[str, list[str]] = {}
        for h in recent:
            count.setdefault(h.opener, []).append(h.who)

        bad = [(op, whos) for op, whos in count.items()
               if len(whos) >= self.min_hits and len(set(whos)) >= 2]
        if not bad:
            return ""
        bad.sort(key=lambda kv: -len(kv[1]))
        shown = "、".join(f"{op}（{len(whos)} 次）" for op, whos in bad[: self.show])
        return ("[这一桌的句子开头撞了]\n"
                f"{shown}\n"
                "上面这些是这一轮里每句话的头两个字，重复出现的是撞车，不是刻意的。")
