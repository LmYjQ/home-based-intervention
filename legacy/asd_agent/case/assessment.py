# -*- coding: utf-8 -*-
"""
把证据折成一份评估
==================

**纯函数，不碰数据库、不调模型。** 判据全在这里，图节点只调它。

★ 四类信息分开存，家长一眼能看出哪条是事实、哪条是系统的猜测
------------------------------------------------------------
    known_facts     你说过、而且原话能对得上
    hypotheses      我们根据已知的事推出来的，可能不对
    unknowns        还不清楚的
    contradictions  两处说法对不上，**两个都留着**

★ 什么能进 known_facts
----------------------
三条判据，**同时**满足：

    1. `status` 不是 `inferred`（我们推的不算事实）
    2. `support` 不是 `none`（原文根本没提的不算）
    3. `quote` 非空（没有可核对的原话的不算）

第 3 条在 `evidence.verify` 里已经降级过一次（自称直述却没引文 → 降为 inferred），
这里再挡一道 —— **两道防线是故意的**：
`verify` 挡的是"模型自称"，这里挡的是"万一漏了一个字段"。
同一个洞用两个不同位置的检查去堵，比在一个地方堵可靠。

★ 矛盾不自动删任何一条
----------------------
两条说法对不上时**并列保留**。删掉一条等于改写家长说过的话，
而原始陈述不可被覆盖（`refract.md` §9.4）。
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from .evidence import EvidenceOutcome, VerifiedClaim

#: 能进 known_facts 的 status。
FACT_STATUSES = ("parent_reported", "observed", "confirmed")

#: 能进 known_facts 的 support。
FACT_SUPPORTS = ("direct", "partial")

#: 两条 claim 的相似度超过这个值，就算**同一条**。
#:
#: ★ 为什么需要模糊比较
#: ------------------
#: 模型会把同一件事换个说法再吐一遍 ——
#: 「老师单独抱着他讲故事时能回答问题」与「老师一个人抱他讲故事时能回答问题」
#: 是同一条，逐字比较看不出来。
#: 而它一旦被当成"新证据"，多轮追问的判据（这一轮有没有新东西）就失真了：
#: 系统会以为自己学到了新东西，于是一直问下去。
SIMILARITY_IS_SAME = 0.8


def _norm(s: str) -> str:
    """归一化，用来判"是不是同一条"。**只用于比较，不用于展示。**"""
    return re.sub(r"[\s，。、,.!?！？：:；;\"'“”‘’（）()【】\[\]]+", "", s or "")


def _same_claim(a: str, b: str) -> bool:
    """两条 claim 是不是同一条。

    ★ 包含判定要**看长度差**
    -----------------------
    「孩子在幼儿园不愿意靠近其他小朋友」与
    「孩子在幼儿园不愿意靠近其他小朋友，换了新老师或新环境才会这样」
    —— 短的那条是长的那条的**子串**，但它们**不是同一件事**：
    后者多了一个关键信息（什么时候会这样）。

    实测：按"包含就算同一条"判，家长回答追问带来的新事实
    会被当成重复**丢掉**（而丢掉这一条，那次追问就白问了）。
    所以只有"短的至少占长的 80%"时才算同一件事。
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if short in long_ and len(short) >= 0.8 * len(long_):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= SIMILARITY_IS_SAME


def _is_fact(c: VerifiedClaim) -> bool:
    return (c.status in FACT_STATUSES
            and c.support in FACT_SUPPORTS
            and bool(c.quote.strip()))


def fold(assessment: dict[str, Any], outcome: EvidenceOutcome,
         answered_unknowns: list[str] | None = None
         ) -> tuple[dict[str, Any], list[VerifiedClaim], int]:
    """把这一轮的证据折进评估。

    返回 (新评估, 这一轮**新**出现的证据, 新证据条数)。

    ★ "新证据条数"是多轮追问的判据（`detect_unknowns` 用它）：
      家长回答了追问却没带来任何新信息 → 就不再问第二轮
      （否则就是"系统把自己没听懂的锅推给人"）。

    ★ 「还不清楚的」现在是**问题驱动**的
    ------------------------------------
    以前它来自模型单独给的 `unknowns` 清单，而那个清单在实测里经常是空的
    （模型把"还缺什么"整个塞进了一句自由问句里）。
    现在每一项都来自一个**具体的问题**（`ClarifyQuestion.unknown`），
    而家长答完的那一条会被**精确销掉**（`answered_unknowns`），
    不再靠"新事实碰巧覆盖了它"那种模糊匹配。
    """
    known = list(assessment.get("known_facts") or [])
    hypos = list(assessment.get("hypotheses") or [])
    unknowns = list(assessment.get("unknowns") or [])
    contradictions = list(assessment.get("contradictions") or [])
    risk_flags = list(assessment.get("risk_flags") or [])
    answered = list(answered_unknowns or [])

    known_keys = {_norm(k) for k in known}

    fresh: list[VerifiedClaim] = []
    for c in outcome.claims:
        key = _norm(c.claim)
        if not key:
            continue
        # ★ 判"是不是新的"要对**全部已知事实**做，而不是只对归一化后的集合做等值比较
        #   —— 模型会把同一件事换个说法再吐一遍（见 `_same_claim` 的说明）。
        if any(_same_claim(c.claim, k) for k in known + hypos):
            continue                       # 已经知道了 → 不算"新证据"
        known_keys.add(key)
        fresh.append(c)

        if _is_fact(c):
            known.append(c.claim)
        else:
            # 推出来的那部分进「猜测」，**不进事实**。
            hypos.append(c.claim)

    # ---- 「还不清楚的」：销掉已答的 → 销掉被新事实覆盖的 → 并上这一轮的 ----
    new_facts = [c.claim for c in fresh if _is_fact(c)]
    kept_unknowns: list[str] = []
    for u in list(unknowns) + [q.get("unknown", "") for q in outcome.questions]:
        if not u or not u.strip():
            continue
        if any(_same_claim(u, a) for a in answered):
            continue                       # ★ 家长已经答过了 → 精确销账
        un = _norm(u)
        if any(un and un in _norm(f) or _norm(f) in un for f in new_facts if un):
            continue                       # 已经被新事实回答掉了 → 收敛
        if any(_same_claim(u, k) for k in kept_unknowns):
            continue                       # 重复
        kept_unknowns.append(u)

    # ---- 矛盾：并列保留，去掉完全重复的那一对 ----
    seen_c = {(_norm(c.get("a", "")), _norm(c.get("b", ""))) for c in contradictions}
    for c in outcome.contradictions:
        pair = (_norm(c.get("a", "")), _norm(c.get("b", "")))
        if pair in seen_c or (pair[1], pair[0]) in seen_c:
            continue
        seen_c.add(pair)
        contradictions.append({"a": c.get("a", ""), "b": c.get("b", "")})

    new_assessment = {
        "known_facts": known,
        "hypotheses": hypos,
        "unknowns": kept_unknowns,
        "contradictions": contradictions,
        "risk_flags": risk_flags,
    }
    return new_assessment, fresh, len(fresh)
