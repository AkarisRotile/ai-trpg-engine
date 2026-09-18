"""从规则书全文里挖术语定义，生成 data/rules/glossary_extra.yaml。

为什么要脚本处理而不是人工抄：
  规则书 56 万字，人工挑术语既慢又会漏。但也没必要把整本书喂给模型——
  真正需要的只是「术语 → 一句话定义」这张表。脚本负责挖，引擎负责按需注入。

脚本做两件事：
  1. 模式挖掘：扫描 `术语：定义` / `术语是指……` 这类句式，按出现频次筛候选。
  2. 定向补全：对一份重要的 COC 术语清单，回原文找它最像"定义句"的那一句。

产出只作为**候补**：engine/glossary.py 的内建权威表优先级更高，
被内建表覆盖的条目不会生效，所以脚本挖歪了也不会污染引擎。

用法：<venv>\\Scripts\\python.exe tools\\build_glossary.py
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import config as cfgmod          # noqa: E402
from engine.glossary import GLOSSARY         # noqa: E402

# 值得定向找定义的重点术语（内建表里已有权威定义的会自动跳过）
TARGET_TERMS = [
    "孤注一掷", "临时性疯狂", "不定性疯狂", "疯狂发作", "重伤", "濒死",
    "奖励骰", "惩罚骰", "对抗检定", "先攻", "反击", "瞄准", "护甲",
    "信用评级", "职业点", "兴趣点", "克苏鲁神话", "魔法点", "理智检定",
    "理智损失", "神话生物", "旧日支配者", "调查员", "守秘人",
    "战斗轮", "逃脱检定", "警觉检定", "闪避", "伤害加值", "移动力",
    "急救", "精神分析", "心理治疗", "幕间成长", "经验检定",
]

DEF_PATTERNS = [
    re.compile(r"^(?P<term>[\u4e00-\u9fff]{2,6})\s*[:：]\s*(?P<def>[^\n]{6,90})$", re.M),
    re.compile(r"(?P<term>[\u4e00-\u9fff]{2,6})(?:是指|指的是|意思是|表示|即)\s*"
               r"(?P<def>[^\n。；]{6,80})"),
]

# 定义句里常见的噪声，命中就丢掉
NOISE = ("见图", "见第", "见附", "参见", "表格", "下一页", "上页", "本书", "本章")


def _clean(text: str) -> str:
    text = re.sub(r"\s+", "", text or "")
    text = re.sub(r"^[，。、；：,.;:]+", "", text)
    text = re.sub(r"[，。、；：,.;:]+$", "", text)
    return text.strip()


def _usable(defn: str) -> bool:
    if not (6 <= len(defn) <= 60):
        return False
    if any(n in defn for n in NOISE):
        return False
    if re.search(r"\d{2,}", defn):        # 页码残留
        return False
    return True


def harvest(books: list[Path]) -> dict[str, str]:
    found: dict[str, str] = {}
    freq: Counter = Counter()

    for book in books:
        try:
            text = book.read_text(encoding="utf-8")
        except Exception:
            continue
        for pat in DEF_PATTERNS:
            for m in pat.finditer(text):
                term = _clean(m.group("term"))
                defn = _clean(m.group("def"))
                freq[term] += 1
                if not _usable(defn):
                    continue
                if term in found:
                    continue
                found[term] = defn

    # 频次太低的候选多半是排版噪声
    found = {t: d for t, d in found.items() if freq[t] >= 2}

    # 定向补全：重要术语回原文找最像定义的一句
    for term in TARGET_TERMS:
        if term in GLOSSARY or term in found:
            continue
        best = ""
        for book in books:
            try:
                text = book.read_text(encoding="utf-8")
            except Exception:
                continue
            for m in re.finditer(re.escape(term), text):
                s = text.rfind("。", 0, m.start()) + 1
                e = text.find("。", m.end())
                if e == -1:
                    continue
                sentence = _clean(text[s:e])
                if term not in sentence:
                    continue
                if not _usable(sentence) or len(sentence) > 55:
                    continue
                # 优先"术语在句首"且包含定义动词的句子
                score = 0
                if sentence.startswith(term):
                    score += 2
                if any(k in sentence for k in ("是指", "指的是", "表示", "即", "时")):
                    score += 1
                if score >= 2:
                    best = sentence
                    break
            if best:
                break
        if best:
            found[term] = best

    return found


def main() -> int:
    full = cfgmod.data_root() / "rules" / "full"
    books = sorted(full.glob("*.txt")) if full.is_dir() else []
    if not books:
        print("没有找到规则书全文。请先运行 tools/extract_rules.py。")
        return 1

    print(f"扫描 {len(books)} 本规则书 …")
    found = harvest(books)
    skipped = [t for t in found if t in GLOSSARY]
    usable = {t: d for t, d in found.items() if t not in GLOSSARY}

    print(f"  挖到候选 {len(found)} 条，其中 {len(skipped)} 条已被内建权威表覆盖（不写入）")
    print(f"  实际写入 {len(usable)} 条")

    import yaml
    out = cfgmod.data_root() / "rules" / "glossary_extra.yaml"
    header = (
        "# 由 tools/build_glossary.py 从规则书全文自动挖掘的术语候补。\n"
        "# engine/glossary.py 的内建权威表优先级更高，同名条目不会生效。\n"
        "# 可以手工修改或删除——这份文件就是给人改的。\n"
    )
    out.write_text(
        header + yaml.safe_dump(usable, allow_unicode=True, sort_keys=True,
                                width=100, default_flow_style=False),
        encoding="utf-8",
    )
    print(f"  已写入 {out}")
    for t, d in list(usable.items())[:12]:
        print(f"    {t}：{d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
