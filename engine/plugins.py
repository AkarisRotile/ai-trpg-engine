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

_ID_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,48}$")
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
    p = state_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(d, dict):
        return {}
    return {str(k): bool(v) for k, v in (d.get("enabled") or {}).items()}


def save_state(state: dict[str, bool]) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"enabled": state}, ensure_ascii=False, indent=2),
                 encoding="utf-8")


# ---------------------------------------------------------------- 读清单

@dataclass
class Plugin:
    id: str
    name: str = ""
    version: str = ""
    author: str = ""
    description: str = ""
    surface: str = "modal"
    entry_page: str = ""
    entry_script: str = ""
    # 插件可以要求引擎打开某个选项。启停时跟着开关走。
    engine_options: dict[str, Any] = field(default_factory=dict)
    enabled: bool = False
    error: str = ""
    dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name or self.id, "version": self.version,
            "author": self.author, "description": self.description,
            "surface": self.surface, "enabled": self.enabled,
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
    """扫一遍插件目录，带上启停状态。永远不抛异常。"""
    root = plugins_root()
    state = load_state()
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
    return out


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

    notes = []
    if hit.engine_options:
        cfg = cfgmod.load_config()
        opts = cfg.setdefault("options", {})
        for k, v in hit.engine_options.items():
            opts[k] = v if enabled else False
        try:
            cfgmod.save_config(cfg)
            notes.append("插件要求的引擎选项已" + ("打开" if enabled else "关掉"))
        except Exception as e:  # noqa: BLE001
            notes.append(f"引擎选项没设上：{e}")
    return {"ok": True, "message": "，".join(notes) or "已保存",
            "plugin": {**hit.to_dict(), "enabled": bool(enabled)}}


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
