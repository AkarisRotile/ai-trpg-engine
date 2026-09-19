"""插件宿主。

一个插件就是 `data/plugins/<id>/` 里的一堆文件，最少要有一个 `plugin.json`：

    data/plugins/thinking-viewer/
      plugin.json      清单
      page.html        它自己的页面（可选）
      ui.js            页面逻辑（可选）

为什么放在 `data/` 而不是 exe 里：`data/` 是用户的东西，升级 exe 不会丢，
也不用为了装一个插件重新打包。规则书、名册、模组都是这个道理。

**不做沙箱。** 这是本地单机程序，插件等于用户自己装的代码，
和浏览器扩展是一个性质。要挡的不是"插件干坏事"，
而是"插件把界面搞崩"：插件的界面代码由界面那边用 try/catch 包住，
出错就在插件管理页标红，并且能一键禁用。
引擎侧钩子（改回合流程那种）风险高得多，这版不开。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config as cfgmod

# 合法的"面"。插件自己挑一个，界面按这个决定怎么摆放它。
SURFACES = ("modal", "window", "panel")

# 认得的坑位。同一件事可以有好几个插件做，谁当值由用户挑。
CAPABILITIES: dict[str, str] = {
    "thinking": "看模型的思维链和引擎改了什么",
    "battle": "画战斗格子和先攻顺序",
}

_ID_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,48}$")
_CAP_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# 插件能读的文件类型。挡住 .py / .exe 这类不该由界面直接取的东西。
_ASSET_SUFFIXES = {".html", ".js", ".css", ".json", ".txt", ".svg", ".png",
                   ".jpg", ".jpeg", ".webp", ".gif"}


def plugins_root() -> Path:
    return cfgmod.data_root() / "plugins"


def state_path() -> Path:
    return cfgmod.data_root() / "plugins.json"


def _safe_id(pid: str) -> str:
    pid = (pid or "").strip()
    return pid if _ID_RE.match(pid) else ""


# ---------------------------------------------------------------- 启停状态

def load_state() -> dict[str, bool]:
    return {str(k): bool(v) for k, v in (load_raw().get("enabled") or {}).items()}


def load_active() -> dict[str, str]:
    """每个坑位现在是谁当值。键是坑位名，值是插件 id。"""
    raw = load_raw().get("active") or {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def load_raw() -> dict[str, Any]:
    p = state_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return d if isinstance(d, dict) else {}


def save_state(state: dict[str, bool]) -> None:
    _write_raw({**load_raw(), "enabled": state})


def save_active(active: dict[str, str]) -> None:
    _write_raw({**load_raw(), "active": active})


def _write_raw(d: dict[str, Any]) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 读清单

@dataclass
class Plugin:
    id: str
    name: str = ""
    version: str = ""
    author: str = ""
    description: str = ""
    surface: str = "modal"
    # 坑位。同一件事可以有好几个插件做，谁当值由用户挑。
    capability: str = ""
    entry_page: str = ""
    entry_script: str = ""
    # 插件可以要求引擎打开某个选项。启停时跟着开关走。
    engine_options: dict[str, Any] = field(default_factory=dict)
    enabled: bool = False
    is_active: bool = False
    error: str = ""
    dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name or self.id, "version": self.version,
            "author": self.author, "description": self.description,
            "surface": self.surface, "enabled": self.enabled,
            "capability": self.capability, "is_active": self.is_active,
            "page": self.entry_page, "script": self.entry_script,
            "has_page": bool(self.entry_page), "has_script": bool(self.entry_script),
            "engine_options": dict(self.engine_options),
            "error": self.error, "dir": self.dir,
        }


def _read_manifest(d: Path) -> Plugin:
    p = Plugin(id=d.name, dir=str(d))
    mf = d / "plugin.json"
    if not mf.exists():
        p.error = "缺 plugin.json"
        return p
    try:
        raw = json.loads(mf.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        p.error = f"plugin.json 读不动：{e}"
        return p
    if not isinstance(raw, dict):
        p.error = "plugin.json 的最外层得是一个对象"
        return p

    p.name = str(raw.get("name") or d.name)
    p.version = str(raw.get("version") or "")
    p.author = str(raw.get("author") or "")
    p.description = str(raw.get("description") or "")

    surface = str(raw.get("surface") or "modal").strip().lower()
    p.surface = surface if surface in SURFACES else "modal"
    if surface and surface not in SURFACES:
        p.error = f"不认识的 surface「{surface}」，只能是 {'/'.join(SURFACES)}"

    cap = str(raw.get("capability") or "").strip()
    p.capability = cap if _CAP_RE.match(cap) else ""

    entry = raw.get("entry") or {}
    if isinstance(entry, dict):
        page = str(entry.get("page") or "").strip()
        script = str(entry.get("script") or "").strip()
    else:
        page = script = ""
    # 相对路径只允许指向插件自己的目录，不许往外爬
    for rel in (page, script):
        if rel and (".." in rel or rel.startswith("/") or rel.startswith("\\")):
            p.error = "entry 里的路径不许往外爬"
            return p
    p.entry_page = page
    p.entry_script = script

    opts = raw.get("engine_options")
    if isinstance(opts, dict):
        p.engine_options = {str(k): v for k, v in opts.items()}
    return p


def list_plugins() -> list[Plugin]:
    """扫一遍插件目录，带上启停状态和谁当值。永远不抛异常。"""
    root = plugins_root()
    state = load_state()
    active = load_active()
    out: list[Plugin] = []
    if not root.is_dir():
        return out
    try:
        dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                      key=lambda x: x.name)
    except OSError:
        return out
    for d in dirs:
        if d.name.startswith("."):
            continue
        try:
            p = _read_manifest(d)
        except Exception as e:  # noqa: BLE001
            p = Plugin(id=d.name, dir=str(d), error=f"读清单时出错：{e}")
        p.enabled = bool(state.get(p.id))
        out.append(p)

    # 谁当值：用户挑过就用挑的，没挑过就是第一个开着的。
    # 挑过的那个被停用或者删了，会自动落到下一个，不会留个空坑。
    for cap in CAPABILITIES:
        members = [p for p in out if p.capability == cap and p.enabled and not p.error]
        if not members:
            continue
        pick = next((p for p in members if p.id == active.get(cap)), members[0])
        pick.is_active = True
    return out


def active_for(cap: str) -> str:
    """这个坑位现在是谁当值。没插件占坑就返回空串。"""
    return next((p.id for p in list_plugins() if p.capability == cap and p.is_active), "")


def set_enabled(pid: str, enabled: bool) -> dict[str, Any]:
    """开关一个插件。顺带把它要求的引擎选项一起设上/撤下。"""
    pid = _safe_id(pid)
    if not pid:
        return {"ok": False, "message": "插件 id 不合法"}
    hit = next((p for p in list_plugins() if p.id == pid), None)
    if hit is None:
        return {"ok": False, "message": f"没有这个插件：{pid}"}
    if hit.error:
        return {"ok": False, "message": f"这个插件有问题，先修好再开：{hit.error}"}

    state = load_state()
    state[pid] = bool(enabled)
    try:
        save_state(state)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"启停状态没存下去：{e}"}

    # 关掉的正好是当值的那个，就让位给同坑位的下一个
    if not enabled and hit.capability and load_active().get(hit.capability) == pid:
        act = load_active()
        act.pop(hit.capability, None)
        try:
            save_active(act)
        except Exception:  # noqa: BLE001
            pass

    notes = apply_engine_options()
    return {"ok": True,
            "message": "，".join(notes) or ("已启用" if enabled else "已停用"),
            "plugin": {**hit.to_dict(), "enabled": bool(enabled)}}


def set_active(cap: str, pid: str) -> dict[str, Any]:
    """指定某个坑位由谁当值。传空串就是让它自己落到第一个开着的。"""
    cap = (cap or "").strip()
    pid = (pid or "").strip()
    if cap not in CAPABILITIES:
        return {"ok": False, "message": f"没有这个坑位：{cap}"}
    members = [p for p in list_plugins() if p.capability == cap]
    if not members:
        return {"ok": False, "message": f"还没有插件占「{cap}」这个坑"}

    act = load_active()
    if pid:
        hit = next((p for p in members if p.id == pid), None)
        if hit is None:
            return {"ok": False, "message": f"{pid} 没有占「{cap}」这个坑"}
        if hit.error:
            return {"ok": False, "message": f"这个插件有问题，先修好：{hit.error}"}
        if not hit.enabled:
            # 挑一个没开的当值那就顺手开上，不然用户会以为没生效
            r = set_enabled(pid, True)
            if not r.get("ok"):
                return r
        act[cap] = pid
    else:
        act.pop(cap, None)
    try:
        save_active(act)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"没存下去：{e}"}

    notes = apply_engine_options()
    winner = active_for(cap)
    return {"ok": True,
            "message": f"「{CAPABILITIES[cap]}」现在由 {winner or '无'} 当值"
                       + ("，" + "，".join(notes) if notes else ""),
            "active": winner}


def apply_engine_options() -> list[str]:
    """把所有插件要求的引擎选项重算一遍写进配置。

    规则：没占坑的插件，开着就生效；占了坑的，只有当值那个生效。
    关掉占坑的插件时，坑位要求的选项要跟着回落，不能留在那儿不生效还以为开着。
    """
    items = list_plugins()
    active = load_active()

    # 插件碰过的键先全清成 False，再把该开的打开。
    # 这样插件被删掉之后，它留下的选项不会永远卡在 True。
    touched: set[str] = set()
    for p in items:
        touched.update(p.engine_options.keys())
    want: dict[str, Any] = {}
    for p in items:
        if p.error or not p.enabled or not p.engine_options:
            continue
        if p.capability and not p.is_active:
            continue
        want.update(p.engine_options)

    cfg = cfgmod.load_config()
    opts = cfg.setdefault("options", {})
    changed = False
    for k in touched:
        v = want.get(k, False)
        if opts.get(k) != v:
            opts[k] = v
            changed = True
    if not changed:
        return []
    try:
        cfgmod.save_config(cfg)
        return ["引擎选项已跟着插件调整"]
    except Exception as e:  # noqa: BLE001
        return [f"引擎选项没设上：{e}"]


def read_asset(pid: str, rel: str) -> dict[str, Any]:
    """读插件自己的一个文件，给界面用。

    只让读插件目录里的东西，也不给读 .py 这类不该由界面直接取的文件。
    """
    pid = _safe_id(pid)
    if not pid:
        return {"ok": False, "message": "插件 id 不合法"}
    rel = (rel or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return {"ok": False, "message": "路径不合法"}
    p = plugins_root() / pid / rel
    if p.suffix.lower() not in _ASSET_SUFFIXES:
        return {"ok": False, "message": f"这类文件不给读：{p.suffix}"}
    if not p.is_file():
        return {"ok": False, "message": f"找不到 {rel}"}
    try:
        # 图片按 base64 走，其余按文本走
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}:
            import base64
            b = p.read_bytes()
            mime = ("image/svg+xml" if p.suffix.lower() == ".svg"
                    else f"image/{p.suffix.lower().lstrip('.')}")
            return {"ok": True, "kind": "binary",
                    "data": f"data:{mime};base64," + base64.b64encode(b).decode("ascii")}
        return {"ok": True, "kind": "text", "data": p.read_text(encoding="utf-8")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"读不出来：{e}"}


def enabled_ids() -> list[str]:
    return [p.id for p in list_plugins() if p.enabled and not p.error]
