"""Bilingual embedding eval for Porter memory retrieval.

Compares embedder backends (hash stub, qwen3, bge-m3, harrier,
embeddinggemma) on a small, hand-built zh/en memory set:

  1. Dedup P/R at the 0.92 duplicate threshold — known-duplicate pairs
     (paraphrases, incl. cross-lingual zh<->en) vs known-distinct pairs
     (hard negatives like like/dislike).
  2. Recall@k — paraphrased queries against the corpus; is the gold
     memory in the top-k?

Runs on a real machine with the model weights. On a box without torch /
network / HF cache (e.g. this VM), model-backed embedders are SKIPPED
gracefully with a clear message; the hash stub always runs so the
plumbing is verifiable everywhere.

Usage:
    python scripts/eval_embeddings.py
    python scripts/eval_embeddings.py --embedders hash,qwen3
    python scripts/eval_embeddings.py --embedders hash,qwen3,bge-m3,harrier,embeddinggemma
    python scripts/eval_embeddings.py --threshold 0.90 --top-k 1,3,5
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.embeddings import Embedder  # noqa: E402

# ---------------------------------------------------------------------------
# Eval set: 14 base memories (7 zh, 7 en incl. 2 cross-lingual dup pairs).
# ---------------------------------------------------------------------------

CORPUS: list[tuple[str, str]] = [
    ("m01", "用户喜欢深色模式，觉得白色背景刺眼"),
    ("m02", "用户对花生过敏"),
    ("m03", "用户每周五下午不安排会议"),
    ("m04", "用户偏好用 Tailwind 写样式"),
    ("m05", "用户住在 San Jose，没有车"),
    ("m06", "用户只喝美式咖啡"),
    ("m07", "User prefers dark mode, finds white backgrounds harsh"),
    ("m08", "User is allergic to peanuts"),
    ("m09", "No meetings on Friday afternoons"),
    ("m10", "User writes styles with Tailwind CSS"),
    ("m11", "User lives in San Jose and has no car"),
    ("m12", "Coffee: americano only"),
    ("m13", "用户喜欢深色模式"),  # cross-lingual dup of m07
    ("m14", "User is allergic to peanuts"),  # cross-lingual dup of m02
]

# (text_a, text_b) that SHOULD be judged duplicates.
DUP_PAIRS: list[tuple[str, str]] = [
    ("用户喜欢深色模式，觉得白色背景刺眼", "用户偏好深色主题，讨厌刺眼的白色背景"),
    ("用户对花生过敏", "用户有花生过敏史"),
    ("用户每周五下午不安排会议", "用户周五下午不排会"),
    ("用户偏好用 Tailwind 写样式", "用户写样式喜欢用 Tailwind"),
    ("用户住在 San Jose，没有车", "用户住 San Jose，无车"),
    ("用户只喝美式咖啡", "用户喝咖啡只喝美式"),
    (
        "User prefers dark mode, finds white backgrounds harsh",
        "User likes dark theme, dislikes glaring white backgrounds",
    ),
    ("User is allergic to peanuts", "The user has a peanut allergy"),
    ("No meetings on Friday afternoons", "User blocks Friday afternoons for no meetings"),
    ("User writes styles with Tailwind CSS", "User prefers Tailwind for styling"),
    ("User lives in San Jose and has no car", "User is carless, based in San Jose"),
    ("Coffee: americano only", "The user drinks only americano"),
    # cross-lingual duplicates (multilingual check)
    ("用户喜欢深色模式", "User prefers dark mode"),
    ("用户对花生过敏", "User is allergic to peanuts"),
    ("用户每周五下午不安排会议", "No meetings on Friday afternoons"),
]

# (text_a, text_b) that must NOT be judged duplicates (hard negatives).
DISTINCT_PAIRS: list[tuple[str, str]] = [
    ("用户喜欢深色模式，觉得白色背景刺眼", "用户喜欢浅色模式，觉得深色太压抑"),
    ("用户对花生过敏", "用户喜欢吃花生"),
    ("用户每周五下午不安排会议", "用户每周一上午不安排会议"),
    ("用户偏好用 Tailwind 写样式", "用户偏好用 Bootstrap 写样式"),
    ("用户住在 San Jose，没有车", "用户住在 San Francisco，有车"),
    ("用户只喝美式咖啡", "用户只喝拿铁"),
    ("User prefers dark mode", "User prefers light mode"),
    ("User is allergic to peanuts", "User loves eating peanuts"),
    ("No meetings on Friday afternoons", "No meetings on Monday mornings"),
    ("用户喜欢深色模式", "User prefers light mode"),  # cross-lingual negative
]

# (query, gold_memory_id): paraphrased queries, gold not verbatim in corpus.
QUERIES: list[tuple[str, str]] = [
    ("深色界面对眼睛比较友好", "m01"),
    ("花生会让他过敏", "m02"),
    ("周五下午别给我排会", "m03"),
    ("写 CSS 用什么框架顺手？", "m04"),
    ("他住哪？有车吗", "m05"),
    ("他喝什么咖啡", "m06"),
    ("dark themes are easier on the eyes", "m07"),
    ("peanut allergy", "m08"),
    ("keep Friday afternoons free", "m09"),
    ("styling framework preference", "m10"),
    ("does the user own a car", "m11"),
    ("coffee order", "m12"),
]


def _load_embedder(kind: str, http_url: str | None = None) -> Embedder | None:
    from porter.embeddings import build_embedder

    try:
        kwargs = {"base_url": http_url} if kind == "http" else {}
        emb = build_embedder(kind, **kwargs)
        # Force one encode so lazy model download/failure surfaces now.
        emb.embed_one("porter embedding smoke test 冒烟测试")
        return emb
    except Exception as exc:  # noqa: BLE001 - eval harness, report and skip
        print(f"  SKIP {kind}: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=1)
        return None


def eval_dedup(emb: Embedder, threshold: float) -> tuple[float, float, int, int]:
    """Return (precision, recall, tp, fp) at the duplicate threshold."""
    tp = fp = fn = 0
    for a, b in DUP_PAIRS:
        sim = Embedder.cosine(emb.embed_one(a), emb.embed_one(b))
        if sim >= threshold:
            tp += 1
        else:
            fn += 1
    for a, b in DISTINCT_PAIRS:
        sim = Embedder.cosine(emb.embed_one(a), emb.embed_one(b))
        if sim >= threshold:
            fp += 1
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return precision, recall, tp, fp


def eval_recall(emb: Embedder, ks: list[int]) -> dict[int, float]:
    ids = [mid for mid, _ in CORPUS]
    vecs = emb.embed([text for _, text in CORPUS])
    hits = {k: 0 for k in ks}
    for query, gold in QUERIES:
        qv = emb.embed_one(query)
        ranked = sorted(
            range(len(ids)),
            key=lambda i: Embedder.cosine(qv, vecs[i]),
            reverse=True,
        )
        rank = ranked.index(ids.index(gold)) + 1
        for k in ks:
            if rank <= k:
                hits[k] += 1
    return {k: hits[k] / len(QUERIES) for k in ks}


def main() -> int:
    ap = argparse.ArgumentParser(description="Porter bilingual embedding eval")
    ap.add_argument(
        "--embedders",
        default="hash,qwen3,bge-m3,harrier,embeddinggemma",
        help="comma-separated embedder kinds",
    )
    ap.add_argument(
        "--threshold", type=float, default=0.92, help="duplicate cosine threshold (default 0.92)"
    )
    ap.add_argument("--top-k", default="1,3,5", help="recall cutoffs, comma-separated")
    ap.add_argument(
        "--http-url",
        default="http://localhost:8001",
        help="running embedding service for --embedders http",
    )
    ap.add_argument("--output", help="write evaluation metrics as JSON")
    args = ap.parse_args()
    ks = [int(x) for x in args.top_k.split(",")]

    print(
        f"eval set: {len(CORPUS)} memories, {len(DUP_PAIRS)} dup pairs, "
        f"{len(DISTINCT_PAIRS)} distinct pairs, {len(QUERIES)} queries"
    )
    print(f"duplicate threshold: {args.threshold}")
    print(f"{'embedder':<10} {'dedup-P':>8} {'dedup-R':>8} " + " ".join(f"R@{k:<5}" for k in ks))
    print("-" * 60)

    ran = 0
    results = []
    for kind in [k.strip() for k in args.embedders.split(",") if k.strip()]:
        emb = _load_embedder(kind, args.http_url)
        if emb is None:
            continue
        ran += 1
        prec, rec, tp, fp = eval_dedup(emb, args.threshold)
        recalls = eval_recall(emb, ks)
        r_str = " ".join(f"{recalls[k]:<7.3f}" for k in ks)
        print(f"{kind:<10} {prec:>8.3f} {rec:>8.3f} {r_str} (tp={tp} fp={fp})")
        results.append(
            {
                "embedder": kind,
                "model": getattr(emb, "model_id", getattr(emb, "MODEL_ID", kind)),
                "dimension": emb.dim,
                "dedup_precision": prec,
                "dedup_recall": rec,
                "true_positives": tp,
                "false_positives": fp,
                "recall_at": recalls,
            }
        )

    if args.output:
        Path(args.output).write_text(
            json.dumps(
                {
                    "memories": len(CORPUS),
                    "queries": len(QUERIES),
                    "duplicate_pairs": len(DUP_PAIRS),
                    "distinct_pairs": len(DISTINCT_PAIRS),
                    "threshold": args.threshold,
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    if ran == 0:
        print(
            "\nNo embedder could run. On a real machine: "
            "pip install sentence-transformers torch, then re-run — "
            "qwen3/bge-m3/harrier/embeddinggemma download from HuggingFace "
            "on first use."
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
