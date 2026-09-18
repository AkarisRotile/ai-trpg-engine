"""界面冒烟测试：验证「pywebview 窗口 → HTML/JS → Python 引擎」这条链真的通。

它做的是真机检查，不是静态检查：
  1. 真开一个窗口加载 ui/index.html
  2. 在页面里执行 JS，读回真实 DOM（席位卡、过滤器、下拉框、模块列表）
  3. 从页面里直接调 window.pywebview.api.bootstrap()，确认桥接能取到数据
  4. 检查页面上有没有冒出错误提示条（boot 失败会弹 toast）
  5. 自动关窗，打印结论

用法：<venv>\\Scripts\\python.exe tools\\smoke_ui.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webview  # noqa: E402

from engine import config as cfgmod      # noqa: E402
from engine.app import App               # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((ok, label, detail))
    mark = "  [OK]  " if ok else "  [FAIL]"
    print(f"{mark} {label}" + (f" —— {detail}" if detail else ""))


def probe(window) -> None:
    time.sleep(6.0)                      # 等页面加载 + boot() 跑完

    def js(code, default=None):
        try:
            return window.evaluate_js(code)
        except Exception as e:  # noqa: BLE001
            print(f"    evaluate_js 失败: {e}")
            return default

    try:
        print("\n界面链路检查")

        bridge = js("typeof window.pywebview !== 'undefined' && !!window.pywebview.api")
        check(bool(bridge), "pywebview 桥接已注入")

        # 页面真的能从 Python 拿到数据。
        # evaluate_js 不会等 Promise，所以用"写进全局变量再轮询"的办法取值。
        js(
            "window.__probe = null;"
            "window.pywebview.api.bootstrap().then(function(b){"
            "  window.__probe = JSON.stringify({"
            "    ok: true,"
            "    seats: (b.config.seats || []).length,"
            "    providers: Object.keys(b.config._providers || {}),"
            "    modules: (b.modules || []).length,"
            "    glossary: b.glossary,"
            "    rules_full: b.rules_full"
            "  });"
            "}).catch(function(e){ window.__probe = JSON.stringify({ok:false, err:String(e)}); });"
        )
        raw = None
        for _ in range(60):
            time.sleep(0.25)
            raw = js("window.__probe")
            if raw:
                break
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            data = {}

        check(bool(data.get("ok")), "页面能调通引擎 bootstrap()",
              json.dumps(data, ensure_ascii=False)[:170])
        check(int(data.get("seats") or 0) >= 1, "配置里读到了席位",
              f"{data.get('seats')} 个")
        check(len(data.get("providers") or []) == 3, "三套接口都在（官方/自定义/离线）",
              "、".join(data.get("providers") or []))
        check(bool(data.get("rules_full")), "规则书全文可检索")
        check(int(data.get("modules") or 0) >= 1, "扫到了演示模组",
              f"{data.get('modules')} 个")

        # DOM 真的渲染出来了
        for label, expr, want in [
            ("左侧渲染出货位卡", "document.querySelectorAll('.seat').length", 1),
            ("过滤器渲染完成", "document.querySelectorAll('#filters label').length", 8),
            ("控制台按钮齐全",
             "['btnNewGame','btnPrepare','btnStart','btnAuto','btnStep','btnStop']"
             ".filter(function(i){return document.getElementById(i);}).length", 6),
            ("有开新团向导", "document.querySelectorAll('#modalNewGame').length", 1),
            ("左侧显示当前是哪一局",
             "document.querySelectorAll('#currentGame').length", 1),
            ("左侧有桌面时间的角标",
             "document.querySelectorAll('#clockBadge').length", 1),
            ("右栏标签页渲染完成", "document.querySelectorAll('#rightTabs .tab').length", 5),
            ("设置弹窗有全局选项", "document.querySelectorAll('#optionEditors .field').length", 5),
            ("设置弹窗有席位编辑器",
             "document.querySelectorAll('#seatEditors .seat-editor').length", 1),
            ("模组弹窗渲染出卡片",
             "document.querySelectorAll('#moduleList .module-card').length", 1),
        ]:
            n = js(expr)
            try:
                n = int(n)
            except (TypeError, ValueError):
                n = -1
            check(n >= want, label, f"实得 {n}")

        toasts = js("Array.from(document.querySelectorAll('#toasts .toast'))"
                    ".map(function(t){return t.textContent;})")
        bad = [t for t in (toasts or []) if "失败" in t or "错误" in t]
        check(not bad, "页面没有报错提示", "；".join(bad)[:200] if bad else "干净")

        js("document.querySelector('[data-modal=\"modalSettings\"]').click()")
        time.sleep(1.2)
        check(bool(js("document.getElementById('modalSettings').classList.contains('open')")),
              "设置弹窗能打开")
        js("document.querySelector('#modalSettings [data-close]').click()")

        # 开新团向导：这是主入口，必须真的能打开并渲染出内容
        js("document.getElementById('btnNewGame').click()")
        time.sleep(2.0)
        opened = bool(js("document.getElementById('modalNewGame').classList.contains('open')"))
        check(opened, "点「开新团」能打开向导")
        n_mod = js("document.querySelectorAll('#newGameModules .module-card').length")
        try:
            n_mod = int(n_mod)
        except (TypeError, ValueError):
            n_mod = -1
        check(n_mod >= 2, "向导里列出了「自由跑团 + 模组」", f"{n_mod} 项")
        n_ppl = js("document.querySelectorAll('#plPicker2 .player-chip').length")
        try:
            n_ppl = int(n_ppl)
        except (TypeError, ValueError):
            n_ppl = -1
        check(n_ppl == 7, "向导里列出了名册上的 7 个人", f"{n_ppl} 人")
        summary = js("document.getElementById('newGameSummary').textContent") or ""
        check("守秘人" in summary and "剧本" in summary, "向导最后有确认摘要",
              summary.replace("\n", " ")[:70])

        js("document.querySelector('#modalNewGame [data-close]').click()")

        # 名册与站位：7 个人 + 换庄按钮
        js("document.querySelector('[data-modal=\"modalRoster\"]').click()")
        time.sleep(2.0)
        check(bool(js("document.getElementById('modalRoster').classList.contains('open')")),
              "名册与站位弹窗能打开")
        n_roster = js("document.querySelectorAll('#rosterEditors .seat-editor').length")
        try:
            n_roster = int(n_roster)
        except (TypeError, ValueError):
            n_roster = -1
        check(n_roster == 7, "名册里就是那 7 个人", f"{n_roster} 人")
        n_kp = js("document.querySelectorAll('#kpSelect option').length")
        try:
            n_kp = int(n_kp)
        except (TypeError, ValueError):
            n_kp = -1
        check(n_kp == 7, "守秘人下拉里 7 个人都能选（可以换庄）", f"{n_kp} 项")
        js("document.querySelector('#modalRoster [data-close]').click()")

        # 研读室：只有你和 KP 的那个单人小窗
        js("document.querySelector('[data-modal=\"modalStudy\"]').click()")
        time.sleep(2.0)
        check(bool(js("document.getElementById('modalStudy').classList.contains('open')")),
              "研读室弹窗能打开")
        n_sm = js("document.querySelectorAll('#studyModuleSel option').length")
        try:
            n_sm = int(n_sm)
        except (TypeError, ValueError):
            n_sm = -1
        check(n_sm >= 1, "研读室里能挑模组", f"{n_sm} 项")
        check(bool(js("document.getElementById('btnRunStudy')"
                      "&&document.getElementById('btnStudyAsk')"
                      "&&document.getElementById('btnRereadStudy')")),
              "研读室有「读一遍 / 重读 / 追问」三个按钮")
        js("document.querySelector('#modalStudy [data-close]').click()")

        # ── 回归：名册的 .seat-editor 与设置面板同名 ──
        # collectConfig() 以前用全局选择器扫 .seat-editor，而 boot() 里就已经
        # 渲染过名册（7 个同名节点，没有 [data-k="provider"]），于是取 .dataset
        # 直接抛异常 —— 表现是「保存设置」和「🔍 拉取模型列表」必失败，
        # 填好的 API Key 一个字也存不进去。
        js("document.querySelector('[data-modal=\"modalRoster\"]').click()")
        time.sleep(1.5)
        js("document.querySelector('#modalRoster [data-close]').click()")
        js("document.querySelector('[data-modal=\"modalSettings\"]').click()")
        time.sleep(1.5)
        n_boxes = js("document.querySelectorAll('.seat-editor').length")
        collected = js("(function(){try{var c=collectConfig();"
                       "return 'ok:'+((c.seats||[]).length);}"
                       "catch(e){return 'ERR:'+e.message+' @ '+(e.stack||'');}})()")
        check(str(collected).startswith("ok:"),
              "「保存设置 / 拉取模型」不会被名册的同名样式带崩",
              f"DOM 里 {n_boxes} 个 .seat-editor（含名册那些），collectConfig → {collected}")

        # ── 出错记录面板 ──
        check(bool(js("typeof window.pywebview.api.log_client_error === 'function'"
                      " && typeof window.pywebview.api.recent_errors === 'function'")),
              "界面的报错能被记到文件里（log_client_error / recent_errors）")
        js("document.getElementById('errLogBox').open = true")
        js("document.getElementById('btnRefreshErrors').click()")
        time.sleep(1.2)
        body = js("document.getElementById('errLogBody').textContent") or ""
        check(bool(body.strip()), "设置里有「最近出错」面板且能读出内容",
              body.replace("\n", " ")[:70])

        bg = js("getComputedStyle(document.body).backgroundColor")
        check(bg not in (None, '', 'rgba(0, 0, 0, 0)'), "样式表已加载", str(bg))

    finally:
        time.sleep(0.5)
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:
                pass


def main() -> int:
    app = App()
    index = cfgmod.ui_dir() / "index.html"
    if not index.exists():
        print(f"找不到 {index}")
        return 1

    # 用 private_mode=True：浏览器不留任何缓存，每次拿到的都是磁盘上这一版界面。
    # （之前用常驻 profile，结果测出来的是上一版的 index.html / app.js，
    #   白排查了一轮"为什么改动没生效"。）
    # clear_if_stale 仍然留着，它管的是真机那条 private_mode=False 的路径。
    from engine.uicache import clear_if_stale
    clear_if_stale(index, cfgmod.data_root())

    window = webview.create_window(
        "界面冒烟测试", url=str(index), js_api=app,
        width=1400, height=900, background_color="#0d0f14",
    )
    webview.start(
        lambda: threading.Thread(target=probe, args=(window,), daemon=True).start(),
        http_server=True, private_mode=True)

    print("\n" + "=" * 62)
    failed = [r for r in RESULTS if not r[0]]
    if failed:
        print(f"结果：{len(failed)} 项失败")
        for _, label, detail in failed:
            print(f"   ✗ {label}  {detail}")
    else:
        print(f"结果：全部通过（{len(RESULTS)} 项）")
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
