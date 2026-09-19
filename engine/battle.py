"""战斗与追逐的格子。

**为什么要有这个**：位置这种东西模型描述不清楚。
你让它在叙述里说「他退到左后方三步」，五轮之后没人知道谁在哪、
谁够得着谁。真人桌上是用棋子摆出来的，看一眼就知道。

所以位置由守秘人用 `<state>` 摆坐标，引擎存着，界面画出来。
引擎不管规则（该不该打、打不打得中），只管**谁在哪**。

    <state>
    battle on           开打，格子打开
    grid 12x12          改格子大小
    npc 石泽 55 那个东西  加一个 NPC，55 是他的 DEX（用来排先攻）
    place 余快 3 5       把某个调查员摆到 (3,5)
    place 石泽 7 4
    drop 某个名字        棋子离场
    battle off          打完了
    </state>

追逐是另一套，线性轨道：

    <state>
    chase on            开始追
    chase 余快 0         谁在第几格，0 是头
    chase 石泽 3
    chase off
    </state>

先攻顺序由 DEX 排，引擎自己算，不用守秘人写。
"""

from __future__ import annotations

from typing import Any

# 格子默认多大。12×12 够一场室内遭遇战，再大画出来就看不清了。
DEFAULT_W = 12
DEFAULT_H = 12
DEFAULT_TRACK = 10
# token 允许的坐标上限，防止守秘人写个 999 把界面撑爆
MAX_SIDE = 40


def blank() -> dict[str, Any]:
    return {"active": False, "w": DEFAULT_W, "h": DEFAULT_H, "tokens": [],
            "chase": {"active": False, "len": DEFAULT_TRACK, "positions": {}}}


def _ensure(session: Any) -> dict[str, Any]:
    b = getattr(session, "battle", None)
    if not isinstance(b, dict) or not b:
        b = blank()
        session.battle = b
    b.setdefault("active", False)
    b.setdefault("w", DEFAULT_W)
    b.setdefault("h", DEFAULT_H)
    b.setdefault("tokens", [])
    ch = b.setdefault("chase", {})
    ch.setdefault("active", False)
    ch.setdefault("len", DEFAULT_TRACK)
    ch.setdefault("positions", {})
    return b


def _int(v: Any, lo: int, hi: int) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, n))


def _find(tokens: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    name = (name or "").strip()
    if not name:
        return None
    return next((t for t in tokens if t.get("name") == name), None)


# ---------------------------------------------------------------- 指令

def apply(session: Any, kind: str, target: str, payload: str,
          *, pc_dex: dict[str, int] | None = None) -> str:
    """执行一条格子指令。返回一句人话给守秘人看（成功就返回空串）。"""
    b = _ensure(session)
    kind = (kind or "").strip().lower()
    target = (target or "").strip()
    payload = (payload or "").strip()

    if kind in ("battle", "战斗"):
        word = (target or payload).lower()
        if word in ("off", "no", "end", "结束", "停", "0"):
            b["active"] = False
            return ""
        b["active"] = True
        if not b["tokens"] and pc_dex:
            # 开打的时候调查员还没摆，先给个默认站位，免得格子上一片空
            for i, name in enumerate(pc_dex):
                b["tokens"].append({
                    "id": f"pc_{i}", "name": name, "kind": "pc",
                    "x": 1 + i, "y": 1, "note": "", "dex": int(pc_dex[name]),
                })
        return ""

    if kind == "grid":
        spec = payload or target
        parts = spec.lower().replace("×", "x").split("x")
        if len(parts) == 2:
            b["w"] = _int(parts[0], 4, MAX_SIDE)
            b["h"] = _int(parts[1], 4, MAX_SIDE)
            b["tokens"] = [t for t in b["tokens"]
                           if t["x"] < b["w"] and t["y"] < b["h"]]
        return ""

    if kind == "npc":
        if not target:
            return "npc 指令要写名字，例如：npc 石泽 55"
        tok = _find(b["tokens"], target)
        dex = _int((payload.split() or ["50"])[0], 1, 99)
        note = payload[len(payload.split()[0]):].strip() if payload.split() else ""
        if tok is None:
            tok = {"id": f"npc_{len(b['tokens'])}", "name": target, "kind": "npc",
                   "x": 0, "y": 0, "note": note, "dex": dex}
            b["tokens"].append(tok)
        else:
            tok["dex"] = dex
            if note:
                tok["note"] = note
        return ""

    if kind in ("place", "move", "摆"):
        if not target:
            return "place 指令要写名字，例如：place 余快 3 5"
        nums = [p for p in payload.replace(",", " ").split() if p]
        if len(nums) < 2:
            return f"place {target} 后面要跟两个数字（列 行），例如：place {target} 3 5"
        x = _int(nums[0], 1, b["w"])
        y = _int(nums[1], 1, b["h"])
        tok = _find(b["tokens"], target)
        if tok is None:
            dex = (pc_dex or {}).get(target)
            tok = {"id": f"t_{len(b['tokens'])}", "name": target,
                   "kind": "pc" if dex is not None else "npc",
                   "x": x, "y": y, "note": "", "dex": int(dex or 50)}
            b["tokens"].append(tok)
        else:
            tok["x"], tok["y"] = x, y
        return ""

    if kind in ("drop", "remove", "撤"):
        tok = _find(b["tokens"], target or payload)
        if tok is not None:
            b["tokens"].remove(tok)
        return ""

    if kind in ("chase", "追逐"):
        ch = b["chase"]
        word = (target or "").lower()
        if word in ("off", "no", "end", "结束", "停", "0"):
            ch["active"] = False
            return ""
        if word in ("on", "yes", "start", "开始", "1"):
            ch["active"] = True
            b["active"] = True
            return ""
        # chase 名字 格数
        if not target:
            return "chase 要写名字和格数，例如：chase 余快 0"
        n = _int(payload, 0, ch["len"])
        ch["positions"][target] = n
        ch["active"] = True
        b["active"] = True
        return ""

    return ""


# ---------------------------------------------------------------- 读出来

def initiative(session: Any) -> list[dict[str, Any]]:
    """先攻顺序。按 DEX 从高到低，DEX 相同的按名字排，免得每次刷新都在跳。"""
    b = _ensure(session)
    toks = [t for t in b["tokens"] if t.get("kind") in ("pc", "npc")]
    toks.sort(key=lambda t: (-int(t.get("dex") or 0), str(t.get("name") or "")))
    return [{"name": t.get("name"), "kind": t.get("kind"),
             "dex": int(t.get("dex") or 0), "note": t.get("note") or ""}
            for t in toks]


def snapshot(session: Any) -> dict[str, Any]:
    """给界面（和插件）看的一份快照。"""
    b = _ensure(session)
    return {
        "active": bool(b["active"]),
        "w": int(b["w"]), "h": int(b["h"]),
        "tokens": [dict(t) for t in b["tokens"]],
        "initiative": initiative(session),
        "chase": {
            "active": bool(b["chase"]["active"]),
            "len": int(b["chase"]["len"]),
            "positions": dict(b["chase"]["positions"]),
        },
    }


def render_for_prompt(session: Any) -> str:
    """给守秘人看的一小块。它得知道现在谁在哪，不然叙述会飘。"""
    b = _ensure(session)
    if not b["active"]:
        return ""
    parts: list[str] = []
    if b["tokens"]:
        lines = []
        for t in b["tokens"]:
            if int(t.get("x") or 0) <= 0 or int(t.get("y") or 0) <= 0:
                lines.append(f"  {t['name']}（{t['kind']}）还没摆位置")
            else:
                lines.append(f"  {t['name']}（{t['kind']}）在 第{t['x']}列 第{t['y']}行")
        parts.append(f"[现在的站位]（格子 {b['w']}×{b['h']}）\n" + "\n".join(lines))
    ini = initiative(session)
    if ini:
        parts.append("[先攻顺序]\n" + " → ".join(
            f"{i['name']}({i['dex']})" for i in ini))
    ch = b["chase"]
    if ch["active"] and ch["positions"]:
        pairs = sorted(ch["positions"].items(), key=lambda kv: kv[1])
        parts.append("[追逐位置]（0 是最前面）\n" + "  ".join(
            f"{n}:{v}" for n, v in pairs))
    return "\n".join(parts)
