"""模组库：把 `data/modules/` 下的剧本文件夹读取成结构化对象。

信息隔离设计：`secret_truth` 字段只应被 KP 的提示词编译器读取，
PL 的编译路径根本不会触碰它——不是"提示模型别说"，而是**取不到**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import config as cfgmod

SCENE_HEADING = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.M)
TRUTH_FILENAMES = ["secret_truth.md", "secret.md", "truth.md", "幕后真相.md",
                   "真相.md", "keeper_notes.md", "kp.md", "spoiler.md"]


@dataclass
class Scene:
    id: str
    title: str
    body: str
    order: int = 0
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "order": self.order,
                "path": self.path, "chars": len(self.body)}


@dataclass
class Handout:
    id: str
    title: str
    body: str
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "path": self.path,
                "chars": len(self.body)}


@dataclass
class Module:
    id: str
    title: str
    dir: str
    system: str = "COC7"
    players: str = ""
    era: str = ""            # 时代背景，例如「1925 年 · 美国马萨诸塞州」
    summary: str = ""
    premise: str = ""
    truth: str = ""
    scenes: list[Scene] = field(default_factory=list)
    handouts: list[Handout] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # -------------------------------------------------- 查询

    def scene(self, scene_id: str) -> Scene | None:
        for s in self.scenes:
            if s.id == scene_id:
                return s
        return None

    def first_scene_id(self) -> str:
        return self.scenes[0].id if self.scenes else ""

    def handout(self, hid: str) -> Handout | None:
        hid = (hid or "").strip()
        for h in self.handouts:
            if h.id == hid or h.title == hid or Path(h.path).name == hid:
                return h
        return None

    def outline(self) -> list[dict[str, Any]]:
        """给 KP 的场景目录（只给标题与序号，正文按需注入，省 token）。"""
        return [{"id": s.id, "title": s.title, "order": s.order} for s in self.scenes]

    def to_dict(self, with_body: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "dir": self.dir,
            "system": self.system,
            "players": self.players,
            "era": self.era,
            "summary": self.summary,
            "premise": self.premise,
            "has_truth": bool(self.truth.strip()),
            "truth_chars": len(self.truth),
            "scenes": [s.to_dict() | ({"body": s.body} if with_body else {})
                       for s in self.scenes],
            "handouts": [h.to_dict() | ({"body": h.body} if with_body else {})
                         for h in self.handouts],
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------- 解析工具

def _read(path: Path) -> str:
    """读一个模组文件。支持 .md / .txt / .yaml / .pdf。

    PDF 走 pypdf 抽文本——网上找来的模组经常是 PDF，不能只认纯文本。
    抽取失败时返回空串而不是抛异常，让上层去给用户提示。
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path)
    for enc in ("utf-8", "utf-8-sig", "gbk", "big5", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except Exception:
            continue
    return ""


def _read_pdf(path: Path, max_pages: int = 400) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        out: list[str] = []
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                out.append(f"\n…（全文共 {len(reader.pages)} 页，此处只取前 {max_pages} 页）")
                break
            try:
                out.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n\n".join(out)
    except Exception:
        return ""


MODULE_FILE_SUFFIXES = (".md", ".txt", ".pdf", ".yaml", ".yml")


def _title_of(text: str, fallback: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or fallback
    return fallback


def _strip_first_heading(text: str) -> str:
    lines = (text or "").splitlines()
    if lines and lines[0].strip().startswith("#"):
        return "\n".join(lines[1:]).strip()
    return (text or "").strip()


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", (name or "").strip())
    return s.strip("_") or "scene"


def _split_by_heading(text: str, prefix: str) -> list[Scene]:
    """把一份长文档按 `## 标题` 切成若干场景——让用户能直接丢一个 .md 进来。"""
    marks = list(re.finditer(r"^##\s+(.+?)\s*$", text or "", re.M))
    if not marks:
        return [Scene(id=f"{prefix}_01", title=_title_of(text, "开场"),
                      body=_strip_first_heading(text), order=1)]
    scenes: list[Scene] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        scenes.append(Scene(id=f"{prefix}_{i + 1:02d}", title=m.group(1).strip(),
                            body=body, order=i + 1))
    return scenes


# ---------------------------------------------------------------- 主流程

def load_module(module_id: str) -> Module | None:
    """加载一个模组。两种放法都支持：

        data\\modules\\我的模组\\         ← 一个文件夹（推荐：能分场景和 handout）
        data\\modules\\我的模组.pdf      ← 直接丢一个文件也行，会自动切成场景
    """
    root = cfgmod.modules_root() / module_id
    if root.is_file():
        return _load_loose_file(root)
    if not root.is_dir():
        return None
    m = Module(id=module_id, title=module_id, dir=str(root))

    # --- 元数据 ---
    info_path = root / "module_info.yaml"
    if not info_path.exists():
        info_path = root / "module.yaml"
    if info_path.exists():
        try:
            info = yaml.safe_load(_read(info_path)) or {}
            m.title = str(info.get("title") or info.get("name") or m.title)
            m.system = str(info.get("system") or info.get("rules") or m.system)
            pl = info.get("players") or info.get("recommended_players") or ""
            m.players = str(pl)
            # 时代背景：审卡就靠它判断"这个东西在这地方合不合理"
            m.era = str(info.get("era") or info.get("setting") or info.get("time") or "")
            m.summary = str(info.get("summary") or info.get("description") or "")
            m.premise = str(info.get("premise") or info.get("hook") or "")
            start = str(info.get("start_scene") or "")
            if start:
                m.premise += f"\n[指定开场场景: {start}]" if start else ""
        except Exception as e:
            m.warnings.append(f"module_info.yaml 解析失败：{e}")
    else:
        m.warnings.append("未找到 module_info.yaml，已按目录名与结构推断。")

    # --- 幕后真相（仅 KP 路径读取）---
    for fn in TRUTH_FILENAMES:
        p = root / fn
        if p.exists():
            m.truth = _read(p).strip()
            break
    if not m.truth:
        for p in root.glob("*.md"):
            if re.search(r"真相|secret|truth|kp|keeper", p.name, re.I):
                m.truth = _read(p).strip()
                break
    if not m.truth:
        m.warnings.append("未找到幕后真相文件（secret_truth.md），KP 将缺少暗线信息。")

    # --- 场景 ---
    sdir = root / "scenes"
    if sdir.is_dir():
        files = sorted([p for p in sdir.iterdir()
                        if p.is_file() and p.suffix.lower() in (".md", ".txt", ".yaml", ".yml")],
                       key=lambda p: p.name)
        for i, p in enumerate(files, start=1):
            body = _read(p)
            if p.suffix.lower() in (".yaml", ".yml"):
                try:
                    d = yaml.safe_load(body) or {}
                    title = str(d.get("title") or p.stem)
                    body = str(d.get("body") or d.get("text") or body)
                except Exception:
                    title = p.stem
            else:
                title = _title_of(body, p.stem)
                body = _strip_first_heading(body)
            m.scenes.append(Scene(id=p.stem, title=title, body=body,
                                  order=i, path=str(p)))
    if not m.scenes:
        # 退路：单文件模组
        cand = [p for p in root.glob("*.md")
                if not re.search(r"真相|secret|truth|kp|keeper", p.name, re.I)]
        cand.sort(key=lambda p: (0 if re.search(r"模组|剧本|module|scenario", p.name) else 1,
                                 p.name))
        if cand:
            text = _read(cand[0])
            m.scenes = _split_by_heading(text, "scene")
            m.warnings.append(f"未找到 scenes/ 目录，已把 {cand[0].name} 按标题切分为场景。")

    # --- Handout ---
    hdir = root / "handouts"
    if hdir.is_dir():
        for p in sorted(hdir.iterdir(), key=lambda p: p.name):
            if not p.is_file():
                continue
            body = _read(p)
            title = p.stem
            if p.suffix.lower() in (".md", ".txt"):
                title = _title_of(body, p.stem) if p.suffix.lower() == ".md" else p.stem
                if p.suffix.lower() == ".md":
                    body = _strip_first_heading(body)
            m.handouts.append(Handout(id=p.stem, title=title, body=body, path=str(p)))

    if not m.scenes:
        m.warnings.append("该模组没有任何场景，无法开跑。")

    return m


def _load_loose_file(path: Path) -> Module:
    """把单个模组文件（.md/.txt/.pdf）当成模组。

    网上找来的模组常常就是一整篇文，没有 scenes/ 目录。
    这里按 `##` 标题自动切场景；没有标题就按长度均分成几幕，
    保证 KP 至少有个能 advance_scene 的骨架。
    """
    text = _read(path).strip()
    m = Module(id=path.name, title=path.stem, dir=str(path))
    m.summary = f"（单文件模组：{path.name}）"
    if not text:
        m.warnings.append(
            f"读不出 {path.name} 的内容。"
            + ("如果是扫描版 PDF，文字是图片，需要先自己转成文本。"
               if path.suffix.lower() == ".pdf" else ""))
        return m

    scenes = _split_by_heading(text, "scene")
    if len(scenes) <= 1:
        scenes = _split_by_length(text, "scene", chunks=4)
        m.warnings.append("文件里没有 `##` 小标题，已按长度粗略切成几幕。")

    # 单文件模组没有单独的幕后真相文件，把整篇正文当作真相材料交给 KP
    m.truth = text
    m.scenes = scenes
    m.warnings.append(
        "单文件模组：整篇正文都会被当作守秘人资料，"
        "但开场简报仍会走剧透哨兵过滤。建议整理成文件夹形式效果更好"
        "（module_info.yaml + secret_truth.md + scenes/ + handouts/）。")
    return m


def _split_by_length(text: str, prefix: str, chunks: int = 4) -> list[Scene]:
    lines = (text or "").splitlines()
    if not lines:
        return []
    size = max(1, len(lines) // max(1, chunks))
    out: list[Scene] = []
    for i in range(chunks):
        part = "\n".join(lines[i * size:(i + 1) * size]).strip()
        if part:
            out.append(Scene(id=f"{prefix}_{i + 1:02d}",
                             title=f"第 {i + 1} 段", body=part, order=i + 1))
    return out


def scan_modules() -> list[dict[str, Any]]:
    """轻量扫描：只读元数据，不读全场正文——模组多了也不卡界面。

    既认文件夹，也认直接丢在 `data\\modules\\` 根目录下的单个文件。
    """
    out: list[dict[str, Any]] = []
    root = cfgmod.modules_root()
    entries: list[Path] = []
    for p in sorted(root.iterdir(), key=lambda x: x.name):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            entries.append(p)
        elif p.is_file() and p.suffix.lower() in MODULE_FILE_SUFFIXES:
            entries.append(p)

    for d in entries:
        try:
            m = load_module(d.name)
        except Exception as e:
            out.append({"id": d.name, "title": d.name, "error": str(e)})
            continue
        if not m:
            continue
        out.append({
            "id": m.id, "title": m.title, "system": m.system,
            "players": m.players, "era": m.era, "summary": m.summary,
            "scene_count": len(m.scenes), "handout_count": len(m.handouts),
            "has_truth": bool(m.truth.strip()),
            "loose_file": d.is_file(),
            "scenes": [{"id": s.id, "title": s.title} for s in m.scenes],
            "warnings": m.warnings,
        })
    return out


def ensure_demo_module() -> None:
    """保证至少有一个可跑的演示模组，让用户装完就能按开始。"""
    target = cfgmod.modules_root() / "demo_洋馆之夜"
    if (target / "module_info.yaml").exists():
        return
    # 演示模组由 tools/make_demo_module.py 生成；此处仅兜底创建空壳提示
    target.mkdir(parents=True, exist_ok=True)
    (target / "module_info.yaml").write_text(
        "title: 洋馆之夜（空壳）\n"
        "system: COC7\n"
        "players: \"1-4\"\n"
        "summary: 演示模组尚未生成，请运行 tools/make_demo_module.py\n",
        encoding="utf-8",
    )
