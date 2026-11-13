"""异常分流规则。

三类异常筐：
- missing   缺号：源行没有编码（sku 为空）
- collision 号相撞：同一批内非空编码重复出现
- short     短题：标题长度低于下限

优先级（一行只进一筐）：缺号 > 号相撞 > 短题。
被压过的命中规则写进 priority_note，便于「点开看优先说明」。
"""

from collections import Counter

SHORT_TITLE_MIN_LEN = 6

BUCKET_MISSING = "missing"
BUCKET_COLLISION = "collision"
BUCKET_SHORT = "short"

BUCKET_LABELS = {
    BUCKET_MISSING: "缺号",
    BUCKET_COLLISION: "号相撞",
    BUCKET_SHORT: "短题",
}

# 数值越小优先级越高
_PRIORITY = (BUCKET_MISSING, BUCKET_COLLISION, BUCKET_SHORT)


def classify_items(items: list[dict]) -> list[dict]:
    """对一批源条目做分流，返回与入参等长、同序的判定结果。

    正常条目 {"kind": "normal", ...}；异常条目 {"kind": "exception",
    "bucket": ..., "reason": ..., "priority_note": ...}。
    """
    sku_counts = Counter(
        (it.get("sku") or "").strip()
        for it in items
        if (it.get("sku") or "").strip()
    )

    results = []
    for index, it in enumerate(items):
        sku = (it.get("sku") or "").strip()
        title = (it.get("title") or "").strip()
        hits = []
        if not sku:
            hits.append(BUCKET_MISSING)
        elif sku_counts[sku] > 1:
            hits.append(BUCKET_COLLISION)
        if len(title) < SHORT_TITLE_MIN_LEN:
            hits.append(BUCKET_SHORT)

        if not hits:
            results.append(
                {"kind": "normal", "index": index, "sku": sku, "title": title}
            )
            continue

        winner = min(hits, key=_PRIORITY.index)
        suppressed = [h for h in hits if h != winner]
        results.append(
            {
                "kind": "exception",
                "index": index,
                "sku": sku,
                "title": title,
                "bucket": winner,
                "reason": _reason(winner, sku, title, sku_counts),
                "priority_note": _priority_note(winner, suppressed),
            }
        )
    return results


def _reason(bucket: str, sku: str, title: str, sku_counts: Counter) -> str:
    if bucket == BUCKET_MISSING:
        return "缺号：源行未提供编码（sku 为空），无法入账"
    if bucket == BUCKET_COLLISION:
        return (
            f"号相撞：编码 {sku} 在本批出现 {sku_counts[sku]} 次，"
            "同一编码不能重复入账"
        )
    return (
        f"短题：标题「{title}」长度 {len(title)} 低于下限 "
        f"{SHORT_TITLE_MIN_LEN}，须人工改判"
    )


def _priority_note(winner: str, suppressed: list[str]) -> str:
    if not suppressed:
        return f"仅命中{BUCKET_LABELS[winner]}，直接进筐"
    chain = "压过".join(
        BUCKET_LABELS[name] for name in (winner, *suppressed)
    )
    return f"同一行同时命中{chain}，按优先级一行只进「{BUCKET_LABELS[winner]}」筐"
