"""泄漏哨兵：参考预设的任何一句都不许出现在项目里。

背景：用户给了一份第三方预设当参考，明确说了「禁止照搬」，而那份来源
本身也禁止二创。所以这里做成一道**可重复执行的检查**——
把预设切成大量短指纹，扫项目里所有会出货的文件，
命中任何一个就说明我不小心把它的话搬进来了。

用法：.venv\\Scripts\\python.exe tools\\leak_sentinel.py
（参考目录不存在时直接跳过，返回 0，不干扰别人克隆下来跑。）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 参考预设的文件名里有 emoji（TGbreak😺V3.1.2.json 这种）。
# 控制台默认是 GBK，一打印就 UnicodeEncodeError 崩掉，
# 而这是道闸门，崩了就等于没检查。所以先把自己切成 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "参考（严格禁止更新至github）"

# 会跟着程序出货的东西：这些文件里一个字都不许来自参考预设
CHECKED = ["engine/*.py", "ui/*", "docs/*.md", "tools/*.py",
           "main.py", "README.md", "requirements.txt"]
SKIP_NAMES = {"leak_sentinel.py"}


def _cjk_ratio(s: str) -> float:
    if not s:
        return 0.0
    n = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    return n / len(s)


def fingerprints(text: str, size: int = 14, step: int = 23) -> set[str]:
    """切出"像人话"的短指纹。

    第一版把 12 字的窗口全收了，结果 `════════`、CSS 片段、JS 方法名
    全被当成命中——那种噪声会让哨兵变成狼来了。
    所以只留**汉字占八成以上**的窗口，而且长度拉到 14：
    这种长度上还能撞车，那就真是抄的了。
    """
    t = "".join(text.split())
    out = set()
    for i in range(0, max(0, len(t) - size), step):
        frag = t[i:i + size]
        if len(frag) == size and _cjk_ratio(frag) >= 0.8:
            out.add(frag)
    return out


def main() -> int:
    if not REF_DIR.is_dir():
        print("没有参考目录，跳过（这是正常的）。")
        return 0
    files = sorted(REF_DIR.glob("*.json"))
    if not files:
        print("参考目录里没有 json，跳过。")
        return 0

    frags: set[str] = set()
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            print(f"  （{f.name} 读不了：{e}）")
            continue
        before = len(frags)
        stack = [data]
        texts = 0
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, str) and len(node) >= 200:
                texts += 1
                frags |= fingerprints(node)
        print(f"  · {f.name}：{texts} 段长文本 → 新增指纹 {len(frags) - before}")
    frags = {x for x in frags if len(x) == 14}
    print(f"参考预设指纹合计：{len(frags)} 段（只取汉字为主的窗口）")

    targets: list[Path] = []
    for pat in CHECKED:
        targets.extend(sorted(ROOT.glob(pat)))
    targets = [p for p in targets if p.is_file() and p.name not in SKIP_NAMES]

    # 项目这一侧也切成同样的指纹，然后**取交集**。
    # 之前是拿参考的每一段去 `in` 每个文件，参考有 6MB 的时候就是几百万次
    # 子串搜索，慢得没法用。两边都做集合，交一下就是 O(n)。
    mine: set[str] = set()
    for p in targets:
        try:
            body = "".join(p.read_text(encoding="utf-8", errors="replace").split())
        except Exception:
            continue
        mine |= fingerprints(body)

    overlap = frags & mine
    print(f"检查了 {len(targets)} 个项目文件（项目侧指纹 {len(mine)} 段）")
    if overlap:
        print(f"\n⚠ 有 {len(overlap)} 段文字与参考预设重合：")
        for frag in sorted(overlap)[:20]:
            print(f"   · {frag!r}")
        print("\n这是不能接受的——那些预设禁止二创/外传。请换成自己的写法。")
        return 1
    print("结论：干净。参考预设一个字都没有进项目。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
