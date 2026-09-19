"""立绘与头像。

**这版只管存和取，不生成图。** 谁画、画成什么样是插件的事，
这里负责：图放哪、用什么名字认领、怎么取出来给界面。

    data/portraits/
      pc_余快.png         调查员的立绘
      npc_石泽.jpg        NPC 的
      player_咸鱼.png     玩家本人的头像（跨周目跟着人走）

命名约定（key）：
    pc_<角色名>     调查员
    npc_<名字>      守秘人这边的 NPC
    player_<玩家id> 玩家本人

key 只用来当文件名，会做一次清洗，所以中文没问题，路径分隔符会被换掉。
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

from . import config as cfgmod

# 认得的图片。别的后缀一律不收，免得有人往里塞 .exe。
ALLOWED = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
# 单张上限。立绘再大也用不着超过这个数，界面那边是当 data URL 塞的。
MAX_BYTES = 4 * 1024 * 1024

_BAD = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")


def portraits_root() -> Path:
    return cfgmod.data_root() / "portraits"


def safe_key(key: str) -> str:
    """把 key 洗成能当文件名的样子。洗不干净就返回空串。"""
    k = _BAD.sub("_", str(key or "").strip())
    k = k.strip(". ")
    if not k or len(k) > 96 or k in (".", ".."):
        return ""
    return k


def _find(key: str) -> Path | None:
    """按 key 找一个已存在的图，不关心后缀是什么。"""
    k = safe_key(key)
    if not k:
        return None
    root = portraits_root()
    if not root.is_dir():
        return None
    for suf in ALLOWED:
        p = root / (k + suf)
        if p.is_file():
            return p
    return None


def get(key: str) -> dict[str, Any]:
    """取一张图，返回 data URL。没有就 ok=False，不是错误。"""
    p = _find(key)
    if p is None:
        return {"ok": False, "message": "还没有这张图"}
    try:
        b = p.read_bytes()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"读不出来：{e}"}
    mime = ALLOWED.get(p.suffix.lower(), "image/png")
    return {"ok": True, "key": safe_key(key), "file": p.name,
            "bytes": len(b), "mtime": p.stat().st_mtime,
            "data": f"data:{mime};base64," + base64.b64encode(b).decode("ascii")}


def put(key: str, data: str | bytes) -> dict[str, Any]:
    """存一张图。

    `data` 可以是 data URL（界面传上来的就是这种），也可以是原始字节。
    存之前会看魔数，光看后缀不算数。
    """
    k = safe_key(key)
    if not k:
        return {"ok": False, "message": "这个 key 不能当文件名"}

    raw: bytes
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        s = str(data or "").strip()
        if s.startswith("data:"):
            _, _, body = s.partition(",")
            try:
                raw = base64.b64decode(body, validate=False)
            except Exception:  # noqa: BLE001
                return {"ok": False, "message": "这不是合法的 data URL"}
        else:
            # 也允许直接给 base64
            try:
                raw = base64.b64decode(s, validate=False)
            except Exception:  # noqa: BLE001
                return {"ok": False, "message": "读不出图片内容"}

    if not raw:
        return {"ok": False, "message": "图是空的"}
    if len(raw) > MAX_BYTES:
        return {"ok": False,
                "message": f"图太大了（{len(raw) // 1024} KB），上限 {MAX_BYTES // 1024 // 1024} MB"}

    ext = sniff(raw)
    if ext is None:
        return {"ok": False, "message": "这不像是一张图片（只收 png / jpg / webp / gif）"}

    root = portraits_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"建不了目录：{e}"}

    # 换了格式就把旧的删掉，别在目录里留两张同 key 不同后缀的
    old = _find(k)
    target = root / (k + ext)
    try:
        target.write_bytes(raw)
        if old is not None and old != target:
            old.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"写不进去：{e}"}
    return {"ok": True, "key": k, "file": target.name, "bytes": len(raw),
            "message": "存好了"}


def remove(key: str) -> dict[str, Any]:
    p = _find(key)
    if p is None:
        return {"ok": False, "message": "本来就没有这张图"}
    try:
        p.unlink()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"删不掉：{e}"}
    return {"ok": True, "message": "删了"}


def listing() -> dict[str, dict[str, Any]]:
    """目录里现在有哪些图。键是 key，值是文件信息（不含图片内容）。"""
    root = portraits_root()
    out: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return out
    try:
        files = sorted(root.iterdir(), key=lambda x: x.name)
    except OSError:
        return out
    for p in files:
        if not p.is_file() or p.suffix.lower() not in ALLOWED:
            continue
        stem = p.stem
        try:
            st = p.stat()
        except OSError:
            continue
        out[stem] = {"file": p.name, "bytes": st.st_size,
                     "mtime": st.st_mtime}
    return out


def sniff(raw: bytes) -> str | None:
    """看魔数认格式。光信后缀的话，改个名就能塞别的东西进来。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
        return ".gif"
    if len(raw) > 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return None
