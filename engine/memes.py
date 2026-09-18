"""桌上的梗，由引擎自己数出来。

梗的定义是**跨人的重复**：同一句短话被两个以上不同的人说过。
这件事只能由引擎来做，因为模型每轮只看得到自己那份历史和一张索引，
它压根不知道「这句话刚才别人也说过」。

和「骰子不服务于剧情」是同一个道理：模型算不好的、看不见的，
都交给代码，别指望提示词求它。

只管**捡**，不管用。捡到的梗写进玩家卡（跨周目），
下一次开团它会出现在这个人的 system 提示里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# 切句。标点、空白、括号、各种引号都算断点。
_SPLIT = re.compile(r"[，。！？、；：,.!?;:\s（）()\[\]【】「」『』“”\"'~～…—]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")
# 笑声和语气词。叠字笑声（哈哈哈哈哈）长度够、汉字也够，
# 不专门挡一下就会被当成梗收进来。
_NOISE = re.compile(r"^[哈呵嘿嘻嘎呜喵嘤啊嗯哦噢额诶呀唉嗨]+$")

# 谁都说的口水词。两个人都说过也不算梗，那只是口头语。
STOP = {
    "真的假的", "哈哈哈哈", "哈哈哈", "什么意思", "为什么", "不知道",
    "可以可以", "这个这个", "什么鬼", "笑死我了", "不是吧",
    "我也不知道", "有点意思", "你说呢", "好好好", "对对对",
}


@dataclass
class Sighting:
    who: str
    round: int


class MemeWatcher:
    """盯一段时间内跨人重复的短句。

    `window` 是往前看几轮。太短会漏掉隔了一轮才被接上的梗，
    太长会把「这局常说的话」当成梗收进来。
    """

    def __init__(self, window: int = 3, min_speakers: int = 2,
                 min_len: int = 3, max_len: int = 12) -> None:
        self.window = int(window)
        self.min_speakers = max(2, int(min_speakers))
        self.min_len = int(min_len)
        self.max_len = int(max_len)
        self._seen: dict[str, list[Sighting]] = {}
        self._done: set[str] = set()
        self._round = 0

    # ---------------------------------------------------------- 记

    def observe(self, who: str, text: str, round_no: int) -> None:
        """记下某个人在这一轮说过的短句。"""
        who = (who or "").strip()
        if not who or not (text or "").strip():
            return
        self._round = max(self._round, int(round_no))
        for phrase in self._phrases(text):
            self._seen.setdefault(phrase, []).append(Sighting(who, self._round))

    def _phrases(self, text: str) -> list[str]:
        out: list[str] = []
        for seg in _SPLIT.split(text or ""):
            seg = seg.strip()
            if not (self.min_len <= len(seg) <= self.max_len):
                continue
            # 至少要够多的汉字。纯符号、纯数字、英文缩写都不算。
            if len(_CJK.findall(seg)) < self.min_len:
                continue
            if seg in STOP:
                continue
            # 纯叠字（绷绷绷）和笑声语气词（哈哈哈哈哈）都不是梗
            if len(set(seg)) <= 1 or _NOISE.match(seg):
                continue
            out.append(seg)
        return out

    # ---------------------------------------------------------- 收

    def harvest(self) -> list[str]:
        """把这一轮够格的重复收成梗。同一句一辈子只收一次。"""
        fired: list[str] = []
        for phrase, hits in list(self._seen.items()):
            if phrase in self._done:
                continue
            recent = [h for h in hits if self._round - h.round <= self.window]
            if len({h.who for h in recent}) >= self.min_speakers:
                self._done.add(phrase)
                fired.append(phrase)
        # 滚出窗口的候选丢掉，不然越攒越多
        self._seen = {
            p: [h for h in hs if self._round - h.round <= self.window]
            for p, hs in self._seen.items()
        }
        self._seen = {p: hs for p, hs in self._seen.items() if hs}
        return fired

    # ---------------------------------------------------------- 看

    def pending(self) -> list[str]:
        """快够格但还没够的候选。调试和界面用。"""
        return [p for p, hs in self._seen.items()
                if len({h.who for h in hs if self._round - h.round <= self.window}) >= 2]
