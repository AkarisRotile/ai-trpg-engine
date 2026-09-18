"""GUI 栈冒烟测试：验证 pywebview + pythonnet(EdgeChromium) 能真的开出窗口。

用法：<venv>\\Scripts\\python.exe tools\\smoke_gui.py
成功时打印 SMOKE OK 并以退出码 0 结束。
"""

from __future__ import annotations

import sys
import threading
import time

import webview

HOLD_SECONDS = 3.0


def _closer() -> None:
    time.sleep(HOLD_SECONDS)
    for w in list(webview.windows):
        try:
            w.destroy()
        except Exception:
            pass


def main() -> int:
    try:
        import importlib.metadata as md
        print(f"pywebview {md.version('pywebview')} / pythonnet {md.version('pythonnet')}")
    except Exception:
        pass

    webview.create_window(
        "pywebview 冒烟测试",
        html=(
            "<body style='background:#12141a;color:#7ee787;font:16px/1.6 system-ui;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            "<div><b>pywebview + EdgeChromium 工作正常</b><br>"
            "<span style='color:#8b949e'>窗口将在 3 秒后自动关闭</span></div></body>"
        ),
        width=480,
        height=200,
    )
    webview.start(_closer)
    print("SMOKE OK: 窗口已创建并正常关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
