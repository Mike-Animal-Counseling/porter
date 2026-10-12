"""Batch-label memories for hand-correction (triage training loop, STEP 1 of 4).

The loop:
  1. PREDICT  this script: run the current triage stack over candidate texts,
             emit JSONL with predicted 0/1 labels per category.
  2. CORRECT  a human fixes the labels (this is the training signal — your
             inbox approvals/rejections are the ground truth).
  3. TRAIN    scripts/finetune_triage.py on the corrected JSONL.
  4. DEPLOY   point PORTER_TRIAGE_MODEL_PATH at the output dir (or set
             PORTER_TRIAGE_MODEL=finetuned); `auto` picks it up automatically.

Usage:
  python scripts/triage_label_batch.py --input texts.txt --output labels.jsonl
  python scripts/triage_label_batch.py --from-db --limit 200 --output labels.jsonl
  python scripts/triage_label_batch.py --input texts.txt --model heuristic

Input file: one memory text per line (blank lines skipped).
--from-db: samples the N most recent memories from Postgres (DATABASE_URL).

Output JSONL — one object per line. Hand-correct "domain",
"project", and the 0/1 sensitivity labels in place:
  {"text": "...", "domain": "personal", "project": null,
   "sensitivity": {"health": 0, "finance": 1, "pii": 0, "credentials": 0},
   "predicted_by": "zeroshot"}

"domain" is one of: personal | project (two memory domains — personal is
the user's own memories, project is the harness's working memory for a
project/workspace: instructions, task state, decisions, codebase
knowledge). "project" is the project:<name> scope for project-domain
memories (null when the text itself carries no identifiable project).
"sensitivity" is dimension 2 (orthogonal): a personal memory can carry a
health hit, and triage queues on EITHER personal domain OR any
sensitivity signal.

Notes:
- "predicted_by" records which backend produced the prediction
  (zeroshot | heuristic | finetuned) — useful when re-running after deploy.
- Encrypted memories are skipped (ciphertext is never labeled).
- A few hundred corrected examples is enough to beat zero-shot on YOUR
  distribution; past ~2000 the gains flatten for this 6-label task.
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from porter import triage_model as tm  # noqa: E402
from porter.config import settings  # noqa: E402

SENS_LABELS = ["health", "finance", "pii", "credentials"]
DOMAIN_LABELS = ["personal", "project"]


def _project_names(tags):
    """Extract project names from `project:<name>` tags (normalized)."""
    out = set()
    for t in tags or ():
        norm = t.strip().lower().replace("：", ":")
        if norm.startswith("project:"):
            name = norm[len("project:"):].strip()
            if name:
                out.add(name)
    return out


def predict_labels(text, model=None, tags=()):
    """Run the triage stack; return (domain, project, sensitivity, backend).

    "project" is the project:<name> scope derived from tags when the
    domain is project (null otherwise / when untagged).
    """
    model = model or tm.get_triage_model()
    score = tm.cached_score(model, text, tags)
    threshold = float(getattr(settings, "triage_model_threshold", 0.65))
    sensitivity = {
        cat: 1 if score.categories.get(cat, 0.0) >= threshold else 0
        for cat in SENS_LABELS
    }
    domain = score.domain if score.domain in DOMAIN_LABELS else "personal"
    project = None
    if domain == "project":
        names = _project_names(tags)
        project = sorted(names)[0] if names else None
    return domain, project, sensitivity, score.backend


def iter_input_file(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if text:
                yield text


async def iter_from_db(limit):
    """Sample recent non-encrypted memories from Postgres.

    Yields (content, scope_tags) pairs; tags feed the project:<name>
    derivation for project-domain labels.
    """
    try:
        import asyncpg
    except ImportError:
        raise SystemExit(
            "asyncpg is required for --from-db: pip install asyncpg")
    dsn = os.getenv("DATABASE_URL",
                    getattr(settings, "database_url", ""))
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT content, scope_tags FROM memories "
            "WHERE status IN ('pending','active') "
            "AND COALESCE(encrypted, FALSE) = FALSE "
            "ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        for r in rows:
            if r["content"] and r["content"].strip():
                yield r["content"], list(r["scope_tags"] or [])
    finally:
        await conn.close()


def write_jsonl(texts, output_path, model=None):
    """texts: iterable of (text, tags) pairs."""
    model = model or tm.get_triage_model()
    n = 0
    with open(output_path, "w", encoding="utf-8") as out:
        for item in texts:
            text, tags = item if isinstance(item, tuple) else (item, ())
            domain, project, sensitivity, backend = predict_labels(
                text, model, tags)
            out.write(json.dumps({
                "text": text,
                "domain": domain,
                "project": project,
                "sensitivity": sensitivity,
                "predicted_by": backend,
            }, ensure_ascii=False) + "\n")
            n += 1
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Predict triage labels for hand-correction (JSONL).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="text file, one memory per line")
    src.add_argument("--from-db", action="store_true",
                     help="sample recent memories from Postgres")
    ap.add_argument("--limit", type=int, default=200,
                    help="max rows for --from-db (default 200)")
    ap.add_argument("--output", required=True, help="output JSONL path")
    ap.add_argument("--model", default=None,
                    help="triage model kind (default: PORTER_TRIAGE_MODEL)")
    args = ap.parse_args(argv)

    if args.model:
        os.environ["PORTER_TRIAGE_MODEL"] = args.model
        tm.clear_model_cache()
    model = tm.get_triage_model()
    print(f"triage backend: {model.backend}", file=sys.stderr)

    if args.from_db:
        async def _collect():
            return [t async for t in iter_from_db(args.limit)]
        texts = asyncio.run(_collect())
    else:
        texts = list(iter_input_file(args.input))

    n = write_jsonl(texts, args.output, model)
    print(f"wrote {n} labeled lines -> {args.output}", file=sys.stderr)
    print("next: hand-correct the 0/1 labels, then run "
          "scripts/finetune_triage.py", file=sys.stderr)


if __name__ == "__main__":
    main()
