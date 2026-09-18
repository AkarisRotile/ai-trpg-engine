"""模组研读档案。

守秘人读完一个模组之后产出的那份"功课"（理解 / 大纲 / 扩展 / 彩蛋），
按**模组**存下来，而不是按会话。

为什么要这样：真实的主持人读完一遍模组，那个理解就长在他脑子里了，
下次带同一桌人跑同一个本，他不用重读。这里照这个来——
研读一次，之后所有用这个模组的团都直接复用，除非你让它重读。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from . import config as cfgmod


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip())[:80] or "module"


def studies_dir() -> Path:
    p = cfgmod.data_root() / "studies"
    p.mkdir(parents=True, exist_ok=True)
    return p


def study_path(module_id: str) -> Path:
    return studies_dir() / f"{_safe(module_id)}.yaml"


def load_study(module_id: str) -> dict[str, Any] | None:
    p = study_path(module_id)
    if not p.exists():
        return None
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    return d if isinstance(d, dict) and d.get("understanding") else None


def save_study(module_id: str, study: dict[str, Any], *,
               keeper: str = "", model: str = "",
               title: str = "") -> Path:
    data = {
        "module_id": module_id,
        "title": title or module_id,
        "keeper": keeper,
        "model": model,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "spine": study.get("spine", ""),
        "spine_ids": study.get("spine_ids", []),
        "understanding": study.get("understanding", ""),
        "expansion": study.get("expansion", ""),
        "eggs": study.get("eggs", ""),
    }
    p = study_path(module_id)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
                 encoding="utf-8")
    return p


def delete_study(module_id: str) -> bool:
    p = study_path(module_id)
    if p.exists():
        p.unlink()
        return True
    return False


def list_studies() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted(studies_dir().glob("*.yaml")):
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        out.append({
            "module_id": d.get("module_id", p.stem),
            "title": d.get("title", p.stem),
            "keeper": d.get("keeper", ""),
            "updated_at": d.get("updated_at", ""),
            "has_expansion": bool(d.get("expansion")),
            "has_eggs": bool(d.get("eggs")),
            "chars": len((d.get("understanding") or "") + (d.get("expansion") or "")
                         + (d.get("eggs") or "")),
        })
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return out


@dataclass
class ChatLog:
    """研读室里的对话记录（只存在内存里，随会话走）。"""
    turns: list[dict[str, str]] = field(default_factory=list)

    def add(self, who: str, text: str) -> None:
        self.turns.append({"who": who, "text": text})

    def recent(self, n: int = 10) -> list[dict[str, str]]:
        return self.turns[-n:]
