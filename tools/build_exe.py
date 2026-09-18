"""把引擎打包成可双击运行的 exe。

用 onedir 而不是 onefile：
  · onefile 每次启动都要把上百 MB 解包到临时目录，冷启动慢十几秒；
  · onedir 直接跑，启动快，出了问题也看得见文件。
交付时整个 dist/COC跑团引擎 文件夹一起给出去即可。

关键点：
  · `--add-data ui;ui`  —— 界面是外挂资源，打包后要从 _MEIPASS/ui 里找（见 config.ui_dir）
  · 界面依赖 pywebview + pythonnet(EdgeChromium)，必须显式带上 winforms 后端
  · `data/` **不打包** —— 它是用户数据（配置、模组、存档、记忆），
    必须躺在 exe 同级目录里，这样升级程序不会覆盖掉跑团记录

用法：<venv>\\Scripts\\python.exe tools\\build_exe.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "COC跑团引擎"

EXCLUDE = [
    "tkinter", "unittest", "pydoc", "doctest", "test",
    "numpy", "pandas", "matplotlib", "scipy",
    "PyInstaller", "setuptools", "pip",
]

HIDDEN = [
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "clr_loader",
    "clr_loader.util",
    "pythonnet",
    "clr",
    "yaml",
    "requests",
    "pypdf",                       # 文本型模组 PDF 用
    "pymupdf",                     # 扫描版模组 PDF 渲染成图用
    "fitz",
    "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "PIL.ImageFilter",
    "openpyxl",                    # 写 COC7 Excel 角色卡用
    "openpyxl.styles", "openpyxl.utils",
    "engine",
    "engine.app", "engine.agents", "engine.turns", "engine.session",
    "engine.prompts", "engine.memory", "engine.player_memory",
    "engine.glossary", "engine.rules", "engine.dice", "engine.chargen",
    "engine.module_lib", "engine.linter", "engine.llm", "engine.config",
    "engine.spoiler", "engine.roster", "engine.sheet", "engine.ocr",
]


def main() -> int:
    if not (ROOT / "main.py").exists():
        print("找不到 main.py")
        return 1

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("没有安装 PyInstaller。请执行：\n"
              "  .venv\\Scripts\\python.exe -m pip install pyinstaller")
        return 1

    ui = ROOT / "ui"
    if not (ui / "index.html").exists():
        print("找不到 ui/index.html")
        return 1

    dist = ROOT / "dist"
    work = ROOT / "build"
    spec = ROOT / "build"

    print("=" * 66)
    print(f"打包 {APP_NAME}（onedir，含界面资源）")
    print("=" * 66)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--onedir",
        "--windowed",
        "--add-data", f"{ui}{os.pathsep}ui",
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(spec),
        "--collect-submodules", "webview",
        "--collect-submodules", "clr_loader",
    ]
    # 图标：用 tools/make_icon.py 画出来的 d20
    icon = ROOT / "assets" / "icon.ico"
    if not icon.exists():
        try:
            subprocess.run([sys.executable, str(ROOT / "tools" / "make_icon.py")],
                           cwd=str(ROOT), check=False)
        except Exception:
            pass
    if icon.exists():
        cmd += ["--icon", str(icon)]
        print(f"使用图标：{icon}")
    else:
        print("（没找到图标，用默认的）")
    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    for e in EXCLUDE:
        cmd += ["--exclude-module", e]
    cmd.append(str(ROOT / "main.py"))

    started = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        print(f"\n打包失败，退出码 {proc.returncode}")
        return proc.returncode

    out_dir = dist / APP_NAME
    exe = out_dir / f"{APP_NAME}.exe"
    print(f"\n构建耗时 {time.time() - started:.0f} 秒")

    if not exe.exists():
        print(f"没找到产物 {exe}")
        return 1

    # 数据目录跟着 exe 走，预建空壳并附一份说明
    data = out_dir / "data"
    for sub in ("modules", "memory", "players", "templates", "rules"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    (data / "runtime" / "sessions").mkdir(parents=True, exist_ok=True)

    # ★ 空白角色卡模板必须一起带上，否则打包版写不出 Excel 卡
    tpl_src = ROOT / "data" / "templates" / "COC7空白卡.xlsx"
    if not tpl_src.exists():
        tpl_src = ROOT / "COC7空白卡CY22.4 Plus.xlsx"
    if tpl_src.exists():
        shutil.copyfile(tpl_src, data / "templates" / "COC7空白卡.xlsx")
        print("已附带 COC7 空白角色卡模板")
    else:
        print("⚠ 没找到空白角色卡模板，打包版将无法生成 Excel 角色卡")

    # 把演示模组一起带上，装好就能按开始
    demo_src = ROOT / "data" / "modules" / "demo_洋馆之夜"
    if demo_src.is_dir():
        shutil.copytree(demo_src, data / "modules" / "demo_洋馆之夜",
                        dirs_exist_ok=True)
        print("已附带演示模组：洋馆之夜")

    (out_dir / "读我.txt").write_text(
        "AI 跑团引擎 · COC 第七版\n"
        "=" * 40 + "\n\n"
        "双击「" + APP_NAME + ".exe」启动。\n\n"
        "第一次使用：\n"
        "  1. 点右上角「设置」，给守秘人与每个玩家各配一个接口。\n"
        "     · 想用 DeepSeek 官方：接口选「DeepSeek 官方」，粘上 Key 即可。\n"
        "     · 想用别的服务：接口选「自定义」，填 Base URL / Key / 模型名。\n"
        "     · 想先零成本试跑：接口选「离线模拟」，不联网不花钱。\n"
        "     · 每个席位是独立的，可以让不同 AI 走不同服务商。\n"
        "  2. 点「模组」，选一个剧本（已附带「洋馆之夜」）。\n"
        "  3. 依次点「① 车卡」→「② 开局」→「▶ 自动推进」。\n\n"
        "你自己的模组放这里：\n"
        "  data\\modules\\你的模组名\\\n"
        "      module_info.yaml   模组信息\n"
        "      secret_truth.md    幕后真相（只有 KP 看得到，玩家绝对看不到）\n"
        "      scenes\\*.md        各幕内容\n"
        "      handouts\\*         线索道具\n\n"
        "跑团记录、存档、每个 AI 的记忆都在 data\\ 下面，\n"
        "升级程序时只要不动 data\\ 就不会丢。\n",
        encoding="utf-8")

    size = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
    print(f"\n产物：{exe}")
    print(f"体积：{size / 1024 / 1024:.1f} MB")
    print("\n验证：双击上面的 exe，或用 tools/smoke_exe.py 做自动检查。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
