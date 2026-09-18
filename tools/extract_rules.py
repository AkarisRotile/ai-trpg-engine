"""把规则书 PDF 抽取成纯文本，供引擎检索。

产物：
  data/rules/full/<书名>.txt      —— 带 [[page N]] 页码标记的全文
  data/rules/full/index.json      —— 每页首行摘要，便于人工翻查

注意：`full/` 下的内容**不会**被自动注入提示词（400 页塞不进上下文），
它作为可检索的参考库存在，由 retrieve_rules() 按需取段落。
真正每轮注入的是 engine/rules.py 里的速查表 + 用户放在 data/rules/ 根目录的自备规则。

用法：<venv>\\Scripts\\python.exe tools\\extract_rules.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import config as cfgmod  # noqa: E402

PDF_CANDIDATES = [
    "克苏鲁的呼唤第七版守秘人规则书 Version2002.pdf",
    "克苏鲁的呼唤第七版调查员手册1.20.pdf",
]


def extract(pdf: Path, out_dir: Path) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    pages: list[str] = []
    index: list[dict] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = text.replace("\r\n", "\n").strip()
        pages.append(f"[[page {i}]]\n{text}")
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        index.append({"page": i, "head": first[:60], "chars": len(text)})

    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / f"{pdf.stem}.txt"
    txt_path.write_text("\n\n".join(pages), encoding="utf-8")
    return {"file": txt_path.name, "pages": len(pages),
            "chars": sum(len(p) for p in pages), "index": index}


def main() -> int:
    full_dir = cfgmod.data_root() / "rules" / "full"
    results = []
    found = 0
    for name in PDF_CANDIDATES:
        pdf = ROOT / name
        if not pdf.exists():
            print(f"  跳过（未找到）：{name}")
            continue
        found += 1
        print(f"  抽取中：{name} …")
        info = extract(pdf, full_dir)
        results.append({"source": name, **{k: v for k, v in info.items() if k != "index"}})
        (full_dir / f"{Path(name).stem}.pages.json").write_text(
            json.dumps(info["index"], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"    -> {info['file']}  {info['pages']} 页 / {info['chars']:,} 字")

    if not found:
        print("没有找到规则书 PDF。把 PDF 放到项目根目录再运行本脚本。")
        return 1

    (full_dir / "index.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成。全文位于 {full_dir}")
    print("提醒：full/ 不会被自动注入提示词，仅供按需检索。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
