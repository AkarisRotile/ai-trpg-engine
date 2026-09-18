"""把扫描版 PDF 模组识别成可跑的模组文件夹。

用法：

    # 先看看这个 PDF 要不要 OCR
    python tools/ocr_module.py --probe "D:\\模组\\疯狂山脉.pdf"

    # 配上视觉模型（只需配一次，会存进 data\\config.json）
    python tools/ocr_module.py --set-vision \\
        --base-url https://api.example.com/v1 --api-key sk-xxx --model qwen-vl-max

    # 识别整个 PDF，自动生成模组文件夹
    python tools/ocr_module.py "D:\\模组\\疯狂山脉.pdf"

    # 只识别前 20 页 / 指定页 / 强制重做
    python tools/ocr_module.py "xx.pdf" --pages 1-20
    python tools/ocr_module.py "xx.pdf" --redo

识别结果一页一个 txt 存在 `<pdf 同级>\\.ocr\\`，**断了能接着跑**。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import config as cfgmod          # noqa: E402
from engine import ocr as ocr_mod            # noqa: E402


def parse_pages(spec: str, total: int = 0) -> list[int] | None:
    """支持 `1-20` / `3,7,9` / `1-5,10` 这种写法（页码从 1 开始）。"""
    if not spec:
        return None
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            out.extend(range(lo - 1, hi))
        else:
            try:
                out.append(int(part) - 1)
            except ValueError:
                continue
    return sorted({i for i in out if i >= 0}) or None


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描版 PDF → 可跑的模组")
    ap.add_argument("pdf", nargs="?", help="PDF 路径")
    ap.add_argument("--probe", metavar="PDF", help="只看这个 PDF 要不要 OCR")
    ap.add_argument("--set-vision", action="store_true", help="保存视觉模型配置")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--dpi", type=int, default=0)
    ap.add_argument("--max-side", type=int, default=0)
    ap.add_argument("--pages-per-call", type=int, default=0)
    ap.add_argument("--keep-images", action="store_true")
    ap.add_argument("--pages", default="", help="只识别这些页，如 1-20 或 3,7,9")
    ap.add_argument("--redo", action="store_true", help="忽略缓存，全部重做")
    ap.add_argument("--module-id", default="", help="生成的模组文件夹名")
    ap.add_argument("--title", default="", help="模组标题")
    args = ap.parse_args()

    cfg = cfgmod.load_config()

    if args.set_vision:
        v = ocr_mod.vision_config(cfg)
        if args.base_url:
            v["base_url"] = args.base_url
        if args.api_key:
            v["api_key"] = args.api_key
        if args.model:
            v["model"] = args.model
        if args.dpi:
            v["dpi"] = args.dpi
        if args.max_side:
            v["max_side"] = args.max_side
        if args.pages_per_call:
            v["pages_per_call"] = args.pages_per_call
        if args.keep_images:
            v["keep_images"] = True
        cfg["vision"] = v
        cfgmod.save_config(cfg)
        print("视觉模型配置已保存：")
        print(f"  Base URL : {v.get('base_url') or '（空）'}")
        print(f"  模型     : {v.get('model') or '（空）'}")
        print(f"  Key      : {'已设置' if v.get('api_key') else '（空）'}")
        print(f"  DPI      : {v.get('dpi')}   长边上限 {v.get('max_side')}"
              f"   每次发 {v.get('pages_per_call')} 页")
        if not args.pdf and not args.probe:
            return 0

    target = args.probe or args.pdf
    if not target:
        ap.print_help()
        return 0
    pdf = Path(target)
    if not pdf.exists():
        print(f"找不到文件：{pdf}")
        return 1

    info = ocr_mod.probe_pdf(pdf)
    print(f"{pdf.name}：共 {info.get('pages', 0)} 页，"
          f"采样页平均 {info.get('avg_chars', 0):.0f} 字")
    for s in info.get("sampled", []):
        print(f"  第 {s['page']} 页：{s['chars']} 字")
    if info.get("error"):
        print(f"  ⚠ {info['error']}")
        return 1
    if not info.get("needs_ocr"):
        print("→ 这是**文本型** PDF，直接当模组用就行，不用 OCR。")
        if args.probe:
            return 0
    else:
        print("→ 这是**扫描型** PDF，需要 OCR。")
        if args.probe:
            return 0

    vcfg = ocr_mod.vision_config(cfg)
    if not (vcfg.get("base_url") and vcfg.get("model")):
        print("\n还没配视觉模型。先跑一次：")
        print("  python tools/ocr_module.py --set-vision "
              "--base-url https://... --api-key sk-... --model 模型名")
        print("（DeepSeek 官方目前没有视觉模型，这一步通常要另配一家。）")
        return 1

    pages = parse_pages(args.pages, info.get("pages", 0))
    if args.redo:
        cache = pdf.parent / ".ocr"
        if cache.is_dir():
            n = 0
            for f in cache.glob("p*.txt"):
                f.unlink()
                n += 1
            print(f"已清掉 {n} 页缓存")

    last = [0.0]

    def progress(msg: str, i: int, n: int) -> None:
        now = time.time()
        if i == n or now - last[0] > 0.4:
            last[0] = now
            pct = int(i * 100 / max(1, n))
            print(f"\r  {pct:>3}%  {msg:<28}", end="", flush=True)

    print(f"\n开始识别（{len(pages) if pages else info.get('pages', 0)} 页）…")
    res = ocr_mod.ocr_pdf(pdf, vcfg, pages=pages, progress=progress)
    print()
    if not res.ok:
        print(f"识别失败：{res.error}")
        if res.failed:
            print(f"  失败页：{res.failed[:20]}")
        print(f"  已识别 {res.pages_done}/{res.pages_total} 页，"
              f"缓存留在 {res.out_dir}，修好之后重跑会跳过已完成的页。")
        return 1

    root = ocr_mod.build_module_from_ocr(pdf, res.text, args.module_id, args.title)
    print(f"识别完成：{res.pages_done}/{res.pages_total} 页"
          f"（缓存命中 {res.from_cache} 页）")
    print(f"模组已生成：{root}")
    print("\n建议接下来手工做两件事：")
    print("  1. 打开 module_info.yaml，补上 era（时代背景）和 players（推荐人数）"
          "——守秘人审卡时会用到 era")
    print("  2. 扫一眼 scene_*.md 里有没有明显识别错的人名地名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
