"""扫描版 PDF → 文本：把每一页渲染成图，交给视觉模型识别。

为什么需要它：扫描版 PDF 的文字是**画**，`pypdf` 抽不出任何文本层。
唯一的出路是把页渲成图，交给看得懂图的模型。

设计取舍：

  · **一次性的，不是运行时的。** OCR 发生在「导入模组」这一刻，
    跑团时读的是识别好的纯文本。所以它慢一点、贵一点都可以接受。
  · **逐页缓存 + 断点续跑。** 一页一个 txt 存在 `.ocr/` 里，
    50 页跑到第 30 页断了，重跑会跳过前 29 页。
  · **不许猜。** 提示词里最要紧的一条是「看不清就写 [看不清]」。
    对跑团来说，**编错一个专有名词比漏掉一段更危险**——
    守秘人会拿着错名字当真相用一整个晚上。
  · **保留原始页图**（可选），方便人工抽查。
  · 识别出来的模组会在开头挂一条**声明**，告诉守秘人这是 OCR 结果、
    专有名词可能有误——让它别对拼写过于自信。
"""

from __future__ import annotations

import base64
import io
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config as cfgmod


# ---------------------------------------------------------------- 配置

OCR_DEFAULTS: dict[str, Any] = {
    "provider": "custom",
    "base_url": "",
    "api_key": "",
    "model": "",
    "dpi": 150,              # 渲染分辨率；扫描件 150 够用，太小会糊
    "max_side": 1800,        # 长边上限，压一压省 token
    "pages_per_call": 1,     # 一次发几页（>1 更省，但准确率略降）
    "keep_images": False,    # 是否把渲染出的页图留档，便于人工核对
    "max_pages": 200,        # 单次最多识别多少页，防跑飞
}


def vision_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or cfgmod.load_config()
    out = dict(OCR_DEFAULTS)
    out.update(cfg.get("vision") or {})
    return out


# ---------------------------------------------------------------- 判断要不要 OCR

def probe_pdf(path: Path, sample: int = 6) -> dict[str, Any]:
    """看这个 PDF 是文本型还是扫描型。

    采样若干页统计文本量：文本型 PDF 每页几千字，扫描型基本是 0。
    """
    import pypdf
    info: dict[str, Any] = {"path": str(path), "pages": 0, "avg_chars": 0.0,
                            "needs_ocr": False, "sampled": []}
    try:
        reader = pypdf.PdfReader(str(path))
        n = len(reader.pages)
    except Exception as e:  # noqa: BLE001
        info["error"] = f"打不开这个 PDF：{type(e).__name__}: {e}"
        return info

    info["pages"] = n
    if n == 0:
        info["error"] = "这个 PDF 一页都没有"
        return info
    idx = sorted({int(i * n / max(1, sample)) for i in range(min(sample, n))})
    total = 0
    for i in idx:
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            text = ""
        chars = len(re.sub(r"\s", "", text))
        info["sampled"].append({"page": i + 1, "chars": chars})
        total += chars
    info["avg_chars"] = total / max(1, len(idx))
    # 每页平均不到 50 个字，基本可以断定是扫描件
    info["needs_ocr"] = info["avg_chars"] < 50
    return info


# ---------------------------------------------------------------- 渲染

def _render_page(doc: Any, index: int, dpi: int, max_side: int) -> bytes:
    """把 PDF 的一页渲成 PNG 字节。"""
    page = doc[index]
    zoom = max(0.5, min(4.0, dpi / 72.0))
    pix = page.get_pixmap(matrix=__import__("pymupdf").Matrix(zoom, zoom), alpha=False)
    data = pix.tobytes("png")
    if max_side and max(pix.width, pix.height) > max_side:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        scale = max_side / max(img.width, img.height)
        img = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
    return data


def render_pages(path: Path, pages: list[int] | None, dpi: int = 150,
                 max_side: int = 1800) -> list[tuple[int, bytes]]:
    try:
        import pymupdf
    except ImportError as e:
        raise RuntimeError("没装 PyMuPDF，渲染不了 PDF：pip install PyMuPDF") from e
    doc = pymupdf.open(str(path))
    try:
        idx = pages if pages is not None else list(range(len(doc)))
        return [(i, _render_page(doc, i, dpi, max_side)) for i in idx]
    finally:
        doc.close()


# ---------------------------------------------------------------- 提示词

OCR_PROMPT = """\
这是一本跑团模组（TRPG scenario）的扫描页。请把它上面的文字**原样**抄下来。

规则：
1. **只抄，不要翻译、不要总结、不要改写、不要补充你自己知道的内容。**
2. 保留原来的结构：标题单独一行，段落之间空行，列表保留列表。
3. **看不清的地方就写 `[看不清]`，绝对不要猜。**
   尤其是人名、地名、专有名词——猜错一个名字，守秘人会拿着错名字跑一整晚。
4. 插图、表格、边框、页码都不要描述；如果这一页主要是插图，
   就写一行 `[插图]` 然后抄上面的图注文字。
5. 中英文混排按原样保留。
6. 直接输出抄下来的文字，不要加"以下是…"这类开场白。

如果这一页是封面/版权页/目录，照样抄，但不用美化。"""


# ---------------------------------------------------------------- OCR 主流程

@dataclass
class OcrResult:
    ok: bool = False
    path: str = ""
    pages_done: int = 0
    pages_total: int = 0
    from_cache: int = 0
    failed: list[int] = field(default_factory=list)
    text: str = ""
    error: str = ""
    out_dir: str = ""


def ocr_pdf(pdf: Path, vcfg: dict[str, Any], *,
            out_dir: Path | None = None,
            pages: list[int] | None = None,
            progress: Callable[[str, int, int], None] | None = None,
            stop: threading.Event | None = None) -> OcrResult:
    """逐页 OCR。带缓存，断了能接着跑。

    `out_dir` 默认是 `<pdf 同级>/.ocr/`，里面一页一个 txt。
    """
    from .llm import LLMError, OpenAICompatClient

    res = OcrResult(path=str(pdf), out_dir=str(out_dir or (pdf.parent / ".ocr")))
    cache = Path(res.out_dir)
    cache.mkdir(parents=True, exist_ok=True)
    img_dir = cache / "images"

    try:
        import pymupdf
    except ImportError as e:
        res.error = f"没装 PyMuPDF，渲染不了 PDF：{e}"
        return res

    if not (vcfg.get("base_url") and vcfg.get("model")):
        res.error = ("还没有配视觉模型。OCR 需要一个看得懂图的接口——"
                     "在「模组」弹窗里的 OCR 面板填 Base URL / Key / 模型名。"
                     "（DeepSeek 官方目前没有视觉模型，所以这一步通常要另配一家。）")
        return res

    base_url = str(vcfg.get("base_url") or "")
    if base_url.startswith("mock") or vcfg.get("provider") == "mock":
        # 离线自检用：不联网，但完整走一遍渲染→切页→缓存→组装
        from .llm import MockClient
        client = MockClient(vcfg.get("model") or "mock-ocr")
    else:
        client = OpenAICompatClient(base_url, vcfg.get("api_key", ""),
                                    vcfg["model"], label=vcfg["model"])

    doc = pymupdf.open(str(pdf))
    try:
        total = len(doc)
        idx = pages if pages is not None else list(range(total))
        idx = [i for i in idx if 0 <= i < total][: int(vcfg.get("max_pages", 200))]
        res.pages_total = len(idx)
        dpi = int(vcfg.get("dpi", 150))
        max_side = int(vcfg.get("max_side", 1800))
        per_call = max(1, int(vcfg.get("pages_per_call", 1)))

        texts: dict[int, str] = {}
        batch: list[tuple[int, bytes]] = []

        def flush() -> None:
            if not batch:
                return
            content: list[dict[str, Any]] = [{"type": "text", "text": OCR_PROMPT}]
            for i, data in batch:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,"
                                         + base64.b64encode(data).decode("ascii")},
                })
            msgs = [{"role": "user", "content": content}]
            try:
                out = client.chat(msgs, max_tokens=4000, temperature=0.0,
                                  mock_phase="ocr")
                body = (out.text or "").strip()
            except LLMError as e:
                for i, _ in batch:
                    res.failed.append(i)
                body = ""
                page_nums = "+".join(str(i + 1) for i, _ in batch)
                res.error = f"第 {page_nums} 页识别失败：{e.message}"
            except Exception as e:  # noqa: BLE001
                for i, _ in batch:
                    res.failed.append(i)
                body = ""
                res.error = f"第 {batch[0][0] + 1} 页识别出错：{type(e).__name__}: {e}"

            if body:
                # 一次多页时按 `--- 第 N 页 ---` 分隔；模型不写就整段给第一页
                chunks = re.split(r"^\s*-{2,}\s*第\s*(\d+)\s*页\s*-{2,}\s*$",
                                  body, flags=re.M)
                if len(chunks) > 1:
                    for k in range(1, len(chunks), 2):
                        try:
                            pno = int(chunks[k]) - 1
                        except ValueError:
                            continue
                        texts[pno] = chunks[k + 1].strip()
                    for i, _ in batch:
                        texts.setdefault(i, "")
                else:
                    texts[batch[0][0]] = body
                    for i, _ in batch[1:]:
                        texts.setdefault(i, "")
            batch.clear()

        for i in idx:
            if stop is not None and stop.is_set():
                res.error = res.error or "被中止"
                break
            page_file = cache / f"p{i + 1:04d}.txt"
            if page_file.exists() and page_file.stat().st_size > 0:
                texts[i] = page_file.read_text(encoding="utf-8")
                res.from_cache += 1
                if progress:
                    progress(f"第 {i + 1} 页（已有缓存）", i + 1, total)
                continue

            data = _render_page(doc, i, dpi, max_side)
            if vcfg.get("keep_images"):
                img_dir.mkdir(exist_ok=True)
                (img_dir / f"p{i + 1:04d}.png").write_bytes(data)
            batch.append((i, data))
            if len(batch) >= per_call:
                flush()
            if progress:
                progress(f"第 {i + 1} 页", i + 1, total)
        flush()

        # 落盘（一页一个文件，断了能接着跑）
        for i, body in sorted(texts.items()):
            if body:
                (cache / f"p{i + 1:04d}.txt").write_text(body, encoding="utf-8")
        res.pages_done = sum(1 for i in idx if texts.get(i))
        parts = []
        for i in sorted(texts):
            if texts.get(i):
                parts.append(f"[[page {i + 1}]]\n{texts[i]}")
        res.text = "\n\n".join(parts)
        res.ok = res.pages_done > 0
        if not res.ok and not res.error:
            res.error = "一页都没识别出来，检查一下模型名和这个接口是否支持图片输入。"
        return res
    finally:
        doc.close()


# ---------------------------------------------------------------- 组装成模组

OCR_NOTICE = (
    "> ⚠ **这份文本是从扫描版 PDF 自动识别的（OCR）**，可能存在错字，\n"
    "> 尤其是人名、地名、专有名词。如果某个名字看起来奇怪，"
    "请以原始 PDF 为准，或者问一下跑团的人。\n"
)


def build_module_from_ocr(pdf: Path, text: str, module_id: str = "",
                          title: str = "", scenes: int = 6) -> Path:
    """把识别出来的全文组装成一个可跑的模组文件夹。

    切场景用 `##` 标题；没有标题就按长度均分，保证 KP 至少有骨架可用。
    """
    root = cfgmod.modules_root() / (module_id or pdf.stem)
    root.mkdir(parents=True, exist_ok=True)
    (root / "scenes").mkdir(exist_ok=True)

    (root / f"{pdf.stem}（OCR原文）.md").write_text(OCR_NOTICE + text, encoding="utf-8")

    body = re.sub(r"^\[\[page \d+\]\]\s*$", "", text, flags=re.M).strip()
    marks = list(re.finditer(r"^##\s+(.+?)\s*$", body, re.M))
    parts: list[tuple[str, str]] = []
    if len(marks) >= 2:
        for k, m in enumerate(marks):
            end = marks[k + 1].start() if k + 1 < len(marks) else len(body)
            parts.append((m.group(1).strip(), body[m.end():end].strip()))
    else:
        lines = body.splitlines()
        size = max(1, len(lines) // max(1, scenes))
        for k in range(max(1, scenes)):
            chunk = "\n".join(lines[k * size:(k + 1) * size]).strip()
            if chunk:
                parts.append((f"第 {k + 1} 段", chunk))

    for k, (name, chunk) in enumerate(parts, start=1):
        safe = re.sub(r'[\\/:*?"<>|]+', "_", name)[:30] or f"第{k}段"
        (root / "scenes" / f"scene_{k:02d}_{safe}.md").write_text(
            f"# {name}\n\n{OCR_NOTICE}\n{chunk}", encoding="utf-8")

    (root / "secret_truth.md").write_text(
        "# 幕后真相\n\n" + OCR_NOTICE
        + "（这是 OCR 出来的全文，守秘人请自己从中摘出真相与暗线。\n"
          "如果原文里有单独标注的真相章节，把它整理到本文件里更清楚。）\n\n" + text,
        encoding="utf-8")

    import yaml
    (root / "module_info.yaml").write_text(yaml.safe_dump({
        "title": title or pdf.stem,
        "system": "COC7",
        "era": "",
        "players": "",
        "summary": f"（由扫描版 PDF 自动识别生成：{pdf.name}，共 {len(parts)} 幕）\n"
                   f"建议手工补一下 era（时代背景）和 players（推荐人数），"
                   f"守秘人审卡时会用到 era。",
        "source_pdf": pdf.name,
        "ocr": True,
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")

    (root / "handouts").mkdir(exist_ok=True)
    return root
