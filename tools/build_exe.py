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

# 默认把成品装到项目根目录（双击就能找到）；
# 加 --portable 则留在 dist\ 下，方便整个文件夹压缩分享。
PORTABLE = False

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
    "docx",                        # .docx 模组（python-docx）
    "docx.api", "docx.opc.constants",
    "xlrd",                        # 老式 .xls 表格（薄暮之国时间线那种）
    "olefile",                     # 老式 .doc（OLE 复合文档，自己抽正文）
    "engine",
    "engine.app", "engine.agents", "engine.turns", "engine.session",
    "engine.prompts", "engine.memory", "engine.player_memory",
    "engine.glossary", "engine.rules", "engine.dice", "engine.chargen",
    "engine.module_lib", "engine.linter", "engine.llm", "engine.config",
    "engine.spoiler", "engine.roster", "engine.sheet", "engine.ocr",
    "engine.docread", "engine.study", "engine.uicache", "engine.clock",
    "engine.errlog",
]


def main() -> int:
    global PORTABLE
    import argparse
    ap = argparse.ArgumentParser(description="打包成 exe")
    ap.add_argument("--portable", action="store_true",
                    help="成品留在 dist\\ 下（方便整个文件夹压缩分享），"
                         "而不是装到项目根目录")
    args = ap.parse_args()
    PORTABLE = args.portable

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

    # ★ 装到项目根目录，双击就能找到，不用翻 dist\
    #   onedir 的 exe 必须和 _internal 在一起，所以两个一起搬。
    #   （exe 依赖 _internal 里的 DLL 和插件，少一个都起不来。）
    if PORTABLE:
        target_exe = exe
        install_dir = out_dir
        print(f"（--portable：成品留在 {out_dir}，方便整个文件夹压缩分享）")
    else:
        target_exe = ROOT / f"{APP_NAME}.exe"
        install_dir = ROOT
        internal_src = out_dir / "_internal"
        internal_dst = ROOT / "_internal"
        if internal_dst.exists():
            shutil.rmtree(internal_dst, ignore_errors=True)
        if internal_src.is_dir():
            shutil.move(str(internal_src), str(internal_dst))
        if target_exe.exists():
            target_exe.unlink()
        shutil.move(str(exe), str(target_exe))
        shutil.rmtree(out_dir, ignore_errors=True)
        try:
            if dist.exists() and not any(dist.iterdir()):
                dist.rmdir()
        except OSError:
            pass
        print(f"已装到根目录：{target_exe}")

    # 数据目录跟 exe 同级
    data = install_dir / "data"
    for sub in ("modules", "memory", "players", "templates", "rules", "studies"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    (data / "runtime" / "sessions").mkdir(parents=True, exist_ok=True)

    # 空白角色卡模板必须一起带上，否则打包版写不出 Excel 卡
    tpl_src = ROOT / "data" / "templates" / "COC7空白卡.xlsx"
    if not tpl_src.exists():
        tpl_src = ROOT / "COC7空白卡CY22.4 Plus.xlsx"
    tpl_dst = data / "templates" / "COC7空白卡.xlsx"
    if tpl_src.exists():
        if tpl_src.resolve() == tpl_dst.resolve():
            print("已附带 COC7 空白角色卡模板（本来就在位）")
        else:
            shutil.copyfile(tpl_src, tpl_dst)
            print("已附带 COC7 空白角色卡模板")
    else:
        print("⚠ 没找到空白角色卡模板，打包版将无法生成 Excel 角色卡")

    # 把演示模组一起带上，装好就能按开始
    demo_src = ROOT / "data" / "modules" / "demo_洋馆之夜"
    demo_dst = data / "modules" / "demo_洋馆之夜"
    if demo_src.is_dir() and demo_src.resolve() != demo_dst.resolve():
        shutil.copytree(demo_src, demo_dst, dirs_exist_ok=True)
        print("已附带演示模组：洋馆之夜")
    elif demo_dst.is_dir():
        print("已附带演示模组：洋馆之夜（本来就在位）")

    (install_dir / "读我.txt").write_text(
        "AI 跑团引擎 · 克苏鲁的呼唤第七版\n"
        + "=" * 44 + "\n\n"
        "【这是什么】\n"
        "  一个跑团工具：守秘人（KP）和所有玩家（PL）都由 AI 扮演。\n"
        "  你只要开着看，一桌人自己就跑起来了。\n\n"
        "【怎么开始】\n"
        "  1. 双击「" + APP_NAME + ".exe」\n"
        "  2. 先什么都不用配，直接点左上角「🎬 开新团」\n"
        "     选《洋馆之夜》，一路点「① 车卡 → ② 开局 → ▶ 自动推进」\n"
        "     —— 默认就是「离线模拟」模式，不联网、不花钱，\n"
        "        但车卡、掷骰、记忆、桌边聊天全流程都是真的。\n\n"
        "【想让它真的聪明起来】\n"
        "  离线模拟只是占位实现，剧情很水。要真跑，得给它接上模型：\n"
        "  1. 点右上角「⚙️ 设置」\n"
        "  2. 给守秘人和每个玩家各配一个接口（每个席位是独立的，\n"
        "     可以走不同服务商、用不同 Key）：\n"
        "       · DeepSeek 官方 —— 接口选它，粘上 Key 就行\n"
        "       · 自定义 —— 填任意 OpenAI 兼容端点的 URL / Key\n"
        "  3. 模型名不用手打：点模型那一栏下面的「🔍 拉取模型列表」，\n"
        "     点着选就好。\n"
        "  4. 配完点「测试全部连接」确认通不通\n\n"
        "【你自己的模组放这里】\n"
        "  data\\modules\\你的模组名\\\n"
        "      module_info.yaml   模组信息（标题、简介、推荐人数）\n"
        "      era                时代背景——审卡时会用到，\n"
        "                         比如「1925 年·美国马萨诸塞州」\n"
        "      secret_truth.md    幕后真相（只有 KP 看得到，玩家绝对看不到）\n"
        "      scenes\\*.md        各幕内容\n"
        "      handouts\\*         线索道具\n"
        "  单个文件直接丢进 data\\modules\\ 也行——.md .txt .pdf .docx .doc\n"
        "  .xlsx .xls 都能读。网上下的本子经常是一堆老格式混在一个文件夹里\n"
        "  （正文 .doc、怪物资料 .docx、时间线 .xls、地图 .png），\n"
        "  整个文件夹丢进去就能认。\n"
        "  扫描版的 PDF 可以在「📁 模组」里用 OCR 面板识别（要另配一个\n"
        "  看得懂图的模型，DeepSeek 官方没有）。\n\n"
        "【新模组好不好跑？先让 KP 讲给你听】\n"
        "  顶栏点「📖 研读室」：那里只有你和守秘人，没有玩家。\n"
        "  它会通读一遍，然后给你一份不藏着的通读报告——幕后真相、\n"
        "  绕不过去的骨架、最容易翻车的地方、它想加什么料，全摊开说。\n"
        "  看完还能接着追问。读过的本会存下来，以后拿它开团不用重读。\n\n"
        "【数据在哪】\n"
        "  全在 data\\ 里面：配置、模组、存档、每个 AI 的记忆、\n"
        "  生成好的 Excel 角色卡（data\\players\\<网名>\\）。\n"
        "  升级程序时只要不动 data\\ 就不会丢。\n\n"
        "【想改成你们自己的名字】\n"
        "  右上角「🎭 名册与站位」里可以直接改名册上那 7 个人的网名和性格，\n"
        "  或者直接编辑 data\\roster.yaml。\n"
        "  改成你们自己人，跑起来才是你们那桌。\n",
        encoding="utf-8")

    # 只统计"这个程序本身的体积"：exe + _internal。
    # 装到根目录时不能整个 rglob 根目录——那会把 .venv 和源码都算进去。
    if PORTABLE:
        size = sum(f.stat().st_size for f in install_dir.rglob("*") if f.is_file())
    else:
        size = target_exe.stat().st_size
        internal = install_dir / "_internal"
        if internal.is_dir():
            size += sum(f.stat().st_size for f in internal.rglob("*") if f.is_file())
    print(f"\n产物：{target_exe}")
    print(f"体积：{size / 1024 / 1024:.1f} MB")
    if not PORTABLE:
        print(f"\n双击这个就能玩：{target_exe}")
        print(f"（同目录的 _internal\\ 是程序自己的东西，别删也别动；"
              f"data\\ 是你的数据，升级时只要不动它就不会丢。）")
    print("验证：双击上面的 exe，或用 tools/smoke_exe.py 做自动检查。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
