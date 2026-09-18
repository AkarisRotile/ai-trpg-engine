"""出错记录。

为什么单独搞一个：打包成 exe 之后是 --windowed，**没有控制台**，
stderr 打出去就没了。界面上的报错提示条一关也就没了。
于是真出事的时候——接口连不上、模型名填错、界面自己崩了——
用户说"报错了"，而我能翻的地方只有一份 session.json，什么都看不到。

所以这里把所有出错都落到 `data/logs/error.log`（追加，纯文本，能直接看），
顺带在内存里留最近若干条给界面显示。

**Key 一律打码之后再写**：错误信息里很可能带着请求 URL 和 Authorization，
那是绝不能落盘的。
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config as cfgmod

_LOCK = threading.Lock()
_MAX_KEEP = 400          # 文件里最多留这么多行，超了从头截
_RECENT: list[dict[str, str]] = []
_RECENT_MAX = 60

# 任何像密钥的东西，写盘前先打码
_SCRUB = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]+"),
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{4})[A-Za-z0-9]+"),
    re.compile(r"(?i)(authorization[\"'\s:=]+bearer\s+\S{4})\S+"),
    re.compile(r"(?i)(api[_-]?key[\"'\s:=]+)([A-Za-z0-9_\-]{6})\S+"),
]


def scrub(text: str) -> str:
    out = str(text or "")
    for rx in _SCRUB:
        out = rx.sub(lambda m: m.group(1) + "****", out)
    return out


def log_path() -> Path:
    p = cfgmod.data_root() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p / "error.log"


def log(where: str, message: str, detail: str = "") -> None:
    """记一条。任何时候都不该因为"记日志失败"而把主流程带崩。"""
    try:
        where = scrub(where)[:120]
        message = scrub(message)[:600]
        detail = scrub(detail)[:2000]
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {where} | {message}"
        if detail:
            line += "\n    " + detail.replace("\n", "\n    ")
        with _LOCK:
            _RECENT.append({"ts": ts, "where": where, "message": message,
                            "detail": detail})
            del _RECENT[:-_RECENT_MAX]
            p = log_path()
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            # 文件别无限长：超过就只留后一半
            try:
                if p.stat().st_size > 512 * 1024:
                    kept = p.read_text(encoding="utf-8", errors="replace").splitlines()
                    p.write_text("\n".join(kept[-_MAX_KEEP:]) + "\n", encoding="utf-8")
            except Exception:
                pass
    except Exception:
        pass


def recent(n: int = 20) -> list[dict[str, Any]]:
    with _LOCK:
        return list(_RECENT[-max(1, n):])


def tail(n: int = 80) -> str:
    """读文件末尾若干行，给"看看到底报了什么"用。"""
    try:
        p = log_path()
        if not p.exists():
            return ""
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max(1, n):])
    except Exception:
        return ""


def clear() -> None:
    with _LOCK:
        _RECENT.clear()
        try:
            p = log_path()
            if p.exists():
                p.unlink()
        except Exception:
            pass
