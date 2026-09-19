"""模组库：把 `data/modules/` 下的剧本文件夹读取成结构化对象。

信息隔离设计：`secret_truth` 字段只应被 KP 的提示词编译器读取，
PL 的编译路径根本不会触碰它——不是"提示模型别说"，而是**取不到**。
"""

from __future__ import annotations

import re
import threading
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
    start_time: str = ""     # 开场时刻，例如「1925-10-03 20:00」
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
            "start_time": self.start_time,
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
            # 开场时刻：引擎拿它起点钟，之后每轮给 AI 一张"哪天是哪天"的对照表
            m.start_time = str(info.get("start_time") or info.get("start_at") or "")
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
        # 退路一：单文件模组
        cand = [p for p in root.glob("*.md")
                if not re.search(r"真相|secret|truth|kp|keeper", p.name, re.I)]
        cand.sort(key=lambda p: (0 if re.search(r"模组|剧本|module|scenario", p.name) else 1,
                                 p.name))
        if cand:
            text = _read(cand[0])
            m.scenes = _split_by_heading(text, "scene")
            m.warnings.append(f"未找到 scenes/ 目录，已把 {cand[0].name} 按标题切分为场景。")

    # 退路二：多格式扫描。
    # 现实里从网上下来的模组经常长这样——
    #   某某模组/ 本体.doc  第一章/地图.png  第二章/怪物资料.docx  时间线.xls
    # 一个 .md 都没有。结构化布局在这种文件夹面前等于瞎了，
    # 所以这里回落到"把整个文件夹按格式读一遍"。
    scan: dict[str, Any] | None = None
    if not m.scenes:
        from . import docread
        scan = docread.scan_folder(root)
        if scan["text"].strip():
            m.scenes = _split_scanned(scan["text"])
            m.warnings.append(
                f"这不是标准模组结构：扫到 {scan['files']} 个文件，"
                f"按内容切成 {len(m.scenes)} 幕。"
                f"（建议整理成 scenes/ + secret_truth.md，守秘人会跑得更准。）")
        else:
            m.warnings.append("这个文件夹里没有能读出的文字。")
        for w in scan.get("warnings", []):
            m.warnings.append(w)
        if not m.truth and scan["text"].strip():
            m.truth = scan["text"]
            m.warnings.append("没有单独的幕后真相文件，已把整包内容当作守秘人资料。")
        if scan.get("assets"):
            m.handouts = [Handout(id=Path(a).stem, title=Path(a).name, body="",
                                  path=str(root / a)) for a in scan["assets"]]
            m.warnings.append(f"文件夹里有 {len(scan['assets'])} 个图片素材，"
                              f"已登记为 handout（内容要你自己看）。")

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


def _split_scanned(text: str, min_chars: int = 1400,
                   max_scenes: int = 48) -> list[Scene]:
    """把"扫出来的"整包文本切成幕。

    难点在于**切多碎**。启发式扫出来的文本里，短行到处都是
    （"提取码：7naa"、"5。大失败固定为96"），按每个 `##` 切会切出三百多幕，
    KP 根本没法用。所以策略是：**先切，再合并**。

      1. 找候选切点：`## ` 小标题（docx 标题样式 / .doc 里像标题的短行）
         找不到再按 `===== 文件名 =====` 分（一个文件一幕也很合理）
      2. 合并：相邻段不足 min_chars 就并进上一幕——
         这一步会把"提取码：7naa"这种伪标题自然吸收掉
      3. 还是超过 max_scenes 就按长度继续并
    """
    marks = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.M))
    if len(marks) < 2:
        marks = list(re.finditer(r"^=====\s*(.+?)\s*=====\s*$", text, re.M))

    if len(marks) >= 2:
        chunks: list[tuple[str, str]] = []
        for i, mk in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[mk.end():end].strip()
            name = mk.group(1).strip()
            if Path(name).suffix:
                name = Path(name).stem
            chunks.append((name[:36], body))
        # 开头如果还有内容（第一个标题之前的），单独放一幕
        head = text[:marks[0].start()].strip()
        if len(head) >= 200:
            chunks.insert(0, ("开场材料", head))
    else:
        chunks = [("", text.strip())]

    # ---- 合并：不足 min_chars 的并进上一幕 ----
    merged: list[tuple[str, str]] = []
    for name, body in chunks:
        if not body:
            continue
        if merged and len(merged[-1][1]) < min_chars:
            prev_name, prev_body = merged[-1]
            merged[-1] = (prev_name, prev_body + "\n\n" + (f"## {name}\n" if name else "") + body)
        else:
            merged.append((name, body))

    # ---- 还是太多就按长度再并 ----
    while len(merged) > max_scenes:
        size = max(2, len(merged) // max_scenes + 1)
        merged = [(merged[i][0],
                   "\n\n".join(b for _, b in merged[i:i + size]))
                  for i in range(0, len(merged), size)]

    out: list[Scene] = []
    for i, (name, body) in enumerate(merged):
        if not body.strip():
            continue
        title = name or (f"第 {i + 1} 段")
        out.append(Scene(id=f"scene_{i + 1:02d}", title=title,
                         body=body, order=i + 1))
    return out or _split_by_length(text, "scene", chunks=4)


# 扫描结果缓存。键是"每个模组目录里最新的改动时间 + 文件数"。
# 为什么要缓存：bootstrap() 每开一次弹窗就会调到这里，而 load_module()
# 要把整个模组的正文读一遍。一个 23 MB 的模组就要 7 秒，
# 用户点「模组」之后界面八秒没反应，看着就是打不开。
# 模组多了会更糟，所以不能再每次重扫。
_SCAN_CACHE: dict[str, Any] = {"key": None, "mods": []}
_SCAN_LOCK = threading.Lock()


def _scan_key(root: Path) -> tuple:
    """目录指纹。只 stat，不读内容，很快。"""
    out = []
    try:
        children = sorted(root.iterdir(), key=lambda x: x.name)
    except OSError:
        return ()
    for p in children:
        if p.name.startswith("."):
            continue
        try:
            if p.is_file():
                out.append((p.name, round(p.stat().st_mtime, 1), 1))
                continue
            if not p.is_dir():
                continue
            newest, count = 0.0, 0
            for f in p.rglob("*"):
                if not f.is_file():
                    continue
                count += 1
                try:
                    newest = max(newest, f.stat().st_mtime)
                except OSError:
                    pass
            out.append((p.name, round(newest, 1), count))
        except OSError:
            continue
    return tuple(out)


def scan_modules(force: bool = False) -> list[dict[str, Any]]:
    """扫描模组。只读元数据，不读全场正文。

    既认文件夹，也认直接丢在 `data\\modules\\` 根目录下的单个文件。

    默认走缓存：目录没变就直接返回上次的结果，界面立刻能开。
    界面上那个「刷新」按钮传 force=True，强制重扫。
    """
    root = cfgmod.modules_root()
    key = _scan_key(root)
    if not force and _SCAN_CACHE["key"] == key:
        return list(_SCAN_CACHE["mods"])

    with _SCAN_LOCK:
        # 双检：等锁这段时间里，别人（比如启动预热那个线程）可能已经扫完了
        if not force and _SCAN_CACHE["key"] == key:
            return list(_SCAN_CACHE["mods"])
        return _scan_modules_locked(root, key, force)


def _scan_modules_locked(root: Path, key: tuple, force: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
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
    _SCAN_CACHE["key"] = key
    _SCAN_CACHE["mods"] = out
    return out


def warm_scan() -> None:
    """后台预热一次扫描。

    第一次扫描要把每个模组的正文都读一遍，一个大模组就要七八秒。
    用户点「模组」时界面八秒没反应，看着就是打不开。
    所以在程序启动时先偷偷跑掉，等真点的时候就是瞬间。
    """
    try:
        scan_modules()
    except Exception:  # noqa: BLE001
        pass


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
