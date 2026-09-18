"""本地掷骰脚本 —— 命令行直接掷，用的是跟引擎**同一个内核**。

所以命令行掷出来的分布，和跑团时 AI 掷出来的分布是同一套随机源
（操作系统熵池），不存在"两套骰子"的问题。

用法：

    # 掷任意表达式
    python tools/roll.py 3d6 1d100 1d8+2 2d6+1d4

    # 掷属性（COC7：STR/CON/DEX/APP/POW 是 3d6×5，SIZ/INT/EDU 是 (2d6+6)×5）
    python tools/roll.py --attrs
    python tools/roll.py --luck

    # 过检定（d100 对抗技能值，自动判成功等级）
    python tools/roll.py --check 侦查 65
    python tools/roll.py --check 聆听 40 --hard
    python tools/roll.py --check 图书馆利用 70 --extreme --bonus 1

    # 检验随机性（掷很多次看分布）
    python tools/roll.py --stats 1d6 6000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import chargen                                  # noqa: E402
from engine.dice import DiceKernel, fairness_probe          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="本地掷骰（COC7），与引擎共用同一个随机内核",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("expr", nargs="*", help="骰子表达式，例如 3d6 / 1d100 / 1d8+2")
    ap.add_argument("--check", nargs=2, metavar=("技能", "值"),
                    help="过一次检定，例如 --check 侦查 65")
    ap.add_argument("--difficulty", choices=["regular", "hard", "extreme"],
                    default="regular", help="达标线（默认 regular）")
    ap.add_argument("--hard", action="store_true", help="等同 --difficulty hard")
    ap.add_argument("--extreme", action="store_true", help="等同 --difficulty extreme")
    ap.add_argument("--bonus", type=int, default=0, help="奖励骰个数")
    ap.add_argument("--penalty", type=int, default=0, help="惩罚骰个数")
    ap.add_argument("--attrs", action="store_true", help="掷一套 COC7 属性")
    ap.add_argument("--luck", action="store_true", help="掷幸运（3d6×5）")
    ap.add_argument("--stats", nargs=2, metavar=("表达式", "次数"),
                    help="掷很多次看分布，例如 --stats 1d6 6000")
    ap.add_argument("--seed", type=int, default=None,
                    help="固定种子（只用于复现，正式跑团别用）")
    args = ap.parse_args()

    kernel = DiceKernel(seed=args.seed)
    if not kernel.fair:
        print("⚠ 使用了固定种子，结果可复现但不是真随机\n")

    diff = args.difficulty
    if args.hard:
        diff = "hard"
    if args.extreme:
        diff = "extreme"

    did = False

    if args.check:
        skill, raw = args.check
        try:
            value = int(raw)
        except ValueError:
            print(f"技能值要是数字：{raw}")
            return 1
        res = kernel.do_check("你", skill, value, difficulty=diff,
                              bonus=args.bonus, penalty=args.penalty)
        print(res.text())
        did = True

    if args.attrs:
        a = chargen.roll_attributes(kernel.rng)
        print("COC7 属性（3d6×5 / (2d6+6)×5，幸运 3d6×5）：")
        for k in ("STR", "CON", "DEX", "APP", "POW", "SIZ", "INT", "EDU", "LUCK"):
            print(f"  {k:<5}{a[k]:>3}")
        d = chargen.derive(a)
        print(f"  派生：HP {d['HP']}  MP {d['MP']}  SAN {d['SAN']}  "
              f"DB {d['DB']}  MOV {d['MOV']}")
        print(f"  技能点预算：职业 {a['EDU'] * 4} + 兴趣 {a['INT'] * 2} "
              f"= {a['EDU'] * 4 + a['INT'] * 2}")
        did = True

    if args.luck:
        total, detail = 0, ""
        from engine.dice import roll_expr
        total, detail = roll_expr("3d6", kernel.rng)
        print(f"幸运 3d6×5 = {total * 5}（3d6 掷出 {total}：{detail}）")
        did = True

    if args.stats:
        expr, times = args.stats
        try:
            n = int(times)
        except ValueError:
            print(f"次数要是数字：{times}")
            return 1
        import re
        m = re.fullmatch(r"(\d*)d(\d+)", expr.strip().lower())
        if not m:
            print("--stats 目前只支持 NdM 形式，例如 1d6")
            return 1
        probe = fairness_probe(kernel, sides=int(m.group(2)), times=n)
        print(f"{expr} 掷 {probe['times']} 次：")
        for face, cnt in probe["counts"].items():
            bar = "█" * int(cnt / max(1, probe["expected"]) * 20)
            print(f"  {face:>3}  {cnt:>5}  {bar}")
        print(f"  理论值 {probe['expected']:.0f}／卡方 {probe['chi2']}"
              f"／最大偏差 {probe['max_deviation'] * 100:.1f}%")
        print("  （卡方小于约 16 就算分布正常）")
        did = True

    if args.expr:
        for e in args.expr:
            info = kernel.do_roll("你", e, "", source="cli")
            print(f"{e:<10} → {info['total']}"
                  + (f"   （{info['detail']}）" if info["detail"] else ""))
        did = True

    if not did:
        ap.print_help()
        return 0

    print(f"\n[随机源：{kernel.audit()['entropy']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
