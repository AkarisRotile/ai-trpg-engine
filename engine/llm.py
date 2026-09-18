"""LLM 路由：每个座位一个独立客户端。

三种提供方：
  · deepseek —— DeepSeek 官方预设（填 Key 即可）
  · custom   —— 任意 OpenAI 兼容端点（自定义 base_url / key / model）
  · mock     —— 离线生成，不联网不花钱，用来零成本跑通全流程与自检

设计要点：
- 客户端是**无状态**的，对话历史由 Agent 持有——因为"历史即身份"是本项目的核心机制。
- deepseek-reasoner 的 `reasoning_content` 会被单独取出：它既不能回灌进历史
  （DeepSeek 明确要求），也不该混进角色的 <think>。
- 所有错误都翻译成中文人话，直接显示在界面上。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from . import chargen, config as cfgmod

DEFAULT_TIMEOUT = (20, 300)      # (连接, 读取) 秒
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """带用户可读说明的调用错误。"""

    def __init__(self, message: str, *, status: int | None = None,
                 detail: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail
        self.retryable = retryable


@dataclass
class LLMResult:
    text: str = ""
    reasoning: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    model: str = ""
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ══════════════════════════════════════════════════════════════ 基类

class BaseClient:
    label = "unknown"

    def chat(self, messages: list[dict[str, str]], *,
             temperature: float | None = None, max_tokens: int | None = None,
             **kw: Any) -> LLMResult:
        raise NotImplementedError

    def probe(self) -> tuple[bool, str]:
        try:
            res = self.chat(
                [{"role": "system", "content": "你是连通性测试端点。"},
                 {"role": "user", "content": "只回复两个字：正常"}],
                max_tokens=16, temperature=0.0,
            )
            got = (res.text or "").strip()[:24]
            return True, f"连接正常（{res.model or self.label}，{res.latency_ms} ms）→ {got}"
        except LLMError as e:
            return False, e.message
        except Exception as e:  # noqa: BLE001
            return False, f"连接失败：{type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════════════ OpenAI 兼容

def _endpoint(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise LLMError("没有填写 Base URL。")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def _friendly_error(status: int, body: str) -> str:
    short = (body or "").strip()[:300]
    table = {
        400: "请求被拒绝（400）。可能是模型名写错，或该模型不支持当前参数。",
        401: "API Key 无效或未授权（401）。请检查 Key 是否填对、是否已激活。",
        402: "账户余额不足（402）。请到服务商后台充值。",
        403: "没有权限访问该模型（403）。",
        404: "接口地址不存在（404）。请检查 Base URL，通常应以 /v1 结尾。",
        422: "参数不合法（422）。",
        429: "请求过于频繁被限流（429）。稍等片刻会自动重试。",
        500: "服务端错误（500）。稍后重试。",
        502: "网关错误（502）。稍后重试。",
        503: "服务暂不可用（503）。稍后重试。",
    }
    base = table.get(status, f"接口返回错误（{status}）。")
    return f"{base}\n原始信息：{short}" if short else base


class OpenAICompatClient(BaseClient):
    """兼容 OpenAI /v1/chat/completions 的任意端点。"""

    label = "openai-compat"

    # 这些模型不接受采样参数，传了会报错
    NO_SAMPLING = ("reasoner", "o1", "o3", "o4")

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: tuple[int, int] = DEFAULT_TIMEOUT,
                 max_retries: int = 2, label: str = "") -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.label = label or model or "openai-compat"
        self.url = _endpoint(base_url)

    def _supports_sampling(self) -> bool:
        m = (self.model or "").lower()
        return not any(tag in m for tag in self.NO_SAMPLING)

    def chat(self, messages: list[dict[str, str]], *,
             temperature: float | None = None, max_tokens: int | None = None,
             **kw: Any) -> LLMResult:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if self._supports_sampling():
            if temperature is not None:
                payload["temperature"] = float(temperature)
        payload["stream"] = False

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        last_err: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                resp = requests.post(self.url, headers=headers,
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     timeout=self.timeout)
            except requests.exceptions.SSLError as e:
                raise LLMError(f"TLS 握手失败，无法连接 {self.url}。\n{e}") from e
            except requests.exceptions.ConnectTimeout as e:
                raise LLMError(f"连接 {self.url} 超时（{self.timeout[0]} 秒）。"
                               f"请检查网络或 Base URL 是否正确。") from e
            except requests.exceptions.ReadTimeout as e:
                last_err = LLMError(f"等待模型响应超时（{self.timeout[1]} 秒）。"
                                    f"可以调小该座位的 max_tokens 后重试。", retryable=True)
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_err from e
            except requests.exceptions.RequestException as e:
                last_err = LLMError(f"网络请求失败：{type(e).__name__}: {e}", retryable=True)
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_err from e

            latency = int((time.monotonic() - started) * 1000)

            if resp.status_code != 200:
                err = LLMError(_friendly_error(resp.status_code, resp.text),
                               status=resp.status_code,
                               retryable=resp.status_code in RETRY_STATUS)
                if err.retryable and attempt < self.max_retries:
                    last_err = err
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise err

            try:
                data = resp.json()
            except ValueError as e:
                raise LLMError(f"接口返回的不是合法 JSON，可能 Base URL 指到了网页而不是 API。\n"
                               f"原始内容开头：{resp.text[:200]}") from e

            if isinstance(data, dict) and data.get("error"):
                msg = data["error"]
                msg = msg.get("message") if isinstance(msg, dict) else str(msg)
                raise LLMError(f"接口返回错误：{msg}")

            try:
                choice = (data.get("choices") or [])[0]
                message = choice.get("message") or {}
            except (IndexError, AttributeError) as e:
                raise LLMError(f"接口响应结构异常，找不到 choices。原始：{str(data)[:300]}") from e

            text = message.get("content") or ""
            reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
            if not text.strip() and choice.get("finish_reason") == "length":
                raise LLMError("模型还没开始输出就达到了 max_tokens 上限，"
                               "请调大该座位的 max_tokens。")

            usage = data.get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            return LLMResult(
                text=text,
                reasoning=reasoning,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cached_tokens=int(usage.get("prompt_cache_hit_tokens")
                                  or details.get("cached_tokens") or 0),
                model=data.get("model") or self.model,
                latency_ms=latency,
                raw={"id": data.get("id", ""), "finish_reason": choice.get("finish_reason", "")},
            )

        raise last_err or LLMError("请求失败，且没有可用的错误信息。")


# ══════════════════════════════════════════════════════════════ 离线 Mock

_PL_ACTS = [
    "我先贴着门听一下里面有没有动静，手搭在门把上但不推。",
    "我蹲下去看地板上的拖痕往哪个方向走，用手电斜着打光。",
    "我把枪端起来，慢慢挪到窗边往外看一眼，不看太久。",
    "我掏出笔记本，把刚才看到的记下来，边写边念给自己听。",
    "我不动，先站在原地数自己听到了几种声音。",
    "我伸手摸了一下壁炉里那半截东西，只用指尖碰一下。",
    "我压低声音跟旁边的人说：别出声，往后退两步。",
    "我绕到楼梯扶手另一侧，尽量踩在靠墙的那一条地板上。",
]

_PL_OOC = [
    "（我侦查才25，别指望我）（谁去？）",
    "（等下，刚才说大门是从外面锁的？那我们怎么进来的）",
    "（我觉得先别上楼，把一楼摸完再说）",
    "（来吧，反正早晚要上去）",
    "（我有个不太好的想法，这房子可能一直有人住）",
    "（KP你是不是又想搞我们）",
    "（谁身上有能照明的东西？我手电快没电了）",
    "（我先申请个聆听，成不成看天）",
]

_PL_SKILLS = ["聆听", "侦查", "图书馆利用", "心理学", "神秘学", "潜行", "急救", "历史"]

# 车卡桌上的闲聊（离线模式用）
_CHARGEN_OOC = [
    "（我都行，看你们缺什么）",
    "（别又全车打架的，上次就是这么团灭的）",
    "（我这个属性也太废了，STR 只有35）",
    "（那就说定了，侦查我来点）",
    "（你不能每次都车记者吧）",
    "（行行行，听你的）",
    "（我先说好，我不玩社交，嘴太笨）",
    "（这个职业我还没玩过，试试）",
    "（点数够吗？别又超了）",
]

_KP_CHARGEN_OOC = [
    "（你们商量好了跟我说一声，我这边的开场已经准备好了）",
    "（这局翻资料和跟人打交道的场合不少，你们自己掂量）",
    "（别都车打架的，我提醒过了啊）",
    "（可以，都挺有意思的）",
    "（不着急，你们慢慢聊）",
]

_TEATIME_OOC = [
    "（等一下，那面镜子照不出人影这件事，我当时真没往心里去）",
    "（所以说那个脚步声从头到尾都是假的？我白紧张了半天）",
    "（我早就说了别一个人上楼）",
    "（那本书里夹的东西到底是谁的，你怎么不早说）",
    "（我算是服了，我猜的方向完全是反的）",
    "（下次我一定要点侦查，真的）",
]

_KP_TEATIME_OOC = [
    "（镜子是给摇人埋的，他那句口头禅我一直想找个地方用上）",
    "（说实话第二幕那段是我现编的，你们没看出来吧）",
    "（你们本来可以完全不下去的，是你们自己非要开那扇门）",
    "（我埋了三个彩蛋，你们一个都没接住）",
    "（下次不吓你们了——大概吧）",
]

_DEFEND_OOC = [
    "（这是我叔叔留下的，他以前跑船，说这东西能保命）",
    "（我一个调查员在外面跑，带个防身的东西不过分吧）",
    "（那行吧，那我换成小一点的成不成）",
    "（好好好，我放车上，不带进去）",
    "（我写了来历的啊，你翻一下我的背景）",
]

_KP_VERDICT_OOC = [
    "（行，但别让我看见你用它干蠢事）",
    "（这次放你一马，下次车卡老实点）",
    "（记住了啊，这一局我盯你）",
    "（可以，算你有理）",
]

# 离线模式的线索池。刻意做大一点——去重之后仍然能沉淀出足够多的编年史条目，
# 这样自检里的"记忆确实在长"才是有效断言，而不是靠运气。
_MOCK_CLUES = [
    "一楼侧门虚掩着，门缝里没有光",
    "空气里有股湿纸浆的味道",
    "地板上的拖痕停在一扇门前，方向朝里",
    "门框内侧有一道很新的划痕，像是被尖东西刮的",
    "楼梯扶手擦得很干净，这栋房子不像空置了两年",
    "壁炉里的灰是新翻过的，有人最近动过",
    "走廊比从外面看要长，走到底花了比预想更多的时间",
    "楼上那个声音有节奏，停了之后就没有了，像在听下面的动静",
    "雨水里有股铁锈味，不该是从天上带来的",
    "地垫上那双男鞋摆得太整齐了，鞋尖朝里",
]

_PL_THINK = [
    "这地方不该这么安静。",
    "先弄清楚有几个人在这栋房子里。",
    "手上的东西不够，得找能照明和能防身的两样。",
    "别自己吓自己，一步一步来。",
    "刚才那个声音不对劲，太有分量了。",
    "要是现在退出去，这几天的功夫就白费了。",
]

_KP_NARR = [
    "门厅里只剩下雨声。你们手电的光柱扫过去，墙纸上有一片深色的渍，"
    "从踢脚线一直洇到齐胸高，形状不太像水痕。\n\n"
    "楼梯上方的黑暗里，什么也没有动。",
    "地板那几道拖痕停在一扇侧门前，门虚掩着，缝里没有光。\n\n"
    "你们靠近的时候，能闻到一股很淡的、类似湿纸浆的味道。",
    "窗外的雨点砸在玻璃上，节奏很密。有一瞬间，你们觉得雨声里夹着别的什么，"
    "但再听就只剩雨声了。",
    "壁炉里的东西被翻出来之后，灰扬起来一点。那半截羊皮纸边缘是焦的，"
    "中间还能认出一个圆圈，圈里有些笔画。",
]

_KP_SECRET = (    "当前场景：{scene}\n"
    "真相暴露程度：玩家目前只知道「这栋房子里有别的东西」，还没有接触核心。\n"
    "NPC：管家在二楼书房（已死，尸体被移动过）；「那个东西」在地下室，尚未被惊动。\n"
    "时间线：再过两轮，地下室的动静会传到一楼。\n"
    "玩家的认知边界：他们不知道羊皮纸是一份契约的一部分，也不知道地下室有人。"
)


_MOCK_HYPOS = [
    "这栋房子里还有别人",
    "楼上的响声不是木头热胀冷缩",
    "有人在最近这几天来过",
    "门口那双鞋的主人还在这里",
    "这些线索都指向同一个地方",
]

_MOCK_QUESTIONS = [
    "是谁从外面锁的门",
    "霍尔本去哪了",
    "地下室的门怎么打开",
    "管家到底在怕什么",
]

# 离线模式下"夹带"的东西——审卡环节靠它制造戏
SUSPICIOUS_ITEMS = [
    "一把藏在行李箱夹层里的手枪（没有持枪证）",
    "一盒来路不明的手枪子弹",
    "一把士兵用的军刺",
    "一小瓶不知道是什么的药水",
]


class MockClient(BaseClient):
    """离线后端。目的是让整套流程（四通道解析、掷骰、记忆回写）零成本跑通。"""

    label = "mock"

    def __init__(self, model: str = "mock", seed: int | None = None) -> None:
        self.model = model or "mock"
        self.rng = random.Random(seed)
        self._turn = 0
        # 记住桌上还有谁。工具轮的第二次调用看不到世界消息，
        # 没有这个记忆就会把同桌的人忘掉（桌边点名就点不出来了）。
        self._seen_names: list[str] = []

    # -------------------------------------------------- 入口

    def chat(self, messages: list[dict[str, str]], *,
             temperature: float | None = None, max_tokens: int | None = None,
             **kw: Any) -> LLMResult:
        time.sleep(0.05)                       # 模拟一点延迟，界面不会闪得太快
        system = ""
        for m in messages:
            if m.get("role") == "system":
                system = m.get("content", "")
                break
        last_user = next((m.get("content", "") for m in reversed(messages)
                          if m.get("role") == "user"), "")
        phase = kw.get("mock_phase") or self._detect_phase(system, last_user)
        self._turn += 1

        if phase == "chargen":
            text = self._chargen(last_user)
        elif phase == "ocr":
            text = self._ocr()
        elif phase == "study":
            text = self._study(system)
        elif phase == "audit":
            text = self._audit(system)
        elif phase == "defend":
            text = self._defend()
        elif phase == "verdict":
            text = self._verdict()
        elif phase == "teatime":
            text = self._teatime()
        elif phase == "teatime_kp":
            text = self._teatime_kp()
        elif phase == "chargen_chat":
            text = self._chargen_chat(last_user)
        elif phase == "kp_chargen_chat":
            text = self._kp_chargen_chat(last_user)
        elif phase == "brief":
            text = self._brief(last_user)
        elif phase == "retro":
            text = self._retro(system, last_user)
        elif phase == "table":
            text = self._table(last_user)
        elif phase == "anchor":
            text = self._anchor(system, last_user)
        elif "主持人" in system and "守秘人" in system:
            text = self._kp(last_user)
        else:
            text = self._pl(system, last_user)

        return LLMResult(
            text=text, model=self.model,
            prompt_tokens=sum(len(m.get("content", "")) for m in messages) // 2,
            completion_tokens=len(text) // 2, latency_ms=40,
            raw={"mock": True},
        )

    @staticmethod
    def _detect_phase(system: str, user: str) -> str:
        if "散场" in system and "player_update" in system:
            return "retro"
        if "你先做功课" in system:
            return "study"
        if "现在轮到你**审卡**" in system:
            return "audit"
        if "守秘人在审卡，他质疑了你身上带的一样东西" in system:
            return "defend"
        if "现在你裁决" in system:
            return "verdict"
        if "模组现在解禁了" in system:
            return "teatime"
        if "跑完了，大家在散场复盘" in system:
            return "teatime_kp"
        if "开场简报" in system and "<brief>" in system:
            return "brief"
        if "正在跟桌上的人一起车卡" in system:
            return "chargen_chat"
        if "现在玩家们正在你眼前商量车卡" in system:
            return "kp_chargen_chat"
        if "只是坐在桌边跟人说句话" in system:
            return "table"
        if "车一张调查员卡" in system or "你得给自己车一张调查员卡" in system:
            return "chargen"
        if "开局之前先说一句" in user:
            return "anchor"
        return "play"

    # -------------------------------------------------- 车卡

    def _chargen(self, user: str) -> str:
        attrs: dict[str, int] = {}
        for key in ("STR", "CON", "DEX", "APP", "POW", "SIZ", "INT", "EDU", "LUCK"):
            m = re.search(rf"{key}\s+(\d+)", user)
            if m:
                attrs[key] = int(m.group(1))

        occ = self.rng.choice(chargen.OCCUPATIONS)
        pack = chargen._skill_pack(occ)
        cap = chargen.budget(attrs) if len(attrs) == 9 else 300

        # 从**基础值**起步再分配预算——这才是真实车卡的做法，
        # 也能保证脚本生成的卡一定过 engine.chargen.validate_sheet 的校验。
        targets: list[list] = [[name, chargen.base_of(name, attrs)] for name, _ in pack]
        for name in self.rng.sample(
                ["侦查", "聆听", "急救", "潜行", "心理学", "图书馆利用", "话术"], k=2):
            if all(t[0] != name for t in targets):
                targets.append([name, chargen.base_of(name, attrs)])

        weights = [self.rng.uniform(0.6, 1.5) for _ in targets]
        total_w = sum(weights) or 1.0
        remaining = cap
        for i, t in enumerate(targets):
            add = min(int(cap * weights[i] / total_w), 90 - t[1], remaining)
            add = max(0, add)
            t[1] += add
            remaining -= add
        for t in targets:                      # 余数逐项填到刚好用完
            if remaining <= 0:
                break
            add = min(90 - t[1], remaining)
            t[1] += add
            remaining -= add

        first = self.rng.choice(chargen.FIRST_NAMES)
        last = self.rng.choice(chargen.LAST_NAMES)
        lines = [
            "<sheet>",
            f"name: {first}·{last}",
            f"occupation: {occ}",
            f"age: {self.rng.randint(25, 52)}",
            f"gender: {self.rng.choice(['男', '女'])}",
            f"credit: {self.rng.randint(20, 60)}",
            "skills:",
        ]
        for nm, val in targets:
            lines.append(f"  {nm}: {val}")
        lines += [
            "inventory:",
            "  - 随身衣物与证件",
            "  - 一支手电筒（电池不太够）",
            "  - 笔记本与铅笔",
            f"  - 一件跟{occ}这行当有关的小工具",
        ]
        # 有意让一部分卡上夹带点不该有的东西——审卡环节才有戏可看
        if self.rng.random() < 0.5:
            lines.append(f"  - {self.rng.choice(SUSPICIOUS_ITEMS)}")
        lines += [
            f"backstory: 我是个{occ}，来这里本来只是想把一件小事办完。"
            "但这地方从我下车那一刻起就不对劲，我说不上来哪里不对。",
            "</sheet>",
            "<ooc>",
            f"（手气一般，不过{occ}这职业我还挺想玩的）",
            "</ooc>",
        ]
        return "\n".join(lines)

    # -------------------------------------------------- 车卡桌上的讨论

    def _chargen_chat(self, user: str) -> str:
        first = "你先说说自己想玩什么" in user
        if first:
            occ = self.rng.choice(chargen.OCCUPATIONS)
            return (f"<pitch>\n我想车个{occ}。"
                    f"{self.rng.choice(['主要是好演', '上次就想玩这个了', '看着顺眼就车'])}\n</pitch>\n"
                    f"<ooc>\n{self.rng.choice(_CHARGEN_OOC)}\n</ooc>")
        if self.rng.random() < 0.3:
            occ = self.rng.choice(chargen.OCCUPATIONS)
            return (f"<pitch>\n算了，我改主意了，还是车个{occ}吧。\n</pitch>\n"
                    f"<ooc>\n{self.rng.choice(_CHARGEN_OOC)}\n</ooc>")
        return f"<ooc>\n{self.rng.choice(_CHARGEN_OOC)}\n</ooc>"

    def _kp_chargen_chat(self, user: str) -> str:
        return f"<ooc>\n{self.rng.choice(_KP_CHARGEN_OOC)}\n</ooc>"

    # -------------------------------------------------- 守秘人的功课

    def _study(self, system: str) -> str:
        ids: list[str] = []
        m = re.search(r"只能用这些 id：(.+)", system)
        if m:
            ids = [x.strip() for x in re.split(r"[、,，\s]+", m.group(1)) if x.strip()]
        spine = "\n".join(ids)
        return f"""<spine>
{spine}
</spine>
<understanding>
这故事真正在讲的，是一件"被封住的东西迟早会想办法让人来开门"的事。
骨架绕不过去：他们进来、发现出不去了、发现这里死过人、最后下去面对源头。
玩家最可能偏的地方是卡在门厅不敢上楼，我打算用楼上那点动静把他们顶上去。
我觉得最该出彩的瞬间，是他们在书房发现尸体被动过的那一刻。
</understanding>
<expansion>
[加] （第一幕）门厅那面镜子照不出人影，但玩家要到第三幕才会想起它
[加] （第二幕）管家的猫躲在书房里，怕人，但会跟着其中一个人走
[加] （全程）每当有人提到"下面"，走廊里就多一声很轻的回应
</expansion>
<eggs>
[加] （第一幕）如果有人问"这房子里有几个人"，让楼上的脚步声正好在此刻响一下
[加] （第二幕）书房那本翻烂的书里夹着一张旧卡片
[加] （第三幕）某个 NPC 会不经意地说出某位玩家常念叨的那句话
</eggs>"""

    # -------------------------------------------------- OCR 占位

    def _ocr(self) -> str:
        """离线模式下返回一段"像扫描件抄下来的"文字，让 OCR 管线能被完整测到。"""
        n = self._turn
        return f"""## 第 {n} 节 · 档案摘录

以下内容来自霍尔本宅遗留的文件，字迹潦草，多处 [看不清]。

"我第三次下到那间石室的时候，那个东西的位置变了。
它本来在石台正中央，现在挪到了靠墙的一侧——像是有谁
把它转过去，让它面朝墙壁。"

管家在页边用铅笔写了一句：
"不是它转过去的。"

## 关于【看不清】的说明

本页右下角有一枚印章，已模糊不可辨。
其余段落完整。"""

    # -------------------------------------------------- 审卡

    def _audit(self, system: str) -> str:
        blocks = re.split(r"【(pl_\d+)】", system)
        rows: list[str] = []
        for i in range(1, len(blocks) - 1, 2):
            sid, body = blocks[i], blocks[i + 1]
            m = re.search(r"随身物品：(.+)", body)
            if not m:
                continue
            items = [x.strip() for x in re.split(r"[、,，]", m.group(1))
                     if x.strip() and x.strip() != "（空）"]
            if not items:
                continue
            item = items[-1]
            rows.append(f"质疑 | {sid} | {item} | "
                        f"你一个调查员带{item}干什么？你怎么带进去的？")
        if not rows:
            rows.append("通过 | pl_1 | 随身衣物 | 这个没什么问题。")
        return ("<audit>\n" + "\n".join(rows[:3]) + "\n</audit>\n"
                "<ooc>\n（带这个的，你先说说哪儿来的）\n</ooc>")

    def _defend(self) -> str:
        if self.rng.random() < 0.4:
            return ("<ooc>\n（那是我叔叔留下的，就一把，真就一把）\n</ooc>\n"
                    "<mem>\ninventory_remove: 那个东西\n</mem>")
        return f"<ooc>\n{self.rng.choice(_DEFEND_OOC)}\n</ooc>"

    def _verdict(self) -> str:
        return ("<verdict>\n"
                "通过 | pl_1 | 那东西 | 行吧，算你从家里带出来的，但别乱用。\n"
                "</verdict>\n"
                f"<ooc>\n{self.rng.choice(_KP_VERDICT_OOC)}\n</ooc>")

    # -------------------------------------------------- 散场茶话会

    def _teatime(self) -> str:
        return f"<ooc>\n{self.rng.choice(_TEATIME_OOC)}\n</ooc>"

    def _teatime_kp(self) -> str:
        return f"<ooc>\n{self.rng.choice(_KP_TEATIME_OOC)}\n</ooc>"

    # -------------------------------------------------- 赛前简报（不剧透）

    def _brief(self, user: str) -> str:
        title = "这一局"
        m = re.search(r"《(.+?)》", user)
        if m:
            title = m.group(1)
        n = 3
        m2 = re.search(r"会有\s*(\d+)\s*名玩家", user)
        if m2:
            n = int(m2.group(1))
        return f"""\
<brief>
## 这是个什么故事

你们因为各自的原因，在一个不太方便的天气里，来到了一个不太方便的地方。
{title}这个故事的开始很简单：门在你们身后关上了，而你们还没弄清楚这里是谁在住。

## 车卡建议

- 这一局有不少需要**留意细节**的场合，建议至少一个人点侦查或聆听。
- 也有需要**跟人打交道**和**翻查资料**的时候，图书馆利用、心理学、话术都用得上。
- {n} 个人分头行动会比较危险，建议有人能应付突发的肉体冲突。
- 最重要的是：车一个**你自己想演的**人，比车一个「好用的」人更有意思。

## 一句开场白

「雨从你们下车那一刻就没停过——而门廊下的灯，是亮着的。」
</brief>"""

    # -------------------------------------------------- 桌边插话

    def _table(self, user: str) -> str:
        """离线模式也要走一遍桌边来回，否则这条链路测不到。"""
        lines = re.findall(r"^·\s*(.+?)（(.+?)）：(.+)$", user, re.M)
        replies = [
            "（我角色不知道你们上去了啊，你们在上面看见什么了？）",
            "（行，那我这就过去，但得等我先把这儿看完）",
            "（别催，我这角色本来就不该知道你们在哪儿）",
            "（你喊我一嗓子，我就有理由过去了）",
            "（我这边也有事，先别管我）",
            "（等下，你先说清楚你们几个人在一起）",
        ]
        if lines:
            who = lines[-1][0]
            pick = f"（{who}，我角色不知道你们那边什么情况，你先跟我说说）"
        else:
            pick = self.rng.choice(replies)
        return f"<ooc>\n{pick}\n</ooc>"

    # -------------------------------------------------- 复盘（跨周目）

    def _retro(self, system: str, user: str) -> str:
        """离线模式下也走一遍复盘，保证跨周目记忆链路被真实检验。"""
        quips = [
            "这栋房子里到底有几个人？", "先别开门，我再听一下。",
            "我永远不信守秘人说这里安全。", "掷骰之前我得先摸一下骰子。",
            "谁身上还有手电？", "下次先确认出口再往里走。",
            "我这人一紧张就开始数东西。", "别分头行动，上局就是这么出事的。",
        ]
        memes = self.rng.sample(quips, k=min(2, len(quips)))
        others = re.findall(r"【(.+?)】", user)
        others = [o for o in dict.fromkeys(others) if o and o != "守秘人"][:2]
        rel = "\n".join(f"  - {n} = 靠得住，但冲得太靠前，得盯着点" for n in others)

        lines = ["<player_update>", "memes:"]
        for i, m in enumerate(memes):
            kind = "口头禅" if i == 0 else "习惯"
            lines.append(f"  - {m} | {kind},桌边")
        lines.append("quirks:")
        lines.append(f"  - {self.rng.choice(['进门前先数一遍人数', '把线索写在纸上一条条对', '掷骰前先问一遍规则'])}")
        if rel:
            lines.append("relationships:")
            lines.append(rel)
        lines.append("reflections:")
        lines.append(f"  - {self.rng.choice(['下次先把出口确认了再往里走', '别一个人上楼', '该问的时候要早点问，别硬扛'])}")
        lines.append("</player_update>")
        lines.append("<ooc>")
        lines.append(f"（这局挺带劲的，下回还来）")
        lines.append("</ooc>")
        return "\n".join(lines)

    # -------------------------------------------------- 入戏锚定

    def _anchor(self, system: str, user: str) -> str:
        pc = self._pc_name(system) or "我"
        return (
            "<think>\n雨太大了，鞋已经湿透。\n</think>\n"
            f"<act>\n{pc}站在门廊下把大衣上的水抖了抖，抬手敲了两下门，没人应。\n</act>\n"
            "<ooc>\n（人齐了吗？齐了我就推门了）\n</ooc>\n"
            "<mem>\n</mem>"
        )

    # -------------------------------------------------- PL 回合

    def _pl(self, system: str, user: str) -> str:
        pc = self._pc_name(system) or "我"
        act = self.rng.choice(_PL_ACTS)
        ooc = self.rng.choice(_PL_OOC)
        think = self.rng.choice(_PL_THINK)

        others = self._other_names(user)
        if others:
            self._seen_names = others
        else:
            others = self._seen_names        # 第二次调用时靠记忆补上
        if others and self.rng.random() < 0.5:
            ooc = f"（{self.rng.choice(others)}，你那边什么情况？）"

        check_line = ""
        if self.rng.random() < 0.55:
            skill = self.rng.choice(_PL_SKILLS)
            check_line = f"\n检定申请 {skill}"

        roll_block = ""
        if self.rng.random() < 0.5:
            roll_block = ("\n<roll>\n"
                          + self.rng.choice(["1d6 | 我随手翻找一下能翻出什么",
                                             "1d100 | 运气",
                                             "1d8 | 大概要花多久",
                                             "2d6 | 那东西有多重"])
                          + "\n</roll>")

        recall_block = ""
        if self.rng.random() < 0.4:
            recall_block = ("\n<recall>\n"
                            + self.rng.choice(["侧门", "壁炉", "拖痕", "鞋", "划痕"])
                            + "\n</recall>")

        mem = ""
        if self.rng.random() < 0.75:
            parent = ""
            if self.rng.random() < 0.35:
                parent = f"[{self.rng.choice(_MOCK_QUESTIONS)}] "
            mem = f"\nadd_fact: {parent}{self.rng.choice(_MOCK_CLUES)}"
        if self.rng.random() < 0.45:
            mem += (f"\nadd_hypothesis: {self.rng.choice(_MOCK_HYPOS)}"
                    f" | 依据: {self.rng.choice(['说不上来，就是觉得', '跟刚才那声对得上', '太巧了'])}")
        if self.rng.random() < 0.30:
            mem += f"\nadd_question: {self.rng.choice(_MOCK_QUESTIONS)}"
        if self.rng.random() < 0.20:
            mem += f"\nconfirm: {self.rng.choice(_MOCK_HYPOS)}"
        if self.rng.random() < 0.35:
            other = self.rng.choice([n for n in others] or [""])
            if other:
                mem += (f"\nimpression: {other} = "
                        f"{self.rng.choice(['靠得住但太冲', '看着冷静，其实也慌', '话少，关键时候顶用'])}")
        if self.rng.random() < 0.25:
            mem += f"\nlocation: {self.rng.choice(['一楼门厅', '二楼走廊', '侧门附近'])}"

        return (
            f"<think>\n{think}\n</think>\n"
            f"<act>\n{act}\n</act>\n"
            f"<ooc>\n{ooc}{check_line}\n</ooc>\n"
            f"{roll_block}"
            f"{recall_block}"
            f"<mem>{mem}\n</mem>"
        )

    # -------------------------------------------------- KP 回合

    def _kp(self, user: str) -> str:
        narr = self.rng.choice(_KP_NARR)
        scene_m = re.search(r"\[当前场景原文[^\]]*\]\s*\n(.{0,40})", user)
        scene = (scene_m.group(1).strip().replace("\n", " ")[:20] if scene_m else "scene_01")

        state_lines = []
        if self.rng.random() < 0.35:
            state_lines.append("advance_scene scene_02")
        if self.rng.random() < 0.30:
            state_lines.append("whisper pl_1 你注意到门框内侧有一道很新的划痕，像是被什么尖的东西刮过。")
        if self.rng.random() < 0.25:
            state_lines.append("check pl_1 侦查 regular")
        if self.rng.random() < 0.30:
            state_lines.append("openroll 1d6 那东西会不会动")

        # 暗骰：只有守秘人和导演看得到
        roll_block = ""
        if self.rng.random() < 0.5:
            roll_block = ("\n<roll>\n"
                          + self.rng.choice(["1d6 | 他会不会撒谎",
                                             "1d100 | 那个东西今晚会不会动",
                                             "1d3 | 走廊尽头有几扇门"])
                          + "\n</roll>")

        return (
            f"<narr>\n{narr}\n</narr>\n"
            f"<ooc>\n（刚才说听门的那位，过个聆听）\n</ooc>\n"
            f"{roll_block}"
            f"<secret>\n{_KP_SECRET.format(scene=scene)}\n</secret>\n"
            f"<state>\n" + "\n".join(state_lines) + "\n</state>"
        )

    # -------------------------------------------------- 小工具

    @staticmethod
    def _pc_name(system: str) -> str:
        m = re.search(r"你手里的调查员叫(.+?)——", system)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _other_names(user: str) -> list[str]:
        block = ""
        m = re.search(r"\[桌上其他人的动静\]\n(.*?)(?:\n\n|\Z)", user, re.S)
        if m:
            block = m.group(1)
        return re.findall(r"^·\s*(.+?)(?:\s*——|\s*$)", block, re.M)


# ══════════════════════════════════════════════════════════════ 工厂

def make_client(seat: dict[str, Any], seed: int | None = None) -> BaseClient:
    provider = (seat.get("provider") or "deepseek").strip()
    model = (seat.get("model") or "").strip()

    if provider == "mock":
        return MockClient(model or "mock", seed=seed)

    base_url = (seat.get("base_url") or "").strip()
    api_key = (seat.get("api_key") or "").strip()
    if not model:
        raise LLMError(f"座位「{seat.get('display_name', seat.get('seat_id'))}」没有填模型名。")
    if not base_url:
        raise LLMError(f"座位「{seat.get('display_name', seat.get('seat_id'))}」没有填 Base URL。")
    if not api_key:
        raise LLMError(f"座位「{seat.get('display_name', seat.get('seat_id'))}」没有填 API Key。")

    return OpenAICompatClient(base_url, api_key, model,
                              label=cfgmod.PROVIDERS.get(provider, {}).get("label", provider))


def probe_seat(seat: dict[str, Any]) -> tuple[bool, str]:
    try:
        client = make_client(seat)
    except LLMError as e:
        return False, e.message
    return client.probe()
