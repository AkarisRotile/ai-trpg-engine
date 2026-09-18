"""三层记忆系统。

| 层  | 名称            | 变化频率 | 注入策略      |
|-----|-----------------|----------|---------------|
| L0  | character_sheet | 几乎不变 | 全量注入      |
| L1  | situation       | 每场景   | 全量注入      |
| L2  | chronicle       | 追加式   | 检索式 top-K  |

**关键设计**：磁盘上是结构化 YAML（人类可读可改、UI 可直接渲染），
但注入 prompt 时会被渲染成**角色的回忆**，而不是让模型"读自己的笔记"。
直接丢 YAML 会让模型进入"我在读一份档案"的元姿态。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from . import config as cfgmod

MEMORY_VERSION = "1.0"

# ---------------------------------------------------------------- 认知树

NODE_KINDS = {"fact": "事实", "hypothesis": "猜想", "question": "疑问", "relation": "印象"}
NODE_STATES = {"confirmed": "已确认", "suspected": "在猜",
               "open": "还没弄明白", "discarded": "已排除"}
STATE_MARK = {"confirmed": "✓", "suspected": "?", "open": "…", "discarded": "×"}


@dataclass
class TreeNode:
    """认知树上的一个节点。

    平铺的线索流水记的是"我见过什么"，树记的是"我现在怎么想"——
    后者才是调查员脑子里真正在运转的东西：哪些确认了、哪些在猜、哪些还没头绪。

    `label` 是**名词**（"叹息之像""里斯""门户"），`brief` 是**一句话**。
    索引里只给这两样 + 节点之间的关系；想看全部细节得显式召回。
    这是照真实记忆的样子做的：先想起名字，再顺着名字把东西捞出来。
    """
    id: str
    text: str
    kind: str = "fact"          # fact | hypothesis | question | relation
    state: str = "confirmed"    # confirmed | suspected | open | discarded
    parent: str = ""            # 父节点 id，空 = 挂在根上
    label: str = ""             # 名词（索引里显示）
    brief: str = ""             # 一句话
    note: str = ""              # 依据 / 备注（只在召回时给）
    source: str = ""            # 谁告诉我的（同伴、KP、自己看见）
    turn: int = 0
    scene: str = ""
    updated: int = 0
    # 桌上的钟走到哪一刻知道的这件事（"10月3日 21:15"）。
    # 有它才说得出「这是三天前听来的」——没有它，模型会把什么都算成"刚才"。
    when: str = ""

    def title(self) -> str:
        return self.label or self.text[:12]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "kind": self.kind,
                "state": self.state, "parent": self.parent, "label": self.label,
                "brief": self.brief, "note": self.note, "source": self.source,
                "turn": self.turn, "scene": self.scene, "updated": self.updated,
                "when": self.when}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "TreeNode | None":
        text = str(d.get("text") or "").strip()
        if not text:
            return None
        return TreeNode(
            id=str(d.get("id") or f"n{abs(hash(text)) % 100000}"),
            text=text,
            kind=str(d.get("kind") or "fact"),
            state=str(d.get("state") or "confirmed"),
            parent=str(d.get("parent") or ""),
            label=str(d.get("label") or ""),
            brief=str(d.get("brief") or ""),
            note=str(d.get("note") or ""),
            source=str(d.get("source") or ""),
            turn=int(d.get("turn") or 0),
            scene=str(d.get("scene") or ""),
            updated=int(d.get("updated") or 0),
            when=str(d.get("when") or ""),
        )


@dataclass
class Link:
    """两个名词之间的**关联**。索引里显示的正是这东西。

    树只表达"属于/包含"，但真实记忆里更常见的是横向关联：
    「里斯」和「叹息之像」不是父子，是"他把它锁了进去"。这种边要单独存。
    """
    a: str
    b: str
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"a": self.a, "b": self.b, "label": self.label}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Link | None":
        a, b = str(d.get("a") or "").strip(), str(d.get("b") or "").strip()
        if not a or not b:
            return None
        return Link(a=a, b=b, label=str(d.get("label") or ""))


def _match_score(ref: str, text: str) -> float:
    """模糊匹配一个引用指向哪个节点。"""
    a, b = _norm(ref), _norm(text)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    ta, tb = _tokens(ref), _tokens(text)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


KIND_CN = {
    "clue": "线索",
    "event": "经历",
    "handout": "到手的文件",
    "injury": "伤势",
    "insight": "想法",
    "relation": "印象",
    "note": "备忘",
}

# 记忆增量 <mem> 通道支持的指令
DELTA_PREFIXES = {
    "add_clue", "clue", "add_event", "event", "add_handout", "handout",
    "add_insight", "insight", "impression", "relation", "note", "memo",
    "inventory_add", "get", "inventory_remove", "drop", "use",
    "location", "objective", "condition_add", "condition_remove",
    "hp", "san", "mp", "luck",
    # —— 认知树 ——
    "add_fact", "fact", "add_hypothesis", "hypothesis", "hunch",
    "add_question", "question", "todo",
    "confirm", "discard", "doubt", "reopen", "relate", "link",
}

TREE_ADD = {
    "add_fact": ("fact", "confirmed"),
    "fact": ("fact", "confirmed"),
    "add_hypothesis": ("hypothesis", "suspected"),
    "hypothesis": ("hypothesis", "suspected"),
    "hunch": ("hypothesis", "suspected"),
    "add_question": ("question", "open"),
    "question": ("question", "open"),
    "todo": ("question", "open"),
}

TREE_STATE = {
    "confirm": "confirmed",
    "discard": "discarded",
    "doubt": "suspected",
    "reopen": "open",
}

_LABEL_RE = re.compile(r"^\s*[【]([^】]{1,20})[】]\s*(.*)$")
_PARENT_RE = re.compile(r"^\s*\[([^\]]{2,40})\]\s*(.+)$")
_NOTE_RE = re.compile(r"\s*[|｜]\s*(?:依据|备注|因为|来源)\s*[:：]\s*(.+)$")
_RELATE_RE = re.compile(r"^\s*[【\[]?([^】\]=\s]{1,20})[】\]]?\s*[=＝]\s*"
                        r"[【\[]?([^】\]|｜\s]{1,20})[】\]]?\s*(?:[|｜]\s*(.+))?$")

_KINDS = {
    "add_clue": "clue", "clue": "clue",
    "add_event": "event", "event": "event",
    "add_handout": "handout", "handout": "handout",
    "add_insight": "insight", "insight": "insight",
    "note": "note", "memo": "note",
}

_ATTR_DELTA = {"hp": "HP", "san": "SAN", "mp": "MP", "luck": "LUCK"}


# ---------------------------------------------------------------- 条目

@dataclass
class ChronicleEntry:
    turn: int
    scene: str
    kind: str
    text: str
    tags: list[str] = field(default_factory=list)
    source: str = ""          # 引擎写入时标记来源（如 handout 文件名）
    when: str = ""            # 这件事发生在桌上的哪一刻（"10月3日 21:15"）

    def to_dict(self) -> dict[str, Any]:
        return {"turn": self.turn, "scene": self.scene, "kind": self.kind,
                "text": self.text, "tags": list(self.tags), "source": self.source,
                "when": self.when}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ChronicleEntry":
        return ChronicleEntry(
            turn=int(d.get("turn", 0) or 0),
            scene=str(d.get("scene", "") or ""),
            kind=str(d.get("kind", "event") or "event"),
            text=str(d.get("text", "") or ""),
            tags=list(d.get("tags") or []),
            source=str(d.get("source", "") or ""),
            when=str(d.get("when", "") or ""),
        )


def _norm(text: str) -> str:
    return re.sub(r"[\s，。、,.;；:：!！?？\"'“”‘’()（）\[\]]+", "", text or "")


def _tokens(text: str) -> set[str]:
    """极简中文分词：按 2-gram 切，并保留长度≥2 的连续英文数字串。"""
    t = _norm(text)
    grams = {t[i:i + 2] for i in range(max(0, len(t) - 1))}
    grams |= set(re.findall(r"[a-zA-Z0-9]{2,}", text or ""))
    return grams


# ---------------------------------------------------------------- 记忆卡

class MemoryCard:
    """单个 AI 座位的独立记忆。PL 用 L0/L1/L2 全套；KP 只用 L1/L2 做场面备忘。"""

    def __init__(self, seat_id: str, kind: str = "PL", agent_label: str = "",
                 model_backend: str = "") -> None:
        self.version = MEMORY_VERSION
        self.seat_id = seat_id
        self.kind = kind
        self.agent_label = agent_label or seat_id
        self.model_backend = model_backend
        self.player_profile: dict[str, Any] = {}
        self.character_sheet: dict[str, Any] = {}
        self.situation: dict[str, Any] = {
            "current_scene": "", "current_location": "", "current_objective": "",
            "inventory": [], "conditions": [], "party_impressions": {},
        }
        self.chronicle: list[ChronicleEntry] = []
        # ★ 认知树：调查员当前的思路——确认了什么、在猜什么、还没弄明白什么。
        #   和 chronicle 的分工：chronicle 是**流水**（我见过什么，只增不改），
        #   树是**心智模型**（我现在怎么想，会挂靠、会升级、会被推翻）。
        self.tree: list[TreeNode] = []
        # 名词之间的横向关联（树只表达"属于/包含"）
        self.links: list[Link] = []
        self.session_id: str = ""
        self.player_id: str = ""      # 归属的人（决定这份记忆存进哪个文件夹）
        self.path: Path | None = None
        # 桌上的钟现在走到哪一刻——每轮由引擎刷新，新记忆自动带上时间。
        # 有它，多年以后（或者三天以后）才说得清"这事是什么时候知道的"。
        self.now: str = ""
        self._seq = 0

    # -------------------------------------------------- 认知树

    def _new_id(self) -> str:
        self._seq += 1
        return f"n{len(self.tree) + 1}_{self._seq}"

    def find_node(self, ref: str, threshold: float = 0.45) -> TreeNode | None:
        """按文字片段或名词找节点（模型只会给一个粗略的引用，不会给 id）。"""
        ref = (ref or "").strip()
        if not ref:
            return None
        best: tuple[float, TreeNode | None] = (0.0, None)
        for n in self.tree:
            s = max(_match_score(ref, n.text), _match_score(ref, n.note) * 0.6)
            if n.label:
                s = max(s, _match_score(ref, n.label) * 0.95)
            if s > best[0]:
                best = (s, n)
        return best[1] if best[0] >= threshold else None

    @staticmethod
    def _derive_label(text: str) -> str:
        head = re.split(r"[，。、；：,.;:（(]", text or "", 1)[0].strip()
        return (head or (text or ""))[:6]

    def tree_add(self, text: str, kind: str = "fact", state: str = "confirmed",
                 parent_ref: str = "", label: str = "", brief: str = "",
                 note: str = "", turn: int = 0, scene: str = "",
                 source: str = "", when: str = "") -> tuple[TreeNode, bool]:
        """加一个节点。文字重复就当作"又提到了一次"，只更新轮次，不重复长枝。"""
        text = (text or "").strip().strip('"').strip("'")
        if not text:
            return TreeNode(id="", text=""), False
        # 判重要**严格**：只认归一化后完全相同的文本。
        # 这里刻意不用 find_node 的模糊匹配——那套是按词重叠率算的，
        # 会把「第0号线索…」和「第1号线索…」这种长得很像但确实不同的记忆错误合并掉。
        # 模糊匹配只该用在"模型给了一个粗略的引用，帮我找找是哪个节点"上。
        key = _norm(text)
        existing = next((n for n in self.tree if _norm(n.text) == key), None)
        if existing:
            existing.updated = turn or existing.updated
            if note and not existing.note:
                existing.note = note
            if state == "confirmed" and existing.state == "suspected":
                existing.state = "confirmed"        # 被证实了
            return existing, False

        parent_id = ""
        if parent_ref:
            p = self.find_node(parent_ref, threshold=0.4)
            if p:
                parent_id = p.id
        lbl = (label or "").strip() or self._derive_label(text)
        brf = (brief or "").strip()
        if not brf:
            # 一句话只从**原文**里取，且不要重复名词本身。
            # 刻意不拿 note 来凑——依据属于"细节"，索引里就不该有它。
            rest = text[len(lbl):].lstrip("，。、：: 　") if text.startswith(lbl) else text
            brf = rest[:22]
        node = TreeNode(id=self._new_id(), text=text, kind=kind, state=state,
                        parent=parent_id, label=lbl, brief=brf,
                        note=note, source=source,
                        turn=turn, scene=scene or self.situation.get("current_scene", ""),
                        updated=turn, when=when)
        self.tree.append(node)
        # 同时进一条流水：树是"我现在怎么想"，流水是"我见过什么"。
        # 流水只增不改，供检索用（拿到 handout 之后再回头找旧线索时用得上），
        # 提示词里渲染的是索引/树，所以不会重复占 token。
        self.add("clue" if kind == "fact" else "event", text,
                 turn=turn, scene=scene, tags=[kind], when=when)
        return node, True

    def tree_set_state(self, ref: str, state: str, turn: int = 0) -> TreeNode | None:
        node = self.find_node(ref)
        if node:
            node.state = state
            node.updated = turn
        return node

    def _children(self, parent_id: str) -> list[TreeNode]:
        return [n for n in self.tree if n.parent == parent_id]

    def tree_dicts(self) -> list[dict[str, Any]]:
        """给界面用的树（已经按父子关系排好序）。"""
        out: list[dict[str, Any]] = []
        order = {n.id: i for i, n in enumerate(self.tree)}

        def walk(node: TreeNode, depth: int) -> None:
            d = node.to_dict()
            d["depth"] = depth
            out.append(d)
            for c in sorted(self._children(node.id), key=lambda x: order.get(x.id, 0)):
                walk(c, depth + 1)

        for root in sorted([n for n in self.tree if not n.parent or
                            not any(x.id == n.parent for x in self.tree)],
                           key=lambda x: order.get(x.id, 0)):
            walk(root, 0)
        return out

    def render_tree(self, scene_id: str = "", query: str = "", limit: int = 16) -> str:
        """把认知树渲染成给模型看的缩进树。

        成本控制：节点太多时只保留跟当前场景/叙述最相关的那部分，
        但**根节点永远保留**——否则树就散了，看不出脉络。
        """
        if not self.tree:
            return ""
        live = [n for n in self.tree if n.state != "discarded"]
        discarded = [n for n in self.tree if n.state == "discarded"]
        roots = [n for n in live if not n.parent] or live[:1]

        if len(live) > limit:
            q = _tokens(query or "")
            def score(n: TreeNode) -> float:
                s = 0.0
                if scene_id and n.scene == scene_id:
                    s += 1.0
                if q:
                    s += 2.0 * (len(q & _tokens(n.text)) / max(1, len(q)))
                s += 0.5 * (n.updated / max(1, max(x.updated for x in live)))
                if n.kind == "question":
                    s += 0.4
                return s
            keep = set()
            for r in roots:
                keep.add(r.id)
            for n in sorted(live, key=score, reverse=True):
                if len(keep) >= limit:
                    break
                keep.add(n.id)
                p = n
                while p.parent:                      # 父链也带上，别让树断掉
                    keep.add(p.parent)
                    p = next((x for x in self.tree if x.id == p.parent), p)
                    if p.parent == "":
                        keep.add(p.id)
                        break
            live = [n for n in live if n.id in keep]
            roots = [n for n in live if not n.parent] or live[:1]

        lines: list[str] = []

        def walk(node: TreeNode, depth: int) -> None:
            mark = STATE_MARK.get(node.state, "·")
            label = NODE_STATES.get(node.state, node.state)
            line = "  " * depth + f"{mark} {node.text}"
            if node.note:
                line += f"（{node.note}）"
            if node.source:
                line += f" ⟨{node.source}⟩"
            if node.state == "suspected" and not node.note:
                line += "（你自己猜的）"
            lines.append(line)
            for c in self._children(node.id):
                if c in live:
                    walk(c, depth + 1)

        for r in roots:
            walk(r, 0)
        if discarded:
            lines.append("× 已经排除的：" + "；".join(n.text for n in discarded[:4]))

        head = ("【你脑子里的脉络】\n"
                "✓=已经确认  ?=你自己在猜  …=还没弄明白")
        return head + "\n" + "\n".join(lines)

    # -------------------------------------------------- 索引与召回（联想式记忆）

    def _rank(self, scene_id: str, query: str, limit: int) -> list[TreeNode]:
        """按相关性挑节点。根节点永远保留，否则索引会散掉。"""
        live = [n for n in self.tree if n.state != "discarded"]
        if len(live) <= limit:
            return live
        q = _tokens(query or "")
        top = max((n.updated for n in live), default=1) or 1

        def score(n: TreeNode) -> float:
            s = 0.0
            if scene_id and n.scene == scene_id:
                s += 1.0
            if q:
                s += 2.0 * (len(q & _tokens(n.text)) / max(1, len(q)))
            s += 0.5 * (n.updated / top)
            if n.kind == "question":
                s += 0.4
            return s

        keep: set[str] = {n.id for n in live if not n.parent}
        for n in sorted(live, key=score, reverse=True):
            if len(keep) >= limit:
                break
            keep.add(n.id)
            p = n
            while p.parent:                     # 父链也带上，别让脉络断掉
                keep.add(p.parent)
                nxt = next((x for x in self.tree if x.id == p.parent), None)
                if nxt is None:
                    break
                p = nxt
        return [n for n in live if n.id in keep]

    def _relations(self, node: TreeNode, live: list[TreeNode]) -> list[str]:
        by_id = {n.id: n for n in self.tree}
        rels: list[str] = []
        if node.parent and node.parent in by_id:
            rels.append(f"属于「{by_id[node.parent].title()}」")
        kids = [c for c in self._children(node.id) if c in live]
        if kids:
            rels.append("包含「" + "」「".join(c.title() for c in kids[:4]) + "」")
        for lk in self.links:
            if node.title() in (lk.a, lk.b):
                other = lk.b if lk.a == node.title() else lk.a
                rels.append(f"关联「{other}」" + (f"（{lk.label}）" if lk.label else ""))
        return rels[:4]

    def render_index(self, scene_id: str = "", query: str = "", limit: int = 20) -> str:
        """只给**名词 + 一句话 + 名词之间的关系**。

        这是照真实记忆的样子做的：你想起一件事的时候，先冒出来的是几个名字
        和它们之间模模糊糊的联系，细节要顺着名字再去捞。
        好处有两个：一是省 token（索引比全树小得多），
        二是逼着模型真的"去回忆"，而不是把一整棵树摊在眼前挨个读。
        """
        if not self.tree:
            return ""
        live = self._rank(scene_id, query, limit)
        if not live:
            return ""
        lines: list[str] = []
        for n in live:
            mark = STATE_MARK.get(n.state, "·")
            one = n.brief or n.text
            if one.startswith(n.title()):
                one = one[len(n.title()):].lstrip("，。、：: 　")
            one = one[:20]
            # ★ 索引只给名词和一句话。**依据、来源、完整原文都不给**——
            #   那正是"要用 <recall> 去捞"的东西。
            #   时间戳是例外：没有它，模型会把三天前听来的事说成"刚才"。
            stamp = f"（{n.when}）" if n.when else ""
            lines.append(f"{mark} {n.title()}{stamp}" + (f"，{one}" if one else ""))
            rels = self._relations(n, live)
            if rels:
                lines.append("    " + "；".join(rels))
        return ("【你想起的东西（索引）】\n" + "\n".join(lines)
                + "\n（要想起某个东西的全部细节，写 <recall>名字</recall>。）")

    def recall(self, terms: list[str]) -> list[dict[str, Any]]:
        """顺着名词把细节捞出来。这就是「联想」的那一步。"""
        out: list[dict[str, Any]] = []
        by_id = {n.id: n for n in self.tree}
        for term in (terms or [])[:4]:
            term = (term or "").strip()
            if not term:
                continue
            node = self.find_node(term, threshold=0.5)
            if node is None:
                out.append({"term": term, "ok": False})
                continue
            related = [e.text for e in self.chronicle
                       if e.text != node.text and (_tokens(e.text) & _tokens(node.text))]
            out.append({
                "term": term, "ok": True, "label": node.title(),
                "text": node.text, "brief": node.brief, "note": node.note,
                "source": node.source, "state": node.state, "when": node.when,
                "parent": by_id[node.parent].title() if node.parent in by_id else "",
                "children": [c.text for c in self._children(node.id)],
                "links": self._relations(node, list(self.tree)),
                "related": related[:4],
            })
        return out

    def render_recall(self, details: list[dict[str, Any]]) -> str:
        parts: list[str] = ["【你想起来的事】"]
        for d in details:
            if not d.get("ok"):
                parts.append(f"「{d['term']}」，你使劲想，但脑子里一片空白。")
                continue
            blk = [f"▸ {d['label']}（{NODE_STATES.get(d['state'], d['state'])}）"]
            if d.get("when"):
                blk.append(f"  （你是在 {d['when']} 知道这件事的）")
            blk.append(f"  {d['text']}")
            if d.get("note"):
                blk.append(f"  你当时的依据：{d['note']}")
            if d.get("source"):
                blk.append(f"  这件事的来源：{d['source']}")
            if d.get("children"):
                blk.append("  相关的还有：" + "；".join(d["children"][:4]))
            if d.get("links"):
                blk.append("  " + "；".join(d["links"][:4]))
            if d.get("related"):
                blk.append("  当时顺带提到过：" + "；".join(d["related"][:3]))
            parts.append("\n".join(blk))
        return "\n".join(parts)

    # -------------------------------------------------- 构造

    @staticmethod
    def for_pl(seat: dict[str, Any], session_id: str) -> "MemoryCard":
        char = seat.get("character") or {}
        card = MemoryCard(seat.get("seat_id", "pl"), "PL",
                          seat.get("display_name", ""), seat.get("model", ""))
        card.session_id = session_id
        card.player_id = str((seat.get("profile") or {}).get("player_id") or "")
        card.player_profile = dict(seat.get("profile") or {})
        card.character_sheet = dict(char)
        card.situation["inventory"] = list(char.get("inventory") or [])
        card.situation["conditions"] = list(char.get("conditions") or [])
        return card

    @staticmethod
    def for_kp(seat: dict[str, Any], session_id: str) -> "MemoryCard":
        card = MemoryCard(seat.get("seat_id", "kp"), "KP",
                          seat.get("display_name", "守秘人"), seat.get("model", ""))
        card.session_id = session_id
        return card

    # -------------------------------------------------- 检索

    def retrieve(self, scene_id: str = "", query: str = "", topk: int = 12) -> list[ChronicleEntry]:
        """L2 检索：场景匹配 ×2 + 关键词重叠 + 时间衰减。"""
        if not self.chronicle:
            return []
        q = _tokens(query) if query else set()
        max_turn = max((e.turn for e in self.chronicle), default=1) or 1
        scored: list[tuple[float, ChronicleEntry]] = []
        for e in self.chronicle:
            score = 0.0
            if scene_id and e.scene == scene_id:
                score += 2.0
            if q:
                et = _tokens(e.text)
                if et:
                    score += 1.6 * (len(q & et) / max(1, min(len(q), len(et))))
            score += 0.6 * (e.turn / max_turn)          # 越近越重要
            if e.kind in ("clue", "handout"):
                score += 0.5                              # 线索优先
            scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:max(1, topk)]]

    # -------------------------------------------------- 渲染注入

    def render_for_prompt(self, scene_id: str = "", query: str = "",
                          topk: int = 12, mode: str = "index") -> str:
        """把记忆渲染成**角色视角的当前认知**，而不是一份待阅读的档案。"""
        blocks: list[str] = []

        if self.kind == "PL":
            c = self.character_sheet
            a = c.get("attributes") or {}
            head = f"{c.get('name', '？')}，{c.get('age', '？')} 岁的{c.get('occupation', '？')}"
            traits = self.player_profile.get("personality_traits") or []
            if traits:
                head += "\n性格：" + "；".join(str(t) for t in traits)
            own_way = self.player_profile.get("playstyle")
            if own_way:
                head += f"\n行事的习惯：{own_way}"
            blocks.append("【你是谁】\n" + head)

            status = (
                f"生命 {a.get('HP', '?')}/{a.get('MAXHP', '?')} · "
                f"理智 {a.get('SAN', '?')}/{a.get('MAXSAN', 99)} · "
                f"魔法点 {a.get('MP', '?')} · 幸运 {a.get('LUCK', '?')} · "
                f"伤害加值 {a.get('DB', '0')}"
            )
            conds = self.situation.get("conditions") or []
            if conds:
                status += "\n身体状况：" + "；".join(str(x) for x in conds)
            blocks.append("【你此刻的状态】\n" + status)

            skills = c.get("skills") or []
            if skills:
                sk = " · ".join(f"{s.get('name')} {s.get('value')}" for s in skills
                                if isinstance(s, dict))
                blocks.append("【你的本事（技能名 数值）】\n" + sk)

            inv = self.situation.get("inventory") or []
            blocks.append("【你身上带着的东西】\n" + ("、".join(str(x) for x in inv) or "（两手空空）"))
        else:
            blocks.append("【你的身份】\n你是这局游戏的主持人（守秘人），"
                          "世界、NPC 与后果由你裁定。")

        sit = []
        if self.situation.get("current_location"):
            sit.append("你在：" + str(self.situation["current_location"]))
        if self.situation.get("current_objective"):
            sit.append("你眼下要做的是：" + str(self.situation["current_objective"]))
        if sit:
            blocks.append("【你的处境】\n" + "\n".join(sit))

        entries = self.retrieve(scene_id=scene_id, query=query, topk=topk)

        # ★ 记忆分两种给法：
        #   index（默认）—— 只给名词 + 一句话 + 名词间的关系，细节要用 <recall> 去捞。
        #                    索引刻意比全树**更小**：这是它存在的意义。
        #   full         —— 直接把整棵树摊开（省一次调用，但每轮都贵）
        if self.tree:
            if (mode or "index").lower() == "full":
                blocks.append(self.render_tree(scene_id=scene_id, query=query,
                                               limit=max(10, topk + 6)))
            else:
                blocks.append(self.render_index(scene_id=scene_id, query=query,
                                                limit=max(8, topk)))

        # 文件类记忆单独列——它们是有实体的东西，不属于"想法"
        docs = [e for e in entries if e.kind == "handout"]
        if docs:
            blocks.append("【你手上有的文件】\n"
                          + "\n".join(f"- {e.text[:320]}" for e in docs))
        if not self.tree and entries:
            # 树还没长出来时退回平铺列表，别让角色一开始就空着脑子
            blocks.append("【你已经知道的事】\n"
                          + "\n".join(f"- {e.text}" for e in entries))

        imp = self.situation.get("party_impressions") or {}
        if imp and self.kind == "PL":
            blocks.append("【你对同行者的印象】\n" +
                          "\n".join(f"{k}：{v}" for k, v in imp.items()))

        return "\n\n".join(blocks)

    # -------------------------------------------------- 写入

    def add(self, kind: str, text: str, turn: int = 0, scene: str = "",
            tags: Iterable[str] | None = None, source: str = "",
            when: str = "") -> bool:
        """追加一条 L2。完全重复（归一化后相同）的内容会被丢弃，防止复读式膨胀。"""
        text = (text or "").strip().strip('"').strip("'")
        if not text:
            return False
        key = _norm(text)
        for e in self.chronicle:
            if _norm(e.text) == key:
                return False
        self.chronicle.append(ChronicleEntry(
            turn=turn, scene=scene or self.situation.get("current_scene", ""),
            kind=kind or "event", text=text, tags=list(tags or []), source=source,
            when=when or self.now,
        ))
        return True

    def apply_delta(self, delta: dict[str, list[str]], turn: int = 0,
                    scene: str = "") -> list[str]:
        """应用 <mem> 通道解析出的增量，返回人类可读的变更摘要。"""
        changes: list[str] = []
        for key, values in (delta or {}).items():
            k = key.strip().lower()
            val = " ".join(values).strip()
            if not val:
                continue
            if k in _KINDS:
                for line in values:
                    if self.add(_KINDS[k], line, turn=turn, scene=scene):
                        changes.append(f"记住：{line.strip()}")
            elif k in ("impression", "relation"):
                name, _, imp = val.partition("=")
                if not imp:
                    name, _, imp = val.partition("：")
                if name.strip() and imp.strip():
                    self.situation.setdefault("party_impressions", {})[name.strip()] = imp.strip()
                    changes.append(f"印象更新：{name.strip()}")
            elif k in ("inventory_add", "get"):
                for item in values:
                    item = item.strip()
                    if item and item not in self.situation["inventory"]:
                        self.situation["inventory"].append(item)
                        changes.append(f"获得：{item}")
            elif k in ("inventory_remove", "drop", "use"):
                for item in values:
                    item = item.strip()
                    if item in self.situation["inventory"]:
                        self.situation["inventory"].remove(item)
                        changes.append(f"用掉/失去：{item}")
            elif k == "location":
                self.situation["current_location"] = val
                changes.append(f"位置：{val}")
            elif k == "objective":
                self.situation["current_objective"] = val
                changes.append(f"目标：{val}")
            elif k == "condition_add":
                for x in values:
                    if x.strip() and x.strip() not in self.situation["conditions"]:
                        self.situation["conditions"].append(x.strip())
                        changes.append(f"状态：{x.strip()}")
            elif k == "condition_remove":
                for x in values:
                    if x.strip() in self.situation["conditions"]:
                        self.situation["conditions"].remove(x.strip())
            elif k in _ATTR_DELTA:
                attr = _ATTR_DELTA[k]
                delta_val = _parse_int(val)
                if delta_val:
                    cur = int(self.character_sheet.get("attributes", {}).get(attr, 0) or 0)
                    self.character_sheet.setdefault("attributes", {})[attr] = cur + delta_val
                    changes.append(f"{attr} {delta_val:+d}")
            elif k in TREE_ADD:
                kind, state = TREE_ADD[k]
                # 【名词】 一句话 —— 【】里是索引里显示的名字
                lm = _LABEL_RE.match(val)
                label = ""
                if lm:
                    label, val = lm.group(1).strip(), (lm.group(2) or "").strip()
                m = _PARENT_RE.match(val)
                parent_ref, text = (m.group(1), m.group(2)) if m else ("", val)
                nm = _NOTE_RE.search(text)
                note = nm.group(1).strip() if nm else ""
                if nm:
                    text = text[:nm.start()].strip()
                node, created = self.tree_add(text, kind=kind, state=state,
                                              parent_ref=parent_ref, label=label,
                                              note=note, turn=turn, scene=scene,
                                              when=self.now)
                if created:
                    desc = f"{NODE_STATES.get(state, state)}｜{node.title()}：{text}"
                    if parent_ref and node.parent:
                        parent = next((x for x in self.tree if x.id == node.parent), None)
                        if parent:
                            desc += f"（挂在「{parent.title()}」下）"
                    changes.append(desc)
            elif k in ("relate", "link"):
                rm = _RELATE_RE.match(val)
                if not rm:
                    continue
                a, b = rm.group(1).strip(), rm.group(2).strip()
                lab = (rm.group(3) or "").strip()
                if a and b and not any(l.a == a and l.b == b for l in self.links):
                    self.links.append(Link(a=a, b=b, label=lab))
                    changes.append(f"关联：{a} ↔ {b}" + (f"（{lab}）" if lab else ""))
            elif k in TREE_STATE:
                new_state = TREE_STATE[k]
                node = self.tree_set_state(val, new_state, turn=turn)
                if node:
                    changes.append(f"「{node.text}」→ {NODE_STATES.get(new_state, new_state)}")
            elif k == "note":
                self.add("note", val, turn=turn, scene=scene)
        return changes

    def apply_engine_stat(self, attr: str, delta: int) -> None:
        attrs = self.character_sheet.setdefault("attributes", {})
        cur = int(attrs.get(attr, 0) or 0)
        mx = attrs.get("MAX" + attr.upper())
        new = cur + delta
        if isinstance(mx, int):
            new = min(new, mx)
        attrs[attr] = max(0, new)

    # -------------------------------------------------- 序列化

    def to_yaml_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "agent_id": f"{self.kind}_{self.seat_id}",
            "display_name": self.agent_label,
            "model_backend": self.model_backend,
            "session_id": self.session_id,
            "player_id": self.player_id,
            "player_profile": self.player_profile,
            "character_sheet": self.character_sheet,
            "situation": self.situation,
            "chronicle": [e.to_dict() for e in self.chronicle],
            "tree": [n.to_dict() for n in self.tree],
            "links": [l.to_dict() for l in self.links],
        }

    @staticmethod
    def from_yaml_dict(d: dict[str, Any]) -> "MemoryCard":
        card = MemoryCard(
            seat_id=str(d.get("agent_id", "pl")).split("_")[-1],
            kind=str(d.get("agent_id", "PL")).split("_")[0] or "PL",
            agent_label=d.get("display_name", ""),
            model_backend=d.get("model_backend", ""),
        )
        card.version = d.get("version", MEMORY_VERSION)
        card.session_id = d.get("session_id", "")
        card.player_id = d.get("player_id", "")
        card.player_profile = dict(d.get("player_profile") or {})
        card.character_sheet = dict(d.get("character_sheet") or {})
        card.situation = dict(d.get("situation") or {})
        card.situation.setdefault("inventory", [])
        card.situation.setdefault("conditions", [])
        card.situation.setdefault("party_impressions", {})
        card.chronicle = [ChronicleEntry.from_dict(x) for x in (d.get("chronicle") or [])]
        card.tree = [n for n in (TreeNode.from_dict(x) for x in (d.get("tree") or [])) if n]
        card.links = [l for l in (Link.from_dict(x) for x in (d.get("links") or [])) if l]
        return card

    def save(self, session_id: str | None = None) -> Path:
        """角色记忆按**人**归档：就写在他自己的文件夹里。

            data\\players\\<网名>\\memory\\<会话id>.yaml

        找不到 player_id 时退回按会话归档的老路径（KP 座位、临时探针等）。
        """
        sid = session_id or self.session_id or "default"
        if self.player_id:
            from . import player_memory
            d = player_memory.player_dir(self.player_id) / "memory"
        else:
            d = cfgmod.memory_root() / sid
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.yaml" if self.player_id else d / f"{self.seat_id}.yaml"
        p.write_text(
            yaml.safe_dump(self.to_yaml_dict(), allow_unicode=True,
                           sort_keys=False, width=100),
            encoding="utf-8",
        )
        self.path = p
        return p

    def load(self) -> bool:
        candidates: list[Path] = []
        if self.player_id:
            from . import player_memory
            candidates.append(player_memory.player_dir(self.player_id)
                              / "memory" / f"{self.session_id}.yaml")
        candidates.append(cfgmod.memory_root() / (self.session_id or "default")
                          / f"{self.seat_id}.yaml")
        p = next((x for x in candidates if x.exists()), None)
        if p is None:
            return False
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            return False
        other = MemoryCard.from_yaml_dict(data)
        self.__dict__.update(other.__dict__)
        self.seat_id = data.get("agent_id", f"{self.kind}_{self.seat_id}").split("_", 1)[-1]
        self.kind = (data.get("agent_id") or "PL_x").split("_", 1)[0]
        self.path = p
        return True


def _parse_int(text: str) -> int:
    m = re.search(r"[+-]?\d+", text or "")
    return int(m.group()) if m else 0


# ---------------------------------------------------------------- <mem> 解析

def parse_mem_block(block: str) -> dict[str, list[str]]:
    """解析 <mem> 通道。

    支持两种写法：
        add_clue: 二楼有拖拽重物的声响
        add_clue: 二楼有拖拽重物的声响 / 壁炉里有羊皮纸
    以及每条独占一行、冒号后可带多条用 / 或 ；分隔。
    """
    out: dict[str, list[str]] = {}
    for raw in (block or "").splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_]+)\s*[:：=]\s*(.+)$", line)
        if not m:
            continue
        key = m.group(1).strip().lower()
        if key not in DELTA_PREFIXES:
            continue
        value = m.group(2).strip()
        parts = [p.strip() for p in re.split(r"[／/]|；|;", value) if p.strip()]
        out.setdefault(key, []).extend(parts or [value])
    return out
