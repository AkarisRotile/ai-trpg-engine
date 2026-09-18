"""把打包好的成品做成一个可以直接发给朋友的压缩包。

用法：
    .venv\\Scripts\\python.exe tools\\build_exe.py --portable   # 先出 dist\\COC跑团引擎\\
    .venv\\Scripts\\python.exe tools\\make_share.py             # 再打成 zip

它做的关键一件事是**清场**：打包目录里会混进你本机跑出来的东西——
config.json（里面有你自己的 Base URL）、data\\players\\（每个 AI 的记忆和角色卡）、
data\\runtime\\sessions\\（你的跑团记录）、OCR 缓存、浏览器缓存……
这些一个都不能进分享包。脚本会把它们删掉，再补上空的占位目录，
让收到包的人第一次打开就是干净的一手。

最后它自己会验一遍：包里的 api_key 是不是全空、有没有漏掉的私货。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "COC跑团引擎"
SHARE_NAME = "AI跑团引擎-分享版.zip"

# 这些是"跑出来的东西"，绝对不能进分享包
DIRTY_FILES = [
    "data/config.json",
    "data/roster.yaml",          # 名册是你本机可能改过的，默认那份跟源码一样
    "data/.ui-version",
    "读我.txt.bak",
]
DIRTY_DIRS = [
    "data/players",
    "data/memory",
    "data/studies",
    "data/runtime",
    "data/exports",
    "data/.webview",
    "data/.webview-smoke",
    "data/rules/full",
    "data/rules/ocr",
    "__pycache__",
]
# 这些目录即便清空也要留着（程序会往里写东西）
KEEP_EMPTY = [
    "data/modules",
    "data/players",
    "data/memory",
    "data/studies",
    "data/templates",
    "data/rules",
    "data/runtime/sessions",
]
# 这些是"证据 / 测试产物"，扫到就该报警。
# 注意：只查 _internal\ 以外的部分——那里面是 Python 依赖本身，
# 「Microsoft.Web.WebView2.Core.dll」这种名字会被误判成浏览器缓存。
SUSPECT = re.compile(
    r"(^|/)(config\.json|roster\.yaml|\.webview[^/]*|\.ui-version|\.selftest"
    r"|transcript\.jsonl|session\.json|player\.yaml|_smoke[^/]*)$"
    r"|_result\.json$|\.bak$|\.log$", re.I)


def _is_own(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (ValueError, OSError):
        return False


def clean(install_dir: Path) -> None:
    """把本机跑出来的痕迹从打包目录里清掉。"""
    for rel in DIRTY_FILES:
        p = install_dir / rel
        if p.is_file():
            p.unlink()
            print(f"  清掉 {rel}")
    for rel in DIRTY_DIRS:
        p = install_dir / rel
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            print(f"  清掉 {rel}\\")
    # 顺手清掉所有子目录里的 __pycache__ / .pyc
    for p in list(install_dir.rglob("__pycache__")):
        shutil.rmtree(p, ignore_errors=True)
    for p in list(install_dir.rglob("*.pyc")):
        p.unlink(missing_ok=True)
    for rel in KEEP_EMPTY:
        (install_dir / rel).mkdir(parents=True, exist_ok=True)


def audit(install_dir: Path) -> list[str]:
    """检查这个目录干不干净。返回问题清单。"""
    bad: list[str] = []
    for f in install_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(install_dir).as_posix()
        if rel.startswith("_internal/"):
            continue
        if SUSPECT.search(rel):
            bad.append(rel)
    cfg = install_dir / "data" / "config.json"
    if cfg.exists():
        bad.append("data/config.json（会把你的接口地址带出去）")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="把成品打成分享用压缩包")
    ap.add_argument("--src", default="", help="成品目录（默认 dist\\COC跑团引擎）")
    ap.add_argument("--out", default="", help="输出 zip（默认 dist\\AI跑团引擎-分享版.zip）")
    ap.add_argument("--keep-dirty", action="store_true",
                    help="跳过清场（只在你自己想留档时用，别拿这个分享）")
    args = ap.parse_args()

    src = Path(args.src) if args.src else ROOT / "dist" / APP_NAME
    out = Path(args.out) if args.out else ROOT / "dist" / SHARE_NAME

    if not (src / f"{APP_NAME}.exe").exists():
        print(f"没找到成品：{src}\\{APP_NAME}.exe")
        print("先跑： .venv\\Scripts\\python.exe tools\\build_exe.py --portable")
        return 1
    # 保险：别把项目根目录当成成品目录来清场——那会删掉你自己的规则书全文和存档
    if (src / "main.py").exists() or (src / "engine").is_dir():
        print(f"⚠ {src} 看起来是**项目根目录**，不是打包成品目录。")
        print("  清场会删掉你自己的 data\\rules\\full 和存档，已拒绝执行。")
        print("  成品目录应该是 dist\\COC跑团引擎\\（用 build_exe.py --portable 生成）。")
        return 1

    print(f"打包源：{src}")
    if not args.keep_dirty:
        print("清场：")
        clean(src)

    problems = audit(src)
    if problems:
        print("\n⚠ 这些文件不该出现在分享包里：")
        for p in problems[:20]:
            print(f"   · {p}")
        return 1
    print("清场检查：干净")

    # 附一份源码里的读我.txt（如果成品目录里没有就让 build_exe.py 生成的那份留着）
    if not (src / "读我.txt").exists():
        print("⚠ 成品目录里没有「读我.txt」，建议重跑 build_exe.py")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for sub in KEEP_EMPTY:
            zi = zipfile.ZipInfo(f"{APP_NAME}/{sub}/")
            zi.external_attr = 0o40775 << 16
            z.writestr(zi, b"")
            n += 1
        for f in sorted(src.rglob("*")):
            if f.is_dir():
                continue
            if _is_own(f, out):
                continue
            z.write(f, f"{APP_NAME}/{f.relative_to(src).as_posix()}")
            n += 1

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"\n分享包：{out}")
    print(f"条目：{n} 个　压缩后：{size_mb:.1f} MB")

    # 自己再验一遍：包里的 config 必须是干净的、不能有私货
    prefix = f"{APP_NAME}/"
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        # 只查 _internal\ 之外的部分：那里是 Python 依赖本身
        leaks = [x for x in names
                 if not x[len(prefix):].startswith("_internal/")
                 and SUSPECT.search(x[len(prefix):])]
        if leaks:
            print("\n⚠ 压缩包里还是混进了这些东西：")
            for x in leaks[:20]:
                print(f"   · {x}")
            return 1
        has_cfg = f"{prefix}data/config.json" in names
        print("包内检查：" + ("⚠ 竟然带了 config.json" if has_cfg
                              else "没有 config.json（首次打开会自己生成空白配置）"))
        for need in (f"{prefix}{APP_NAME}.exe",
                     f"{prefix}_internal/python314.dll",
                     f"{prefix}_internal/ui/app.js"):
            if need not in names:
                print(f"⚠ 压缩包里缺 {need}")
                return 1
        print("包内检查：exe / _internal / 界面资源都在")
    print("结论：可以直接发出去。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
