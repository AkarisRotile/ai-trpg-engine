"""把本轮新增/修改的提示词原文导出来，交给用户审。

为什么单独导一份：用户要核"有没有抄袭嫌疑"。
这份文件里**只有我自己写的句子**，不含任何第三方预设的原文——
`tools/leak_sentinel.py` 会一并校验这一点（4256 段指纹取交集）。

用法：.venv\\Scripts\\python.exe tools\\dump_prompts.py [输出路径]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import prompts as P          # noqa: E402

SEAT = {
    "seat_id": "pl_1", "kind": "PL", "display_name": "摇人",
    "profile": {"player_name": "摇人", "table_voice": "爱吐槽、说话短。",
                "playstyle": "慎重解密流", "rigor_note": "记得七七八八"},
    "character": {"name": "顾云章", "occupation": "记者", "age": 34},
}
KP_SEAT = {"seat_id": "kp", "kind": "KP", "display_name": "汽水",
           "profile": {"player_name": "汽水"}}
ATTRS = {"STR": 30, "CON": 30, "DEX": 70, "APP": 80, "POW": 55,
         "SIZ": 65, "INT": 90, "EDU": 45, "LUCK": 25}


def section(title: str) -> str:
    return "\n\n" + "=" * 72 + f"\n## {title}\n" + "=" * 72 + "\n"


def main() -> int:
    out: list[str] = [
        "# 本轮新增/修改的提示词（我自己写的，供审阅）\n",
        "本文件由 `tools/dump_prompts.py` 生成，内容 = `engine/prompts.py` 里"
        "本轮动过的部分。\n不含任何第三方预设原文（可用 `tools/leak_sentinel.py` 复核）。\n",
    ]

    out.append(section("1. TABLE_REGISTER —— 桌边语域（新增，车卡讨论与插话轮共用）"))
    out.append(P.TABLE_REGISTER)

    out.append(section("2. DEEPSEEK_INNER_VOICE —— 思考方式标记（新增）"))
    out.append(P.DEEPSEEK_INNER_VOICE.strip())

    out.append(section("3. 车卡铁律（改：不再要求 AI 自己凑预算）"))
    sysmsg = P.build_chargen_system(SEAT, "（这里本来是规则分节）", "（这里本来是守秘人简报）")
    i = sysmsg.find("# 你要输出什么")
    out.append(sysmsg[i:] if i > 0 else sysmsg)

    out.append(section("4. build_chargen_user —— 车卡任务书里的预算段（改）"))
    u = P.build_chargen_user(ATTRS, P.__dict__.get("_x") or {}, "")
    out.append(u)

    out.append(section("5. build_chargen_table_system —— 车卡桌边讨论（改：接上语域）"))
    out.append(P.build_chargen_table_system(SEAT, "（这里本来是规则分节）", "（这里本来是简报）"))

    out.append(section("6. build_table_talk_system —— 桌边插话轮（改：接上语域）"))
    out.append(P.build_table_talk_system(SEAT))

    out.append(section("7. build_table_talk_user —— 插话轮的任务书（未改，供对照）"))
    out.append(P.build_table_talk_user(["摇人（阿凯）：（你们先别动）"], "你在一楼门厅。", "阿凯"))

    text = "".join(out)
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        ROOT / "本轮新增提示词.md"
    target.write_text(text, encoding="utf-8")
    print(f"已写出：{target}（{len(text)} 字）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
