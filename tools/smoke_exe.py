"""验证**打包后的 exe 本身**能双击跑起来。

为什么要单独测：开发目录里能跑不等于打包后能跑。
最常见的翻车是「界面资源没打进去」和「pythonnet 后端漏了 hidden import」，
这两种在源码模式下都不会暴露。

做法：用 COC_SMOKE=1 启动 exe，它会在真实窗口里做一遍界面链路检查，
把结果写成 JSON，然后自己退出。这里读回来判定。

用法：<venv>\\Scripts\\python.exe tools\\smoke_exe.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "COC跑团引擎"
EXE = ROOT / "dist" / APP_NAME / f"{APP_NAME}.exe"


def main() -> int:
    if not EXE.exists():
        print(f"找不到 {EXE}\n请先运行 tools/build_exe.py")
        return 1

    out = ROOT / "dist" / "_smoke_exe_result.json"
    if out.exists():
        out.unlink()

    env = dict(os.environ)
    env["COC_SMOKE"] = "1"
    env["COC_SMOKE_OUT"] = str(out)

    print("=" * 62)
    print(f"启动打包后的 exe：{EXE.name}")
    print("=" * 62)

    started = time.time()
    proc = subprocess.Popen([str(EXE)], cwd=str(EXE.parent), env=env)

    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        print("  [FAIL] exe 超过 120 秒没有自行退出——可能卡住或没跑起来")
        return 1

    elapsed = time.time() - started
    print(f"  exe 退出码 {proc.returncode}，耗时 {elapsed:.1f} 秒\n")

    if not out.exists():
        print("  [FAIL] 没有产出自检结果文件——说明窗口根本没起来")
        print("         常见原因：界面资源没打进包，或 pythonnet 后端缺失。")
        return 1

    result = json.loads(out.read_text(encoding="utf-8"))
    ok = True
    for label, passed in result.get("steps", []):
        print(f"  [{'OK' if passed else 'FAIL'}] {label}")
        ok = ok and bool(passed)
    if result.get("error"):
        print(f"  [FAIL] 自检异常：{result['error']}")
        ok = False
    if result.get("toasts"):
        print("\n  界面上出现过的提示：")
        for t in result["toasts"]:
            print(f"    · {t}")
    if result.get("data"):
        print(f"\n  引擎数据：{json.dumps(result['data'], ensure_ascii=False)}")

    # 数据目录必须落在 exe 同级，便携且升级不丢
    data_dir = EXE.parent / "data"
    has_cfg = (data_dir / "config.json").exists()
    print(f"  [{'OK' if has_cfg else 'FAIL'}] 数据目录生成在 exe 同级（{data_dir}）")
    ok = ok and has_cfg

    # 空白角色卡模板要在，否则打包版写不出 Excel 卡
    tpl = data_dir / "templates" / "COC7空白卡.xlsx"
    print(f"  [{'OK' if tpl.exists() else 'FAIL'}] COC7 空白角色卡模板已附带")
    ok = ok and tpl.exists()

    # 演示模组要在
    demo = data_dir / "modules" / "demo_洋馆之夜" / "module_info.yaml"
    print(f"  [{'OK' if demo.exists() else 'FAIL'}] 演示模组已附带")
    ok = ok and demo.exists()

    print("\n" + "=" * 62)
    print("结果：打包后的 exe 可以正常启动并通过界面链路检查"
          if ok else "结果：打包后的 exe 存在问题")
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
