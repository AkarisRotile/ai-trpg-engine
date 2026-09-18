"""玩家层持久记忆 —— **跨周目**。

这是双声部设计里最容易漏掉、但最出「活人感」的一层：

    角色的记忆（MemoryCard）随模组结束而死。
    玩家的记忆（PlayerCard）跨模组存活。

老周这一局操控的是民俗学者艾德温·卡特；下一局他可能是个私家侦探。
但**老周还是老周**——他还是会先问「这房子里有几个人」，
他还是记得上一局阿凛把他的医生扔在门口自己跑了。

机制上有两半：
  1. **注入**：稳定部分（说话习惯、生涯、老梗）进 system；
     受触发部分（跟当前情境对得上的梗、在场的旧相识）进世界消息。
     这样既有味道，又不会每轮把整本"玩家回忆录"重发一遍。
  2. **复盘**：一局结束时跑一次 retro 通道，让 AI 以**玩家口吻**写回：
     这一局留下什么梗、对同桌的人什么看法、学到什么。
     没有这一半，跨周目记忆就永远长不出来。

存储位置与角色记忆刻意分开：
    data/memory/<session_id>/<seat_id>.yaml   ← 角色记忆，按局隔离
    data/memory/players/<player_id>.yaml      ← 玩家记忆，跨局累积
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from . import config as cfgmod

CARD_VERSION = 1

MEME_KINDS = {
    "catchphrase": "口头禅",
    "habit": "习惯",
    "running_joke": "老梗",
    "grudge": "旧账",
    "superstition": "迷信",
    "ritual": "固定仪式",
}

_FORBIDDEN_IN_NAME = re.compile(r'[\\/:*?"<>|]+')


def safe_id(name: str) -> str:
    s = _FORBIDDEN_IN_NAME.sub("_", (name or "").strip())
    s = s.strip(". ")
    return s[:48] or "player"


def players_dir() -> Path:
    p = cfgmod.data_root() / "players"
    p.mkdir(parents=True, exist_ok=True)
    return p


def player_dir(player_id: str) -> Path:
    """一个人的专属文件夹。

        data\\players\\<网名>\\
            player.yaml            跨周目玩家记忆（梗、习惯、关系、生涯）
            memory\\<会话id>.yaml    那一局里他操控的角色的记忆树
            sheets\\<角色名>.xlsx    按 COC7 空白卡填出来的角色卡
            sheets\\<角色名>.yaml    同一张卡的结构化版本
    """
    p = players_dir() / safe_id(player_id)
    p.mkdir(parents=True, exist_ok=True)
    (p / "memory").mkdir(exist_ok=True)
    (p / "sheets").mkdir(exist_ok=True)
    return p


def _legacy_card_path(player_id: str) -> Path:
    """旧版把所有玩家卡平铺在 memory\\players\\ 下，这里做一次迁移。"""
    return cfgmod.memory_root() / "players" / f"{safe_id(player_id)}.yaml"


# ══════════════════════════════════════════════ 数据结构

@dataclass
class Meme:
    text: str
    kind: str = "running_joke"
    origin: str = ""              # 出自哪一局
    tags: list[str] = field(default_factory=list)
    uses: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "kind": self.kind, "origin": self.origin,
                "tags": list(self.tags), "uses": self.uses}

    @staticmethod
    def from_dict(d: Any) -> "Meme | None":
        if isinstance(d, str):
            return Meme(text=d.strip())
        if not isinstance(d, dict):
            return None
        text = str(d.get("text") or "").strip()
        if not text:
            return None
        return Meme(text=text, kind=str(d.get("kind") or "running_joke"),
                    origin=str(d.get("origin") or ""),
                    tags=[str(t) for t in (d.get("tags") or [])],
                    uses=int(d.get("uses") or 0))


@dataclass
class PastCharacter:
    name: str = ""
    occupation: str = ""
    session_id: str = ""
    module: str = ""
    fate: str = ""                # 活下来 / 死了 / 疯了 / 中途退出
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "occupation": self.occupation,
                "session_id": self.session_id, "module": self.module,
                "fate": self.fate, "note": self.note}

    @staticmethod
    def from_dict(d: Any) -> "PastCharacter | None":
        if not isinstance(d, dict):
            return None
        return PastCharacter(
            name=str(d.get("name") or ""), occupation=str(d.get("occupation") or ""),
            session_id=str(d.get("session_id") or ""), module=str(d.get("module") or ""),
            fate=str(d.get("fate") or ""), note=str(d.get("note") or ""),
        )


# ══════════════════════════════════════════════ 玩家卡

class PlayerCard:
    def __init__(self, player_id: str, display_name: str = "") -> None:
        self.version = CARD_VERSION
        self.player_id = safe_id(player_id)
        self.display_name = display_name or player_id
        self.table_voice = ""
        self.playstyle = ""
        self.quirks: list[str] = []
        self.memes: list[Meme] = []
        self.relationships: dict[str, str] = {}
        self.reflections: list[str] = []
        self.characters: list[PastCharacter] = []
        self.stats: dict[str, Any] = {
            "sessions_played": 0, "deaths": 0, "insanities": 0,
            "first_played": "", "last_played": "",
        }
        self._path: Path | None = None

    # -------------------------------------------------- 读写

    @property
    def path(self) -> Path:
        return self._path or (player_dir(self.player_id) / "player.yaml")

    @staticmethod
    def load_or_create(player_id: str, display_name: str = "",
                       table_voice: str = "", playstyle: str = "") -> "PlayerCard":
        card = PlayerCard(player_id, display_name)
        p = player_dir(card.player_id) / "player.yaml"
        # 旧路径迁移：第一次读不到就去老地方找，找到就搬到新文件夹
        if not p.exists():
            legacy = _legacy_card_path(card.player_id)
            if legacy.exists():
                try:
                    p.write_bytes(legacy.read_bytes())
                    legacy.rename(legacy.with_suffix(".yaml.migrated"))
                except Exception:
                    pass
        loaded = False
        if p.exists():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                card._from_dict(data)
                loaded = True
            except Exception:
                loaded = False
        if not loaded:
            card.table_voice = table_voice
            card.playstyle = playstyle
            if not card.stats["first_played"]:
                card.stats["first_played"] = datetime.now().isoformat(timespec="seconds")
        else:
            # 座位上新填的说话习惯优先（用户可能在设置里改过）
            if table_voice and not card.table_voice:
                card.table_voice = table_voice
            if playstyle and not card.playstyle:
                card.playstyle = playstyle
        card._path = p
        return card

    def _from_dict(self, d: dict[str, Any]) -> None:
        self.version = int(d.get("version") or CARD_VERSION)
        self.player_id = safe_id(str(d.get("player_id") or self.player_id))
        self.display_name = str(d.get("display_name") or self.display_name)
        self.table_voice = str(d.get("table_voice") or "")
        self.playstyle = str(d.get("playstyle") or "")
        self.quirks = [str(x) for x in (d.get("quirks") or []) if str(x).strip()]
        self.memes = [m for m in (Meme.from_dict(x) for x in (d.get("memes") or [])) if m]
        self.relationships = {str(k): str(v) for k, v in (d.get("relationships") or {}).items()}
        self.reflections = [str(x) for x in (d.get("reflections") or []) if str(x).strip()]
        self.characters = [c for c in (PastCharacter.from_dict(x)
                                       for x in (d.get("characters") or [])) if c]
        self.stats = {**self.stats, **(d.get("stats") or {})}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "player_id": self.player_id,
            "display_name": self.display_name,
            "table_voice": self.table_voice,
            "playstyle": self.playstyle,
            "quirks": self.quirks,
            "memes": [m.to_dict() for m in self.memes],
            "relationships": self.relationships,
            "reflections": self.reflections,
            "characters": [c.to_dict() for c in self.characters],
            "stats": self.stats,
        }

    def save(self) -> Path:
        p = self.path
        p.write_text(
            yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8")
        return p

    def load_dict(self, d: dict[str, Any]) -> None:
        """用一份字典整体覆盖（界面手工编辑档案时用）。"""
        self._from_dict(d or {})

    # -------------------------------------------------- 注入

    def render_system_block(self, max_memes: int = 2) -> str:
        """稳定部分——进 system 提示，跟着前缀缓存一起吃便宜。"""
        lines: list[str] = []
        if self.table_voice:
            lines.append(f"你说话的样子：{self.table_voice}")
        if self.quirks:
            lines.append("你的老规矩：" + "；".join(self.quirks[:3]))

        n = int(self.stats.get("sessions_played") or 0)
        if n:
            last = self.characters[-1] if self.characters else None
            career = f"你已经跑过 {n} 局了"
            ask = int(self.stats.get("as_keeper") or 0)
            if ask:
                career += f"，其中 {ask} 局是你当守秘人"
            if last and last.name:
                career += f"；上一个角色是{last.occupation or ''}{last.name}"
                if last.fate:
                    career += f"（{last.fate}）"
            lines.append(career + "。")

        if self.memes:
            top = sorted(self.memes, key=lambda m: -(m.uses))[:max_memes]
            for m in top:
                lines.append(f"你常念叨的一句话：「{m.text}」")
        if self.reflections:
            lines.append("你自己总结过：" + self.reflections[-1])
        return "\n".join(lines)

    def recall(self, context: str, others: Iterable[str] = (),
               limit: int = 3) -> list[str]:
        """受触发部分——跟当前情境对得上的梗、在场的旧相识。"""
        out: list[str] = []
        ctx = context or ""
        names = [n for n in others if n]

        # 1) 在场的旧相识
        for name, view in self.relationships.items():
            if any(name and (name in n or n in name) for n in names) or name in ctx:
                out.append(f"你跟{name}有旧账：{view}")

        # 2) 标签/关键词能对上的梗
        scored: list[tuple[int, Meme]] = []
        for m in self.memes:
            score = sum(2 for t in m.tags if t and t in ctx)
            score += sum(1 for w in re.findall(r"[\u4e00-\u9fff]{2,4}", m.text)
                         if w in ctx)
            if score:
                scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        for _, m in scored[:limit]:
            out.append(f"这让你想起自己那句老话：「{m.text}」")
            m.uses += 1
        return out[:limit]

    def latest_character(self) -> PastCharacter | None:
        return self.characters[-1] if self.characters else None

    # -------------------------------------------------- 复盘吸收

    def begin_session(self, session_id: str, role: str = "PL") -> None:
        """记一局。角色可以轮换——今天当 KP，明天当玩家，人还是这个人。"""
        self.stats["sessions_played"] = int(self.stats.get("sessions_played") or 0) + 1
        key = "as_keeper" if (role or "").upper() == "KP" else "as_player"
        self.stats[key] = int(self.stats.get(key) or 0) + 1
        self.stats["last_played"] = datetime.now().isoformat(timespec="seconds")
        if not self.stats.get("first_played"):
            self.stats["first_played"] = self.stats["last_played"]

    def add_meme(self, text: str, origin: str = "",
                 kind: str = "running_joke") -> bool:
        """记一条梗。引擎自己数出来的，不走复盘那条路。

        已经在卡上的就把 uses 加一。render_system_block 按 uses 排序，
        所以被反复念叨的梗会自己浮到最上面。返回 True 表示新收了一条。
        """
        text = (text or "").strip()
        if not text or len(text) > 60:
            return False
        hit = next((m for m in self.memes if _norm(m.text) == _norm(text)), None)
        if hit is not None:
            hit.uses += 1
            return False
        self.memes.append(Meme(text=text, kind=kind, origin=origin))
        self.memes = self.memes[-40:]
        return True

    def absorb(self, retro: dict[str, Any], session_id: str,
               character: dict[str, Any] | None = None,
               module_title: str = "", fate: str = "") -> list[str]:
        """把复盘输出合并进玩家卡。返回人类可读的变更摘要。"""
        changes: list[str] = []

        if character:
            pc = PastCharacter(
                name=str(character.get("name") or ""),
                occupation=str(character.get("occupation") or ""),
                session_id=session_id, module=module_title, fate=fate,
                note=str(character.get("backstory") or "")[:80],
            )
            if pc.name and not any(c.session_id == session_id for c in self.characters):
                self.characters.append(pc)
                changes.append(f"记下这个角色：{pc.name}（{pc.occupation}）")
            if fate and "死" in fate:
                self.stats["deaths"] = int(self.stats.get("deaths") or 0) + 1
            if fate and "疯" in fate:
                self.stats["insanities"] = int(self.stats.get("insanities") or 0) + 1

        for item in _as_list(retro.get("memes")):
            text, tags = _split_tags(item)
            if not text or len(text) > 60:
                continue
            if any(_norm(m.text) == _norm(text) for m in self.memes):
                continue
            self.memes.append(Meme(text=text, kind="running_joke",
                                   origin=session_id, tags=tags))
            changes.append(f"新增老梗：{text}")
        self.memes = self.memes[-40:]

        for item in _as_list(retro.get("quirks")):
            text = str(item).strip()
            if text and text not in self.quirks and len(text) <= 40:
                self.quirks.append(text)
                changes.append(f"新习惯：{text}")
        self.quirks = self.quirks[-10:]

        for item in _as_list(retro.get("relationships")):
            name, _, view = str(item).partition("=")
            if not view:
                name, _, view = str(item).partition("：")
            name, view = name.strip(), view.strip()
            if name and view:
                self.relationships[name] = view
                changes.append(f"对{name}的看法已更新")

        for item in _as_list(retro.get("reflections")):
            text = str(item).strip()
            if text and len(text) <= 120 and text not in self.reflections:
                self.reflections.append(text)
                changes.append(f"复盘：{text}")
        self.reflections = self.reflections[-12:]

        voice = str(retro.get("table_voice") or "").strip()
        if voice and len(voice) <= 80 and voice != self.table_voice:
            self.table_voice = voice
            changes.append("说话风格已更新")

        return changes


# ══════════════════════════════════════════════ 工具

def _norm(t: str) -> str:
    return re.sub(r"[\s，。、,.;；:：!！?？\"'“”‘’()（）]+", "", t or "")


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _split_tags(item: Any) -> tuple[str, list[str]]:
    text = str(item).strip()
    tags: list[str] = []
    if "|" in text:
        text, _, tail = text.partition("|")
        tags = [t.strip() for t in re.split(r"[,，、/]", tail) if t.strip()]
    elif "（" in text and text.endswith("）"):
        head, _, tail = text[:-1].partition("（")
        text, tags = head, [t.strip() for t in re.split(r"[,，、/]", tail) if t.strip()]
    return text.strip(), tags[:4]


def parse_retro_block(text: str) -> dict[str, Any]:
    """解析复盘输出里的 <player_update> 块。"""
    m = re.search(r"<player_update>(.*?)(?:</player_update>|$)", text or "", re.S | re.I)
    body = m.group(1) if m else (text or "")
    try:
        data = yaml.safe_load(body)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # YAML 失败时退化为行解析：键名: 值
    out: dict[str, list[str]] = {}
    for line in body.splitlines():
        line = line.strip().lstrip("-*•").strip()
        mm = re.match(r"^([A-Za-z_]+)\s*[:：]\s*(.+)$", line)
        if mm:
            out.setdefault(mm.group(1).lower(), []).append(mm.group(2).strip())
    return out


def list_player_cards() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # 新旧两种位置都扫，保证迁移期也能列全
    candidates: list[Path] = list(players_dir().glob("*/player.yaml"))
    candidates += list((cfgmod.memory_root() / "players").glob("*.yaml")) \
        if (cfgmod.memory_root() / "players").is_dir() else []
    for p in candidates:
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        pid = d.get("player_id", p.parent.name if p.parent.name != "players" else p.stem)
        folder = player_dir(str(pid))
        out.append({
            "player_id": pid,
            "display_name": d.get("display_name", pid),
            "sessions_played": (d.get("stats") or {}).get("sessions_played", 0),
            "memes": len(d.get("memes") or []),
            "quirks": len(d.get("quirks") or []),
            "characters": len(d.get("characters") or []),
            "last_played": (d.get("stats") or {}).get("last_played", ""),
            "folder": str(folder),
            "sheets": sorted(x.name for x in (folder / "sheets").glob("*")
                             if x.is_file()),
            "memories": sorted(x.name for x in (folder / "memory").glob("*.yaml")),
        })
    out.sort(key=lambda x: x.get("last_played", ""), reverse=True)
    return out


def load_card(player_id: str) -> PlayerCard | None:
    if not (player_dir(player_id) / "player.yaml").exists() \
            and not _legacy_card_path(player_id).exists():
        return None
    return PlayerCard.load_or_create(player_id)
