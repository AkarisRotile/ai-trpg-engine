"""配置模型与读写。

便携原则：所有数据目录都相对 **exe 所在目录**（或开发期的项目根）解析，
绝不写系统盘——这样整个文件夹拷到 U 盘也能跑。
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import chargen

APP_TITLE = "AI 跑团引擎 · COC 第七版"
CONFIG_VERSION = 1

# UI 提交配置时，未改动的密钥字段回传此哨兵值，服务端保留原值
KEEP = "__KEEP__"


# ---------------------------------------------------------------- 路径

def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """只读资源（ui/）所在目录。打包后位于 PyInstaller 解包目录。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_root()))
    return app_root()


def data_root() -> Path:
    """数据根目录。

    默认在 exe 同级，便携；但允许用环境变量 COC_DATA_DIR 指到别处——
    离线自检脚本靠它把测试数据写到临时目录，不去动用户的正式存档。
    """
    env = os.environ.get("COC_DATA_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    p = app_root() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def modules_root() -> Path:
    p = data_root() / "modules"
    p.mkdir(parents=True, exist_ok=True)
    return p


def memory_root() -> Path:
    p = data_root() / "memory"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sessions_root() -> Path:
    p = data_root() / "runtime" / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def exports_root() -> Path:
    p = data_root() / "exports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return data_root() / "config.json"


def ui_dir() -> Path:
    return resource_root() / "ui"


# ---------------------------------------------------------------- 提供方

PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "hint": "在 platform.deepseek.com 创建 API Key（形如 sk-…）。"
                "deepseek-chat 反应快、适合 PL；deepseek-reasoner 更擅长裁定，适合 KP。",
        "needs_key": True,
    },
    "custom": {
        "label": "自定义（任意 OpenAI 兼容端点）",
        "base_url": "",
        "models": [],
        "hint": "填入形如 https://your-host/v1 的地址、你的 Key 与模型名。"
                "兼容 OpenAI /v1/chat/completions 协议的服务都可以。",
        "needs_key": True,
    },
    "mock": {
        "label": "离线模拟（不联网、不消耗额度）",
        "base_url": "",
        "models": ["mock-kp", "mock-pl"],
        "hint": "本地生成的占位输出，用于试用界面与零成本自检。"
                "它会正常走完四通道、掷骰与记忆回写全流程。",
        "needs_key": False,
    },
}


# ---------------------------------------------------------------- 默认配置

def make_pl_seat(index: int, entry: Any = None, preset_index: int | None = None) -> dict[str, Any]:
    """造一个玩家座位。

    `entry` 是名册里的人（engine/roster.py）。**玩家身份是持久的**，
    所以 profile.player_id 指向名册 id —— 跨周目记忆跟着人走，不跟着座位走。

    **刻意不预置角色。** 以前这里会塞一张现成的调查员卡，于是：
      · 桌上从第一秒显示的就是一个引擎挑的角色名，而不是坐在这儿的那个玩家
      · 车卡时提示词还说「守秘人给你预留的名字是「X」」——
        那个名字是引擎随机给的，跟守秘人没关系，纯粹是编的
    现在 `character` 是空的：角色由 AI 自己车，**名字也由它自己取**。
    在它车出来之前，这个座位上坐的是玩家本人，显示的就是网名。
    """
    from . import roster as roster_mod
    person = entry or roster_mod.default_pl(index)
    return {
        "seat_id": f"pl_{index + 1}",
        "kind": "PL",
        # 车卡之前显示网名；车出角色之后由 PLAgent 换成「角色名（网名）」
        "display_name": person.display(),
        "provider": "deepseek",
        "base_url": PROVIDERS["deepseek"]["base_url"],
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.9,
        "max_tokens": 1400,
        "enabled": True,
        "profile": {
            "player_id": person.id,
            "player_name": person.display(),   # 桌上实际怎么喊他（网名截出来的简称）
            "handle": person.handle,           # 网名全称
            "table_voice": person.voice,
            "playstyle": person.playstyle,
            "rigor": person.rigor,
            "rigor_note": person.rigor_note(),
            "personality_traits": list(person.traits),
            "habits": list(person.habits),
        },
        "character": {},
    }


def make_kp_seat(entry: Any = None) -> dict[str, Any]:
    """造守秘人座位。

    display_name 用**真人网名**而不是"守秘人"——这样玩家可以在括号里直接喊
    （汤圆你这描述也太吓人了），这在真实桌上是常态，也让 KP 更像个人而不是个功能。
    """
    from . import roster as roster_mod
    person = entry or roster_mod.default_kp()
    return {
        "seat_id": "kp",
        "kind": "KP",
        "display_name": person.display(),
        "provider": "deepseek",
        "base_url": PROVIDERS["deepseek"]["base_url"],
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.85,
        "max_tokens": 2200,
        "enabled": True,
        "profile": {
            "player_id": person.id,
            "player_name": person.display(),
            "handle": person.handle,
            "table_voice": person.voice,
            "playstyle": person.playstyle,
            "rigor": person.rigor,
            "rigor_note": person.rigor_note(),
            "personality_traits": list(person.traits),
            "habits": list(person.habits),
        },
        "character": None,
    }


def _carry_credentials(new_seat: dict[str, Any], old: dict[str, Any],
                       changed_role: bool) -> None:
    """换站位时把 API 凭据带过去——因为 Key 是**人的**，不是座位的。

    温度与 max_tokens 按新站位的默认值走：当 KP 需要更长的输出。
    """
    for k in ("provider", "base_url", "api_key", "model"):
        if k in old and old[k] is not None:
            new_seat[k] = old[k]
    if not changed_role:
        for k in ("temperature", "max_tokens"):
            if old.get(k) is not None:
                new_seat[k] = old[k]


def apply_roster(cfg: dict[str, Any], kp_player_id: str,
                 pl_player_ids: list[str]) -> dict[str, Any]:
    """重新排站位：谁当守秘人、谁当玩家。

    这就是"今天你当 KP，明天他当 KP"的落点。凭据按**玩家**继承，
    所以换位置不会把谁的 API Key 弄丢；
    跨周目记忆本来就按 player_id 存，自动跟着人走。
    """
    from . import roster as roster_mod

    old_by_player: dict[str, dict[str, Any]] = {}
    old_roles: dict[str, str] = {}
    for s in cfg.get("seats", []):
        pid = (s.get("profile") or {}).get("player_id") or s.get("seat_id")
        old_by_player[pid] = s
        old_roles[pid] = s.get("kind", "")

    new_seats: list[dict[str, Any]] = []

    kp = make_kp_seat(roster_mod.get(kp_player_id))
    if kp_player_id in old_by_player:
        _carry_credentials(kp, old_by_player[kp_player_id],
                           old_roles.get(kp_player_id) != "KP")
    new_seats.append(kp)

    seen = {kp_player_id}
    idx = 0
    for pid in pl_player_ids:
        if pid in seen:            # 同一个人不能既当 KP 又当玩家
            continue
        seen.add(pid)
        seat = make_pl_seat(idx, entry=roster_mod.get(pid))
        if pid in old_by_player:
            _carry_credentials(seat, old_by_player[pid],
                               old_roles.get(pid) != "PL")
        new_seats.append(seat)
        idx += 1

    cfg["seats"] = new_seats
    return cfg


def default_config() -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        # 默认 1 名守秘人 + 3 名玩家，都来自内置名册
        "seats": [make_kp_seat(), make_pl_seat(0), make_pl_seat(1), make_pl_seat(2)],
        "module": {
            "id": "",              # 空 = 自由跑团（无模组）
            "scene_id": "",        # 空 = 从第一幕开始
            "premise": "",         # 自由跑团时的开场设定，供 KP 使用
        },
        "options": {
            "max_rounds": 40,          # 单次自动推进的回合上限（防费用失控）
            "turn_delay_ms": 400,      # 回合间隔，便于观察
            "history_keep": 8,         # 每座位保留的逐字历史轮数 —— 这是最大的成本项
            "memory_topk": 10,         # L2 编年史检索条数
            "memory_mode": "index",    # index = 只给索引+联想召回；full = 直接摊开整棵树
            "rules_detail": "lean",    # lean | standard | full —— 规则注入详细度
            "linter_retry": 1,         # 元层命中后的静默重写次数
            "parallel_pl": True,       # 多名 PL 并行生成（更快，但瞬时并发高）
            "table_talk": True,        # 回合之间插一轮桌边点名对话
            "table_talk_exchanges": 2, # 桌边来回几次（0 = 关闭，最省）
            "chargen_chat_rounds": 2,  # 车卡时先在桌上聊几轮再定妆（0 = 直接出卡）
            "chargen_method": "roll",  # roll = 掷骰车卡；pointbuy = 购点车卡
            "pointbuy_total": 480,     # 购点总预算（八项属性之和）
            "pointbuy_min": 40,
            "pointbuy_max": 90,
            "age_adjust": True,        # 按规则书做年龄补正（EDU 增强检定等）
            "chargen_audit": True,     # 车卡后守秘人审卡（夹带东西会被逮住）
            # 车卡违规时回炉重车几次（0 = 不回炉，直接由引擎裁剪到合规）。
            # 只给一次就够：违规清单是具体的，能改的第一次就改了。
            "chargen_retry": 1,
            "combat_by_dex": True,     # 战斗轮按 DEX 顺序而非同时行动
            "retrospective": True,     # 散场后让每个玩家复盘（跨周目记忆的生长点）
            "reveal_after_end": True,  # 结局后解禁模组原文给玩家看
            "teatime_rounds": 2,       # 散场茶话会来回几次
            # —— 桌上的钟 ——
            # 引擎自己管时间，每一轮把"哪天是哪天"的对照表算好给 AI。
            # 模型自己推日子一定会把什么都叫"昨天"，这是唯一靠谱的解法。
            "minutes_per_round": 10,   # 每轮默认过去多久（0 = 完全由守秘人说了算）
            "start_time": "",          # 开场时刻，如 "1925-10-03 20:00"（留空取模组或默认）
            "clock_past_days": 7,      # 对照表往前列几天
            "clock_future_days": 3,    # 对照表往后列几天
            "auto_start": False,
        },
        "pricing": {                   # 界面上的费用估算，单位：元 / 百万 token
            "input": 2.0,
            "input_cached": 0.2,
            "output": 8.0,
        },
        "ui": {"last_session": "", "theme": "dark"},
    }


# ---------------------------------------------------------------- 读写

def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def migrate_roster(cfg: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把座位里**已经不在名册上**的 player_id 换成有效的人。

    为什么需要这一步：内置名册改过之后（改了网名、换了人），
    老配置里的 id 就成了悬空引用——界面上表现为"守秘人未选"，
    跑起来则会静默换成别人。这不能靠让用户删配置解决。

    按位置重映射，并且**不动凭据**：API Key 是人配的，
    不能因为换了名字就把谁的 Key 弄丢。
    """
    from . import roster as roster_mod
    valid = {e.id for e in roster_mod.load_roster()}
    notes: list[str] = []
    pl_i = 0
    for s in cfg.get("seats", []):
        prof = s.setdefault("profile", {})
        pid = str(prof.get("player_id") or "").strip()
        if pid in valid:
            if s.get("kind") == "PL":
                pl_i += 1
            continue
        person = (roster_mod.default_kp() if s.get("kind") == "KP"
                  else roster_mod.default_pl(pl_i))
        if s.get("kind") == "PL":
            pl_i += 1
        notes.append(f"{s.get('display_name') or s.get('seat_id')}"
                     f"（{pid or '空'}）→ {person.handle}")
        prof["player_id"] = person.id
        prof["player_name"] = person.display()
        prof["handle"] = person.handle
        prof.setdefault("table_voice", person.voice)
        prof.setdefault("playstyle", person.playstyle)
        prof.setdefault("rigor", person.rigor)
        prof.setdefault("rigor_note", person.rigor_note())
    return cfg, notes


def load_config() -> dict[str, Any]:
    p = config_path()
    if not p.exists():
        cfg = default_config()
        save_config(cfg)
        return cfg
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # 配置损坏时不要静默清空用户数据，改名备份后重建
        try:
            p.replace(p.with_suffix(".json.broken"))
        except Exception:
            pass
        cfg = default_config()
        save_config(cfg)
        return cfg
    cfg = _deep_merge(default_config(), raw)
    merged, notes = migrate_roster(cfg)
    if notes:
        merged["_migration_notes"] = notes
        save_config(merged)
    return merged


def save_config(cfg: dict[str, Any], incoming_keys: dict[str, str] | None = None) -> dict[str, Any]:
    """保存配置。若某座位的 api_key 回传 KEEP 哨兵或脱敏串，则保留磁盘上的原值。"""
    cfg = copy.deepcopy(cfg)
    old = {}
    p = config_path()
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    old_keys = {s.get("seat_id"): s.get("api_key", "") for s in old.get("seats", [])}
    for s in cfg.get("seats", []):
        sid = s.get("seat_id")
        key = (s.get("api_key") or "").strip()
        if key == KEEP or (key and set(key) <= set("*•") ) or key == mask_key(old_keys.get(sid, "")):
            s["api_key"] = old_keys.get(sid, "")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


# ---------------------------------------------------------------- 座位工具

def mask_key(key: str) -> str:
    key = key or ""
    if not key:
        return ""
    if len(key) <= 10:
        return "•" * len(key)
    return f"{key[:6]}{'•' * 6}{key[-4:]}"


# ---------------------------------------------------------------- 模型名缓存

def norm_base(url: str) -> str:
    """归一化 Base URL，用作缓存键（大小写、末尾斜杠、/chat/completions 都不算数）。"""
    u = (url or "").strip().rstrip("/").lower()
    if u.endswith("/chat/completions"):
        u = u[: -len("/chat/completions")]
    return u


def remember_models(cfg: dict[str, Any], base_url: str, models: list[str]) -> None:
    """记住某个端点拉到过的模型名。

    模型名是最难记的一栏（`DeepSeek-V4-Flash-Vision-Exp` 这种），
    拉过一次就存下来，下次断网、或者端点暂时抽风，也照样能点着选。
    """
    key = norm_base(base_url)
    models = [m for m in (models or []) if m]
    if not key or not models:
        return
    ui = cfg.setdefault("ui", {})
    cache = ui.setdefault("model_cache", {})
    old = [m for m in (cache.get(key) or []) if m not in models]
    cache[key] = (list(models) + old)[:120]
    while len(cache) > 20:                     # 别让缓存无限长大
        cache.pop(next(iter(cache)), None)


def cached_models(cfg: dict[str, Any], base_url: str) -> list[str]:
    return [str(m) for m in
            ((cfg.get("ui") or {}).get("model_cache", {}).get(norm_base(base_url)) or [])]


def seats(cfg: dict[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
    out = [s for s in cfg.get("seats", []) if s.get("enabled", True)]
    if kind:
        out = [s for s in out if s.get("kind") == kind]
    return out


def find_seat(cfg: dict[str, Any], seat_id: str) -> dict[str, Any] | None:
    for s in cfg.get("seats", []):
        if s.get("seat_id") == seat_id:
            return s
    return None


def next_pl_id(cfg: dict[str, Any]) -> str:
    used = {s.get("seat_id") for s in cfg.get("seats", [])}
    i = 1
    while f"pl_{i}" in used:
        i += 1
    return f"pl_{i}"


def public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """给前端的配置副本：密钥一律脱敏，原始 key 永不出进程。"""
    out = copy.deepcopy(cfg)
    for s in out.get("seats", []):
        s["api_key"] = mask_key(s.get("api_key", ""))
        s["api_key_set"] = bool((find_seat(cfg, s["seat_id"]) or {}).get("api_key"))
    out["_providers"] = copy.deepcopy(PROVIDERS)
    out["_paths"] = {
        "app_root": str(app_root()),
        "data_root": str(data_root()),
        "modules_root": str(modules_root()),
    }
    return out


def seat_ready(seat: dict[str, Any]) -> tuple[bool, str]:
    """检查座位是否配置完整，返回 (是否就绪, 原因)。"""
    provider = seat.get("provider") or "deepseek"
    if provider not in PROVIDERS:
        return False, f"未知提供方 {provider}"
    if not (seat.get("model") or "").strip():
        return False, "未填写模型名"
    if PROVIDERS[provider]["needs_key"]:
        if not (seat.get("base_url") or "").strip():
            return False, "未填写 Base URL"
        if not (seat.get("api_key") or "").strip():
            return False, "未填写 API Key"
    return True, ""
