"""桌上的钟。

模型对「时间」这种连续量很不敏感：它会把所有过去的事都叫「昨天」，
把「三天前」和「上周」混着用，日期加减基本靠猜。
可模组里时间往往是关键——今晚几点举行仪式、开车过去要多久、
尸检报告是哪天签的字、你们已经在这栋房子里待了几天。

所以引擎自己拿一只钟，并且**替模型把相对说法算好**，直接摆一张对照表：

    现在是 1925 年 10 月 3 日（星期六）晚上 21:15
    · 10月2日 = 昨天
    · 10月1日 = 前天   ← 你们就是这天到的
    · 9月30日 = 3 天前
    · 10月4日 = 明天

模型要做的只是**查表**，不是做算术。这是这个模块存在的全部理由。

时间由引擎掌管：守秘人只能说「过了两小时」「等到天亮」，
说完了引擎去拨钟，而不是让模型自己在叙述里瞎编一个日期出来。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_CN_DIGIT = "零一二三四五六七八九"


def cn_num(n: int) -> str:
    """0-99 的中文写法（够用了：天数、小时数不会更大）。"""
    n = int(n)
    if n < 0:
        return "-" + cn_num(-n)
    if n < 10:
        return _CN_DIGIT[n]
    if n < 20:
        return "十" + (_CN_DIGIT[n % 10] if n % 10 else "")
    return _CN_DIGIT[n // 10] + "十" + (_CN_DIGIT[n % 10] if n % 10 else "")


def time_of_day(hour: int) -> str:
    """几点钟的说法。这个也是模型最容易写歪的地方之一。"""
    if 0 <= hour < 5:
        return "凌晨"
    if 5 <= hour < 8:
        return "清晨"
    if 8 <= hour < 11:
        return "上午"
    if 11 <= hour < 13:
        return "中午"
    if 13 <= hour < 17:
        return "下午"
    if 17 <= hour < 19:
        return "傍晚"
    if 19 <= hour < 23:
        return "晚上"
    return "深夜"


def rel_label(days: int) -> str:
    """把「差几天」翻成桌面上真会说的那个词。days>0 表示过去。"""
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    if days <= 9:
        return f"{cn_num(days)}天前"
    if days <= 13:
        return "一周多以前"
    if days <= 20:
        return "两周以前"
    if days <= 27:
        return "三周以前"
    if days <= 59:
        return "一个多月前"
    if days <= 330:
        return f"约 {max(2, round(days / 30))} 个月前"
    return f"约 {max(1, round(days / 365))} 年前"


# ---------------------------------------------------------------- 解析

_UNITS = {
    "m": 1, "min": 1, "分钟": 1, "分": 1,
    "h": 60, "hr": 60, "hour": 60, "小时": 60, "钟头": 60,
    "d": 1440, "day": 1440, "天": 1440, "日": 1440,
    "w": 10080, "周": 10080, "星期": 10080, "礼拜": 10080,
}
_DUR_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*([a-zA-Z\u4e00-\u9fff]{1,3})")
_ABS_RE = re.compile(
    r"(?:(\d{4})\s*[年\-/])?\s*(\d{1,2})\s*[月\-/]\s*(\d{1,2})\s*[日号]?"
    r"(?:[\sT]*(\d{1,2})\s*[:：点时]\s*(\d{1,2})?\s*分?)?")
_HHMM_RE = re.compile(r"^\s*(\d{1,2})\s*[:：点时]\s*(\d{1,2})?\s*分?\s*$")


def parse_duration(text: str) -> int:
    """「2h」「两小时」「1d 3h」「半小时」「+90m」→ 分钟数。认不出来返回 0。"""
    t = (text or "").strip()
    if not t:
        return 0
    t = t.replace("半个", "0.5个").replace("一个", "1个").replace("两", "2")
    total = 0.0
    for num, unit in _DUR_RE.findall(t):
        u = unit.lower().strip("个")
        if u in _UNITS:
            try:
                total += float(num) * _UNITS[u]
            except ValueError:
                continue
    return int(round(total))


def parse_absolute(text: str, base: datetime) -> datetime | None:
    """「10月5日 08:00」「10-05」「08:30」→ datetime。认不出来返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    # 只给了个钟点：「08:30」「8点」——按"当天，若已过则算次日"来理解
    m = _HHMM_RE.match(t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if 0 <= h <= 23 and 0 <= mi <= 59:
            cand = base.replace(hour=h, minute=mi, second=0, microsecond=0)
            return cand if cand >= base else cand + timedelta(days=1)
        return None
    m = _ABS_RE.search(t)
    if not m:
        return None
    year = int(m.group(1)) if m.group(1) else base.year
    try:
        cand = datetime(year, int(m.group(2)), int(m.group(3)),
                        int(m.group(4) or 0), int(m.group(5) or 0))
    except ValueError:
        return None
    return cand


# ---------------------------------------------------------------- 钟

@dataclass
class GameClock:
    year: int = 1925
    month: int = 10
    day: int = 3
    hour: int = 21
    minute: int = 0
    # 开场时刻（"你们已经在这栋房子里待了两天"是这么来的）
    start: datetime | None = None
    # 模组里写的备注，例如「连着下了三天雨」
    note: str = ""

    # ------------------------------------------------ 基本

    @property
    def dt(self) -> datetime:
        return datetime(self.year, self.month, self.day, self.hour, self.minute)

    def set_dt(self, d: datetime) -> None:
        self.year, self.month, self.day = d.year, d.month, d.day
        self.hour, self.minute = d.hour, d.minute

    def advance(self, minutes: int = 0) -> None:
        if not minutes:
            return
        self.set_dt(self.dt + timedelta(minutes=int(minutes)))

    def elapsed(self) -> timedelta:
        if not self.start:
            return timedelta(0)
        return self.dt - self.start

    def elapsed_text(self) -> str:
        td = self.elapsed()
        total = int(td.total_seconds() // 60)
        if total <= 0:
            return "刚开场"
        d, rem = divmod(total, 1440)
        h, mi = divmod(rem, 60)
        bits = []
        if d:
            bits.append(f"{d} 天")
        if h:
            bits.append(f"{h} 小时")
        if mi and not d:
            bits.append(f"{mi} 分钟")
        return " ".join(bits) or "不到一分钟"

    # ------------------------------------------------ 输出

    def date_cn(self, with_weekday: bool = True) -> str:
        d = self.dt
        s = f"{d.year} 年 {d.month} 月 {d.day} 日"
        if with_weekday:
            s += f"（{WEEKDAY_CN[d.weekday()]}）"
        return s

    def clock_cn(self) -> str:
        """自然说法：「下午 3 点 40 分」。"""
        d = self.dt
        h12 = d.hour % 12 or 12
        mins = f" {d.minute} 分" if d.minute else " 整"
        return f"{time_of_day(d.hour)} {h12} 点{mins}"

    def h24(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def stamp(self) -> str:
        """一行短时间戳，用来给记忆条目打时间。"""
        d = self.dt
        return f"{d.month}月{d.day}日 {self.h24()}"

    def short(self) -> str:
        return f"{self.month}月{self.day}日 {self.h24()}"

    # ------------------------------------------------ 对照表（核心）

    def _md_label(self, d: datetime) -> str:
        """一行里的日期。跨年了就把年份也写出来——「12月31日」孤零零摆在那儿，
        谁都会以为是今年的 12 月 31 日（未来）。"""
        y = f"{d.year}年" if d.year != self.dt.year else ""
        return f"{y}{d.month}月{d.day}日（{WEEKDAY_CN[d.weekday()]}）"

    def calendar_block(self, past_days: int = 7, future_days: int = 3) -> str:
        """把「哪天是哪天」算好摆出来。

        这是整个模块的意义所在：模型不需要做日期加减，只需要查表。
        """
        now = self.dt
        rows: list[str] = []
        for back in range(1, past_days + 1):
            d = now - timedelta(days=back)
            extra = ""
            if self.start and d.date() == self.start.date():
                extra = "　← 本局开场那天"
            rows.append(f"· {self._md_label(d)} = {rel_label(back)}{extra}")
        # 再往前的几个锚点，免得它把"上周的事"说成"昨天"
        far = []
        for back in (10, 14, 21, 30):
            d = now - timedelta(days=back)
            far.append(f"{self._md_label(d)} = {rel_label(back)}")
        for fwd in range(1, future_days + 1):
            d = now + timedelta(days=fwd)
            label = {1: "明天", 2: "后天", 3: "大后天"}.get(fwd, f"{cn_num(fwd)}天后")
            rows.append(f"· {self._md_label(d)} = {label}")
        out = list(rows)
        out.append("· 再往前：" + "，".join(far))
        return "\n".join(out)

    def render(self, past_days: int = 7, future_days: int = 3) -> str:
        """给模型看的一整块。放进每一轮的消息里，不进 system（它会变）。"""
        head = f"现在是 {self.date_cn()} {self.h24()}（{self.clock_cn()}）。"
        if self.start:
            head += (f"距离开场（{self.start.strftime('%m月%d日 %H:%M')}）"
                     f"已经过了 {self.elapsed_text()}。")
        if self.note:
            head += f"\n{self.note}"
        return (f"[现在的时间]\n{head}\n\n"
                f"[日期对照表 · 相对说法都算好了]\n"
                f"{self.calendar_block(past_days, future_days)}")

    # ------------------------------------------------ 存档

    def to_dict(self) -> dict[str, Any]:
        return {
            "year": self.year, "month": self.month, "day": self.day,
            "hour": self.hour, "minute": self.minute,
            "start": self.start.isoformat(timespec="minutes") if self.start else "",
            "note": self.note,
        }

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> "GameClock":
        d = d or {}
        c = GameClock()
        for k in ("year", "month", "day", "hour", "minute"):
            if d.get(k) is not None:
                try:
                    setattr(c, k, int(d[k]))
                except (TypeError, ValueError):
                    pass
        try:
            c.dt                       # 日期非法就退回默认值
        except ValueError:
            c = GameClock()
        s = str(d.get("start") or "")
        if s:
            try:
                c.start = datetime.fromisoformat(s)
            except ValueError:
                c.start = None
        if c.start is None:
            c.start = c.dt
        c.note = str(d.get("note") or "")
        return c


# ---------------------------------------------------------------- 从模组/配置猜开场时间

_YEAR_RE = re.compile(r"(1[89]\d{2}|20\d{2})")
_MD_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
# 「1928-06-01」这种写法也得认——很多人写日期就是这个样子
_ISO_RE = re.compile(r"(1[89]\d{2}|20\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})")
_SLASH_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[/.]\s*(\d{1,2})(?!\d)")
_HM_RE = re.compile(r"(\d{1,2})\s*[:：点]\s*(\d{1,2})?")


def _parse_date_spec(spec: str) -> tuple[int, int, int] | None:
    """从一段文字里抠出 (年, 月, 日)。抠不出的部分返回 0，由调用方补默认值。"""
    t = (spec or "").strip()
    if not t:
        return None
    m = _ISO_RE.search(t)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    ym = _YEAR_RE.search(t)
    md = _MD_RE.search(t) or _SLASH_RE.search(t)
    if not ym and not md:
        return None
    return (int(ym.group(1)) if ym else 0,
            int(md.group(1)) if md else 0,
            int(md.group(2)) if md else 0)


# 模组什么都没说时用的年份。这是兜底，不是默认值：模组写了时代就用模组的。
_DEFAULT_YEAR = 1925


def _hash_day(seed: str) -> tuple[int, int]:
    """按模组 id 定下一个稳定的月日。

    以前这里写死 10 月 3 日，每一局的日历都从同一天开始，
    守秘人一开口就报同一个日子。同一模组每次开局仍然一样，不同模组不一样。
    日子取 1 到 28，撞不到二月的边界。
    """
    h = hashlib.sha256((seed or "coc").encode("utf-8")).digest()
    return h[0] % 12 + 1, h[1] % 28 + 1


def resolve_start(module: Any, options: dict[str, Any] | None = None,
                  picked: str = "") -> GameClock:
    """定开场时刻，优先级从高到低：

    1. 模组的 `start_time`（写在 module_info.yaml 里，最准）
    2. 配置里的 `options.start_time`
    3. 守秘人研读阶段挑的那一天（`picked`，来自研读输出的 <clock>）
    4. 模组简介或开场设定里明写的日期
    5. 模组 `era` 里的年份
    6. 兜底：1925 年，月日按模组 id 定

    第 4 步只扫简介和开场设定，不扫场景正文。正文里的日期多半是
    历史资料或旧报纸上的，抠出来会把开场定到几十年前去。

    这只是给个像样的起点。真正要紧的是之后每一轮那张对照表：
    模型查表说话，不自己做日期加减。
    """
    opts = options or {}
    era = str(getattr(module, "era", "") or "") if module is not None else ""
    note = f"（模组时代：{era}）" if era else ""

    src = ""
    if module is not None:
        src = str(getattr(module, "start_time", "") or "").strip()
    if not src:
        src = str(opts.get("start_time") or "").strip()
    if not src:
        src = str(picked or "").strip()
    if not src and module is not None:
        for field_name in ("summary", "premise"):
            text = str(getattr(module, field_name, "") or "")
            found = _parse_date_spec(text)
            if found and found[0]:
                src = text
                break
    if not src:
        src = era

    mid = str(getattr(module, "id", "") or "") if module is not None else ""
    mo0, da0 = _hash_day(mid)
    base = datetime(_DEFAULT_YEAR, mo0, da0, 20, 0)

    if src:
        parsed = _parse_date_spec(src)
        # 只在短文本里认时刻。长简介里随便一个"点"字都可能被当成钟点。
        hm = _HM_RE.search(src) if len(src) <= 60 else None
        y, mo, da = parsed if parsed else (0, 0, 0)
        y = y or base.year
        mo = mo or base.month
        da = da or base.day
        hh = int(hm.group(1)) if hm else base.hour
        mi = int(hm.group(2)) if (hm and hm.group(2)) else 0
        try:
            base = datetime(y, mo, da, min(hh, 23), min(mi, 59))
        except ValueError:
            pass

    c = GameClock()
    c.set_dt(base)
    c.start = base
    c.note = note
    return c


# ---------------------------------------------------------------- 解析 KP 拨钟

_ADVANCE_KINDS = {"advance", "advance_time", "elapse", "later", "拨钟", "过"}
_SETTIME_KINDS = {"time", "settime", "set_time", "clock", "now", "现在是", "时间"}


def parse_clock_directive(kind: str, target: str, payload: str) -> tuple[str, str]:
    """把守秘人的一条 <state> 指令翻译成 (动作, 参数)。

    动作是 "advance" / "set" / ""（不认识）。
      advance 2h          → 过两小时
      time 10月5日 08:00  → 直接把钟拨到那个时刻
    """
    k = (kind or "").strip().lower().rstrip(":：")
    arg = " ".join(x for x in (target, payload) if x).strip()
    if k in _ADVANCE_KINDS:
        return ("advance", arg)
    if k in _SETTIME_KINDS:
        return ("set", arg)
    return ("", "")
