"""剧透哨兵。

守秘人要在开局前给玩家写一份「不含幕后的开场简报」。
但光靠一句"不要剧透"是靠不住的——LLM 经常一边保证不剧透一边把关键名字写出来。

所以这里做两层：
  1. **事前**：从模组真相里抽出「秘密词表」——那些在真相里反复出现、
     却从没在公开材料（模组简介、第一幕）里出现过的专有名词。
     把这张表直接塞进守秘人的提示词里，告诉它「这些词一个都不许出现」。
     给出具体清单，比给一句抽象禁令有效得多。
  2. **事后**：拿同一张表去查它写出来的简报，命中就带着命中词重写一次；
     还漏就直接退回到作者自己写的公开简介，宁可简报平淡，也不能剧透。

局限：这是词面匹配，不是语义检查。换了说法（把「叹息之像」写成「那尊小雕像」）
它就抓不到。所以它是**兜底**，不是保证。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

# 常见中文词，避免把「他们」「这个」当成秘密词
STOPWORDS: set[str] = {
    "这个", "那个", "什么", "怎么", "因为", "所以", "但是", "如果", "就是", "可以",
    "一个", "已经", "没有", "不是", "他们", "我们", "你们", "自己", "时候", "现在",
    "知道", "觉得", "可能", "应该", "开始", "然后", "而且", "还有", "或者", "这样",
    "那样", "这里", "那里", "一次", "一直", "一起", "一点", "一些", "之后", "之前",
    "的话", "一样", "只是", "还是", "为了", "关于", "对于", "通过", "以及", "并且",
    "不会", "不能", "需要", "想要", "起来", "出来", "下来", "过去", "过来", "发现",
    "看到", "听到", "感到", "时候", "地方", "东西", "事情", "问题", "情况", "方式",
    "上面", "下面", "里面", "外面", "前面", "后面", "左边", "右边", "中间", "附近",
    "第一", "第二", "第三", "最后", "非常", "特别", "真的", "确实", "好像", "似乎",
    "决定", "认为", "感觉", "发生", "出现", "进入", "离开", "回来", "回家", "出去",
    "开门", "关门", "说话", "告诉", "问道", "回答", "看着", "听着", "想着", "接着",
    "不再", "仍然", "依然", "终于", "突然", "立刻", "马上", "现在", "当时", "后来",
    "玩家", "角色", "调查", "守秘", "模组", "建议", "注意", "提醒", "记录", "内容",
    # 跑团与规则领域的通用词——这些不是秘密，出现很正常
    "守秘人", "主持人", "调查员", "玩家们", "理智值", "理智损", "克苏鲁", "第七版",
    "规则书", "技能值", "成功等", "奖励骰", "惩罚骰", "大失败", "大成功", "人物卡",
    "角色卡", "时间线", "暗线", "幕后", "真相", "剧情", "故事", "线索", "道具",
    "检定", "掷骰", "属性", "职业", "技能", "场景", "回合", "进行", "判定",
    "开场", "结局", "后续", "玩家会", "可能会", "这一段", "另外", "其中", "以及",
}


def _is_stop(gram: str) -> bool:
    """判断一个片段是不是普通词。

    三层过滤，缺一不可：
      · 精确命中通用词表
      · 片段**包含**某个通用词（「调查员们」包含「调查」）
      · 片段**是**某个通用词的一部分（「秘人」是「守秘人」的一部分）
        —— 少了这一层，真相里反复出现的「守秘人」会被切出「秘人」当秘密词
    """
    if gram in STOPWORDS:
        return True
    for s in STOPWORDS:
        if len(s) >= 2 and (s in gram or gram in s):
            return True
    return False

CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def _ngrams(text: str, lo: int = 2, hi: int = 6) -> Counter:
    """把中文串切成 2-6 字的连续片段并计数。连续英文/数字串单独保留。"""
    out: Counter = Counter()
    for run in CJK_RUN.findall(text or ""):
        n = len(run)
        for size in range(lo, hi + 1):
            for i in range(n - size + 1):
                gram = run[i:i + size]
                if _is_stop(gram):
                    continue
                out[gram] += 1
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text or ""):
        out[word.lower()] += 1
    return out


def _public_text(module: Any) -> str:
    """玩家在开局前就有权看到的东西：模组简介、开场设定、第一幕。"""
    parts = [
        getattr(module, "title", "") or "",
        getattr(module, "summary", "") or "",
        getattr(module, "premise", "") or "",
    ]
    scenes = list(getattr(module, "scenes", []) or [])
    if scenes:
        parts.append(scenes[0].body or "")
    return "\n".join(parts)


def hidden_terms(module: Any, min_freq: int = 2, limit: int = 50) -> list[str]:
    """抽出「反复出现在真相里、却从没在公开材料里出现过」的词。

    这些就是这个模组的关键秘密。把它们列给守秘人看，比让它自己猜边界可靠得多。
    """
    truth = (getattr(module, "truth", "") or "").strip()
    if not truth:
        return []
    public = _public_text(module)
    truth_grams = _ngrams(truth, 2, 6)
    public_grams = _ngrams(public, 2, 6)

    candidates: list[tuple[float, str]] = []
    for gram, freq in truth_grams.items():
        # 两字词门槛更高：中文里两字组合太多，频次低的几乎都是噪声
        need = 3 if len(gram) <= 2 else min_freq
        if freq < need:
            continue
        if gram in public_grams:
            continue
        # 短词更容易是巧合；长词更可能是专名。用长度给一点权重。
        candidates.append((freq * (1.0 + 0.35 * (len(gram) - 2)), gram))

    candidates.sort(key=lambda x: (-x[0], -len(x[1])))
    # 去掉被更长候选词包含的短词，避免刷屏
    picked: list[str] = []
    for _, gram in candidates:
        if any(gram in longer for longer in picked):
            continue
        picked.append(gram)
        if len(picked) >= limit:
            break
    return picked


def audit(text: str, terms: Iterable[str]) -> list[str]:
    """检查一段文字里出现了哪些秘密词。"""
    blob = text or ""
    return [t for t in terms if t and t in blob]


def brief_prompt_block(terms: list[str], limit: int = 30) -> str:
    """把秘密词表做成提示词里的一段硬约束。"""
    if not terms:
        return ""
    shown = terms[:limit]
    more = "（还有更多同类，不一一列出）" if len(terms) > limit else ""
    return (
        "# 这是这个模组的关键秘密，你写的东西里一个都不能出现\n"
        + "、".join(shown) + more + "\n"
        "上面这些词是玩家现在**绝对不该知道**的。你写的开场介绍里不能提到它们，"
        "也不能用同义的说法把它们的性质讲出来。"
    )
