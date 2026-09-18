"""会话状态与存档。

一个会话 = 一次跑团。所有可变状态都落在这个文件管理的目录里：

  data/runtime/sessions/<session_id>/
      session.json      —— 座位、场景、回合数、车卡结果（可续跑）
      transcript.jsonl  —— 全量流水（逐条事件，归档与导出用）
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from . import clock as clock_mod
from . import config as cfgmod


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


class Session:
    """会话状态容器。引擎层对象，不含任何 UI 依赖。"""

    def __init__(self, session_id: str = "", *, module_id: str = "",
                 premise: str = "") -> None:
        self.session_id = session_id or new_session_id()
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.mode = "game"          # game | study（研读室：只有你和守秘人）
        self.module_id = module_id
        self.premise = premise
        self.scene_id = ""
        self.round = 0
        self.phase = "setup"          # setup | chargen | anchored | running | paused | ended | study
        self.study_report = ""        # 研读室里的"通读报告"
        self.study_talk: list[dict[str, str]] = []   # 研读室的对话记录
        # 桌上的钟。引擎掌管时间，每轮给 AI 一张"哪天是哪天"的对照表——
        # 模型自己算日子一定会把什么事都说成"昨天"。
        self.clock: Any = None        # engine.clock.GameClock
        self.seats: dict[str, dict[str, Any]] = {}
        self.pending_private: dict[str, list[str]] = {}   # 私聊/线索，下轮送达
        self.pending_dice: dict[str, list[str]] = {}      # 骰子结果，下轮送达
        # 谁私下收到了什么东西（只记标签不记内容）——
        # 用来给其他人生成「你知道 KP 递了张纸给他，但不知道写了什么」这种知识边界
        self.seat_private_log: dict[str, list[str]] = {}
        self.director_notes: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.scene_log: list[str] = []
        self.ended_reason = ""
        # 守秘人在车卡前发给全桌的赛前简报（不含剧透），PL 车卡时会看到
        self.chargen_briefing: str = ""
        # 守秘人开局前做的功课：理解 / 大纲 / 扩展 / 彩蛋
        self.module_study: dict[str, Any] = {}
        # 结局之后才解禁模组原文给 PL 看
        self.module_revealed: bool = False

    # -------------------------------------------------- 路径

    @property
    def dir(self) -> Path:
        d = cfgmod.sessions_root() / self.session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -------------------------------------------------- 座位状态

    def seat_state(self, seat_id: str) -> dict[str, Any]:
        return self.seats.setdefault(seat_id, {
            "attrs": {}, "character": None, "chargen_warnings": [],
            "chargen_raw": "", "chargen_ooc": "", "ready": False,
        })

    # -------------------------------------------------- 事件

    def log(self, evt: dict[str, Any]) -> dict[str, Any]:
        """记录一条事件，同时追加进内存列表与磁盘流水。"""
        evt.setdefault("ts", time.time())
        evt.setdefault("round", self.round)
        self.events.append(evt)
        try:
            with (self.dir / "transcript.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(evt, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return evt

    # -------------------------------------------------- 存档

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "mode": self.mode,
            "clock": self.clock.to_dict() if self.clock is not None else None,
            "study_report": self.study_report,
            "study_talk": self.study_talk[-40:],
            "module_id": self.module_id,
            "premise": self.premise,
            "scene_id": self.scene_id,
            "round": self.round,
            "phase": self.phase,
            "seats": self.seats,
            "pending_private": self.pending_private,
            "pending_dice": self.pending_dice,
            "seat_private_log": self.seat_private_log,
            "director_notes": self.director_notes[-50:],
            "scene_log": self.scene_log,
            "ended_reason": self.ended_reason,
            "chargen_briefing": self.chargen_briefing,
            "module_study": self.module_study,
            "module_revealed": self.module_revealed,
        }

    def save(self) -> Path:
        p = self.dir / "session.json"
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return p

    @staticmethod
    def load(session_id: str) -> "Session | None":
        p = cfgmod.sessions_root() / session_id / "session.json"
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        s = Session(d.get("session_id") or session_id)
        s.created_at = d.get("created_at", s.created_at)
        s.mode = d.get("mode", "game")
        s.clock = clock_mod.GameClock.from_dict(d.get("clock"))
        s.study_report = d.get("study_report", "")
        s.study_talk = d.get("study_talk") or []
        s.module_id = d.get("module_id", "")
        s.premise = d.get("premise", "")
        s.scene_id = d.get("scene_id", "")
        s.round = int(d.get("round", 0))
        s.phase = d.get("phase", "paused")
        s.seats = d.get("seats") or {}
        s.pending_private = d.get("pending_private") or {}
        s.pending_dice = d.get("pending_dice") or {}
        s.seat_private_log = d.get("seat_private_log") or {}
        s.director_notes = d.get("director_notes") or []
        s.scene_log = d.get("scene_log") or []
        s.ended_reason = d.get("ended_reason", "")
        s.chargen_briefing = d.get("chargen_briefing", "")
        s.module_study = d.get("module_study") or {}
        s.module_revealed = bool(d.get("module_revealed"))
        s.events = Session._read_transcript(session_id)
        return s

    @staticmethod
    def _read_transcript(session_id: str, limit: int = 4000) -> list[dict[str, Any]]:
        p = cfgmod.sessions_root() / session_id / "transcript.jsonl"
        if not p.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return []
        return out[-limit:]

    # -------------------------------------------------- 枚举

    @staticmethod
    def list_sessions() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        root = cfgmod.sessions_root()
        for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            p = d / "session.json"
            if not p.exists():
                continue
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (j.get("mode") or "game") != "game":
                continue        # 研读室不是"一局团"，不该出现在存档列表里
            out.append({
                "session_id": j.get("session_id", d.name),
                "created_at": j.get("created_at", ""),
                "module_id": j.get("module_id", ""),
                "round": j.get("round", 0),
                "phase": j.get("phase", ""),
                "mode": j.get("mode", "game"),
                "players": sum(1 for k, v in (j.get("seats") or {}).items()
                               if v.get("character")),
            })
        return out

    # -------------------------------------------------- 导出

    TYPE_LABEL = {
        "narr": "守秘人", "act": "行动", "ooc": "桌边", "think": "内心",
        "secret": "幕后", "dice": "骰子", "system": "系统",
        "chargen": "车卡", "scene": "场景", "director": "导演指令",
    }

    def export_markdown(self) -> Path:
        lines = [
            f"# 跑团记录 · {self.session_id}",
            "",
            f"- 开始时间：{self.created_at}",
            f"- 模组：{self.module_id or '（自由跑团）'}",
            f"- 回合数：{self.round}",
            "",
            "---",
            "",
        ]
        for e in self.events:
            t = e.get("type", "")
            name = e.get("name", "")
            text = (e.get("text") or "").strip()
            if not text:
                continue
            label = self.TYPE_LABEL.get(t, t)
            if t in ("think", "secret"):
                lines.append(f"> **{name}·{label}**：{text}")
            elif t == "dice":
                lines.append(f"🎲 **{label}**：{text}")
            elif t == "system":
                lines.append(f"⚙️ *{text}*")
            elif t == "chargen":
                lines.append(f"📋 **{name} 的角色卡**\n\n```yaml\n{text}\n```")
            else:
                lines.append(f"**{name}**：{text}")
            lines.append("")
        out = cfgmod.exports_root() / f"{self.session_id}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        return out
