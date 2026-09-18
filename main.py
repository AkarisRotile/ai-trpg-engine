"""AI 跑团引擎 · 启动器。

双击 exe 时执行的就是这个文件：
  1. 建一个 App（引擎门面，不含任何 UI 依赖）
  2. 开一个 pywebview 原生窗口，把 ui/index.html 装进去
  3. 窗口内的 JS 通过 window.pywebview.api.* 直接调 Python

之所以用「Python 引擎 + HTML 界面」而不是纯原生控件：
跑团日志需要大量富文本、折叠、过滤、配色分声部，HTML/CSS 做这个又快又好；
而引擎层完全不知道界面的存在，将来换前端不用改一行引擎代码。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# 打包成 exe 后，_MEIPASS 是解包目录；开发期就是本文件所在目录
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fatal(msg: str, detail: str = "") -> None:
    """启动失败时给一个人能看懂的提示，而不是一闪而过的黑框。"""
    text = f"{msg}\n\n{detail}" if detail else msg
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "AI 跑团引擎 · 启动失败", 0x10)
    except Exception:
        print(text, file=sys.stderr)
    sys.exit(1)


def _reset_ui_cache(index: Path) -> None:
    """界面文件变了就清一次浏览器缓存（否则新版本会显示上一版的界面）。"""
    try:
        from engine import config as cfgmod
        from engine.uicache import clear_if_stale
        if clear_if_stale(index, cfgmod.data_root()):
            print("[ui] 界面文件已更新，清掉浏览器缓存", file=sys.stderr)
    except Exception:
        pass


def main() -> int:
    try:
        import webview
    except ImportError as e:
        _fatal("缺少界面依赖 pywebview。\n请在项目目录执行：\n"
               "  .venv\\Scripts\\python.exe -m pip install pywebview pythonnet",
               str(e))
        return 1

    try:
        from engine import config as cfgmod
        from engine.app import App
    except Exception as e:
        _fatal("引擎加载失败。", traceback.format_exc())
        return 1

    index = cfgmod.ui_dir() / "index.html"
    if not index.exists():
        _fatal(f"找不到界面文件：{index}\n"
               f"请确认 ui/ 目录和 exe 在一起。")
        return 1

    try:
        app = App()
    except Exception:
        _fatal("引擎初始化失败。", traceback.format_exc())
        return 1

    debug = bool(os.environ.get("COC_DEBUG"))
    smoke = bool(os.environ.get("COC_SMOKE"))

    _reset_ui_cache(index)

    try:
        window = webview.create_window(
            cfgmod.APP_TITLE,
            url=str(index),
            js_api=app,
            width=1560,
            height=980,
            min_size=(1120, 720),
            background_color="#0d0f14",
            text_select=True,
        )
    except Exception:
        _fatal("创建窗口失败。", traceback.format_exc())
        return 1

    # 关窗前先存档，别让用户白跑一局
    try:
        def _on_closing() -> bool:
            try:
                app.save_now()
            except Exception:
                pass
            return True          # 返回 True 表示允许关闭
        window.events.closing += _on_closing
    except Exception:
        pass

    # 首次启动给一句人话提示，别让用户对着空白面板发呆
    def _welcome() -> None:
        try:
            import time
            time.sleep(0.8)
            window.evaluate_js(
                "window.__cocPush && window.__cocPush("
                "{type:'system',text:'引擎已就绪。左侧「设置」里配置席位，"
                "然后依次点 ① 车卡 → ② 开局。想零成本试跑就把接口选成「离线模拟」。'});"
            )
        except Exception:
            pass

    def _runner() -> None:
        """exe 自检模式（COC_SMOKE=1）：在**打包后的真实程序**里验证界面链路。

        因为打包成了 --windowed（没有控制台），结果写成 JSON 文件再由外部脚本读取。
        """
        if not smoke:
            _welcome()
            return
        import json
        import threading
        import time

        def probe() -> None:
            result = {"ok": False, "steps": [], "error": ""}
            time.sleep(6.0)
            try:
                def js(code):
                    return window.evaluate_js(code)

                result["steps"].append(
                    ["桥接注入", bool(js("typeof window.pywebview !== 'undefined' "
                                        "&& !!window.pywebview.api"))])
                # mods 里带上每个模组解析出几幕：打包版的 docread
                # （.doc / .docx / .xls / .xlsx）到底能不能用，看这个最直接
                js("window.__p = null; window.pywebview.api.bootstrap().then(function(b){"
                   "window.__p = JSON.stringify({seats:(b.config.seats||[]).length,"
                   "providers:Object.keys(b.config._providers||{}),"
                   "modules:(b.modules||[]).length, rules_full:b.rules_full,"
                   "mods:(b.modules||[]).map(function(m){return m.id+':'+"
                   "(m.error?('ERR '+m.error):((m.scene_count||0)+' scenes'));})});"
                   "}).catch(function(e){ window.__p = JSON.stringify({err:String(e)}); });")
                raw = None
                for _ in range(60):
                    time.sleep(0.25)
                    raw = js("window.__p")
                    if raw:
                        break
                data = json.loads(raw) if raw else {}
                result["steps"].append(["引擎 bootstrap", bool(data.get("seats"))])
                result["steps"].append(
                    ["三套接口齐全", len(data.get("providers") or []) == 3])
                result["steps"].append(["席位渲染", int(js("document.querySelectorAll('.seat').length") or 0) >= 1])
                result["steps"].append(
                    ["模组弹窗", int(js("document.querySelectorAll('#moduleList .module-card').length") or 0) >= 1])
                # 有没有哪个模组解析失败（打包版最容易在这里翻车：docread 的
                # python-docx / xlrd / olefile 漏了 hidden import 就会 ERR）
                bad_mods = [m for m in (data.get("mods") or []) if "ERR" in str(m)]
                result["steps"].append(["模组解析无报错", not bad_mods])
                toasts = js("Array.from(document.querySelectorAll('#toasts .toast'))"
                            ".map(function(t){return t.textContent;})") or []
                result["toasts"] = toasts
                result["steps"].append(
                    ["无错误提示", not any(("失败" in t or "错误" in t) for t in toasts)])
                result["data"] = data
                result["ok"] = all(s[1] for s in result["steps"])
            except Exception as e:  # noqa: BLE001
                result["error"] = f"{type(e).__name__}: {e}"
            finally:
                try:
                    out = os.environ.get("COC_SMOKE_OUT")
                    if out:
                        with open(out, "w", encoding="utf-8") as f:
                            json.dump(result, f, ensure_ascii=False, indent=1)
                except Exception:
                    pass
                try:
                    for w in list(webview.windows):
                        w.destroy()
                except Exception:
                    pass

        threading.Thread(target=probe, daemon=True).start()

    try:
        webview.start(_runner, http_server=True, debug=debug,
                      private_mode=False, storage_path=str(cfgmod.data_root() / ".webview"))
    except TypeError:
        # 老版本 pywebview 不支持某些参数
        webview.start(http_server=True, debug=debug)
    except Exception:
        _fatal("窗口启动失败。", traceback.format_exc())
        return 1

    if smoke:
        out = os.environ.get("COC_SMOKE_OUT")
        try:
            import json
            with open(out, "r", encoding="utf-8") as f:
                return 0 if json.load(f).get("ok") else 1
        except Exception:
            return 1

    # 关窗即存档，别让用户白跑一局
    try:
        app.save_now()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
