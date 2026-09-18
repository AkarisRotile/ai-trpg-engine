"""界面缓存清理。

pywebview 用的是常驻的 Edge 用户目录，它会**自作聪明地缓存** index.html /
app.js / style.css。结果就是：换了新版本的 exe，窗口里跑的还是上一版的界面，
而且怎么点都没用——因为磁盘上的文件其实已经是新的了。

所以这里给界面文件算个指纹，和上次启动比对；对不上就把缓存目录删掉。
只删缓存，不碰 cookies / 设置之类的其它东西。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

UI_FILES = ("index.html", "app.js", "style.css")
CACHE_SUBDIRS = ("Cache", "Code Cache", "GPUCache", "Service Worker")


def fingerprint(ui_dir: Path) -> str:
    h = hashlib.sha1()
    for name in UI_FILES:
        f = ui_dir / name
        if f.exists():
            st = f.stat()
            h.update(f"{name}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]


def clear_if_stale(index: Path, data_root: Path) -> bool:
    """界面文件变了就清一次缓存。返回是否真的清了。"""
    try:
        stamp = fingerprint(index.parent)
        mark = data_root / ".ui-version"
        old = mark.read_text(encoding="utf-8").strip() if mark.exists() else ""
        if old == stamp:
            return False

        profile = data_root / ".webview" / "EBWebView" / "Default"
        for sub in CACHE_SUBDIRS:
            target = profile / sub
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(stamp, encoding="utf-8")
        return True
    except Exception:
        return False      # 清不掉不是致命问题，顶多看到旧界面，不该拦住启动
