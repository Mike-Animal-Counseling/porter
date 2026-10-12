"""Fine-tune a multi-label triage classifier (triage training loop, STEP 3 of 4).

  python scripts/finetune_triage.py --input labels_corrected.jsonl --output models/triage-v1

Input JSONL (hand-corrected output of scripts/triage_label_batch.py):
  {"text": "...", "domain": "personal", "project": null,
   "sensitivity": {"health": 0, "finance": 1, "pii": 0, "credentials": 0}}

Trains microsoft/mdeberta-v3-base with a 6-label classification head
(multi_label): 2 domain labels (personal, project — argmax at inference)
+ 4 sensitivity labels (independent). Eval split, saves the best
checkpoint to --output, and prints per-label precision/recall/F1 plus
domain accuracy on the eval split at the end.

Step 4 (deploy): set PORTER_TRIAGE_MODEL_PATH to the output dir (or
PORTER_TRIAGE_MODEL=finetuned); `auto` picks it up automatically.

CPU-friendly defaults: 3 epochs, batch 8, lr 2e-5. A few hundred labeled
examples is enough to beat zero-shot on your distribution.

Dependencies: transformers, torch, scikit-learn.
  pip install transformers torch scikit-learn
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LABELS = [
    # Dimension 1 (domain): mutually exclusive in practice, argmax at inference.
    "domain_personal",
    "domain_project",
    # Dimension 2 (sensitivity): independent.
    "sens_health",
    "sens_finance",
    "sens_pii",
    "sens_credentials",
]
DOMAIN_LABELS = ["personal", "project"]
SENS_LABELS = ["health", "finance", "pii", "credentials"]
DEFAULT_BASE_MODEL = "microsoft/mdeberta-v3-base"
DEFAULT_OUTPUT = "models/triage-v1"


# ------------------------------------------------------------ data loading
def load_labeled_jsonl(path):
    """Load hand-corrected labels. Returns [(text, [6 floats]), ...].

    Label order is LABELS: 2 one-hot domain entries then 4 sensitivity
    bits. "project" (the project:<name> scope) is metadata for the
    human and is NOT trained on — triage derives it from tags at write
    time. Raises ValueError on malformed lines (fail fast — bad labels
    train a bad model).
    """
    examples = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {e}")
            text = obj.get("text", "")
            domain = obj.get("domain", "")
            sens = obj.get("sensitivity", {})
            if not text or not text.strip():
                raise ValueError(f"{path}:{lineno}: empty text")
            if domain not in DOMAIN_LABELS:
                raise ValueError(
                    f"{path}:{lineno}: domain must be one of {DOMAIN_LABELS}")
            try:
                sens_vec = [int(sens[cat]) for cat in SENS_LABELS]
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(
                    f"{path}:{lineno}: sensitivity must be 0/1 for "
                    f"{SENS_LABELS}: {e}")
            if any(v not in (0, 1) for v in sens_vec):
                raise ValueError(f"{path}:{lineno}: sensitivity must be 0/1")
            domain_vec = [1 if d == domain else 0 for d in DOMAIN_LABELS]
            examples.append((text, domain_vec + sens_vec))
    if len(examples) < 8:
        raise ValueError(
            f"only {len(examples)} examples — need at least 8 "
            "(a few hundred recommended)")
    return examples


def split_train_eval(examples, eval_fraction=0.2, seed=42):
    """Deterministic shuffle split. Keeps at least 2 eval examples."""
    import random

    rng = random.Random(seed)
    idx = list(range(len(examples)))
    rng.shuffle(idx)
    n_eval = max(2, int(len(examples) * eval_fraction))
    n_eval = min(n_eval, len(examples) - 2)  # keep train non-trivial
    eval_idx = set(idx[:n_eval])
    train = [e for i, e in enumerate(examples) if i not in eval_idx]
    ev = [e for i, e in enumerate(examples) if i in eval_idx]
    return train, ev


# ------------------------------------------------------------ torch dataset
def make_torch_dataset(examples, tokenizer, max_length=256):
    """Wrap tokenized examples for HF Trainer. Falls back to plain lists
    when torch is unavailable (dry-run)."""
    try:
        import torch

        _torch = torch
    except ImportError:
        _torch = None

    texts = [t for t, _ in examples]
    enc = tokenizer(texts, truncation=True, padding=True, max_length=max_length)
    label_rows = [[float(v) for v in vec] for _, vec in examples]

    if _torch is None:
        class _ListDS:
            def __len__(self):
                return len(examples)

            def __getitem__(self, i):
                return {
                    "input_ids": enc["input_ids"][i],
                    "attention_mask": enc["attention_mask"][i],
                    "labels": label_rows[i],
                }

        return _ListDS()

    class _DS(_torch.utils.data.Dataset):
        def __init__(self):
            self.enc = enc
            self.labels = _torch.tensor(label_rows, dtype=_torch.float)

        def __len__(self):
            return len(examples)

        def __getitem__(self, i):
            return {
                "input_ids": self.enc["input_ids"][i],
                "attention_mask": self.enc["attention_mask"][i],
                "labels": self.labels[i],
            }

    return _DS()


def compute_metrics_fn():
    """Per-label precision/recall/F1 at 0.5 (multi-label) + domain accuracy
    (argmax over the 2 domain logits)."""
    import numpy as np
    from sklearn.metrics import precision_recall_fscore_support

    def _compute(eval_pred):
        logits, labels = eval_pred
        logits = np.asarray(logits)
        labels = np.asarray(labels)
        probs = 1 / (1 + np.exp(-logits))
        preds = (probs >= 0.5).astype(int)
        out = {}
        for i, name in enumerate(LABELS):
            p, r, f, _ = precision_recall_fscore_support(
                labels[:, i], preds[:, i], average="binary", zero_division=0)
            out[f"{name}_precision"] = round(float(p), 4)
            out[f"{name}_recall"] = round(float(r), 4)
            out[f"{name}_f1"] = round(float(f), 4)
        # Domain accuracy: argmax over the 2 domain heads.
        domain_pred = probs[:, :2].argmax(axis=1)
        domain_true = labels[:, :2].argmax(axis=1)
        out["domain_accuracy"] = round(
            float((domain_pred == domain_true).mean()), 4)
        # macro F1 drives checkpoint selection
        out["macro_f1"] = round(
            float(sum(out[f"{n}_f1"] for n in LABELS) / len(LABELS)), 4)
        return out

    return _compute


# ------------------------------------------------------------ training
def train(args, tokenizer_factory, model_factory, trainer_factory,
          training_args_factory, compute_metrics=None):
    """Train and save. All heavy dependencies arrive through the factories,
    so --dry-run and tests can inject stubs (no transformers/torch needed)."""
    examples = load_labeled_jsonl(args.input)
    print(f"loaded {len(examples)} labeled examples from {args.input}")
    train_ex, eval_ex = split_train_eval(
        examples, eval_fraction=args.eval_split, seed=args.seed)
    print(f"train={len(train_ex)} eval={len(eval_ex)}")

    tokenizer = tokenizer_factory()
    train_ds = make_torch_dataset(train_ex, tokenizer)
    eval_ds = make_torch_dataset(eval_ex, tokenizer)
    model = model_factory()

    training_args = training_args_factory(args)
    trainer = trainer_factory(
        model=model, args=training_args,
        train_dataset=train_ds, eval_dataset=eval_ds,
        compute_metrics=compute_metrics,
    )
    trainer.train()

    # Save best + tokenizer + label map.
    os.makedirs(args.output, exist_ok=True)
    trainer.save_model(args.output)
    if hasattr(tokenizer, "save_pretrained"):
        tokenizer.save_pretrained(args.output)
    with open(os.path.join(args.output, "labels.json"), "w") as f:
        json.dump({"labels": LABELS}, f, indent=2)

    print("\n=== eval (best checkpoint) ===")
    metrics = trainer.evaluate()
    for cat in LABELS:
        print(f"  {cat:13s} P={metrics.get(f'eval_{cat}_precision', float('nan')):.3f} "
              f"R={metrics.get(f'eval_{cat}_recall', float('nan')):.3f} "
              f"F1={metrics.get(f'eval_{cat}_f1', float('nan')):.3f}")
    print(f"  macro_f1={metrics.get('eval_macro_f1', float('nan')):.3f}")
    print(f"\nsaved -> {args.output}")
    print("deploy: PORTER_TRIAGE_MODEL_PATH=" + args.output)
    return metrics


# ------------------------------------------------------------ dry-run stubs
class _StubTokenizer:
    """Whitespace tokenizer for --dry-run (no downloads)."""

    def __call__(self, texts, truncation=True, padding=True, max_length=256):
        ids, masks = [], []
        for t in texts:
            toks = t.split()[:max_length]
            ids.append([min(999, len(w)) for w in toks] or [0])
            masks.append([1] * len(ids[-1]))
        ml = max(len(x) for x in ids)
        ids = [x + [0] * (ml - len(x)) for x in ids]
        masks = [x + [0] * (ml - len(x)) for x in masks]

        class _T:
            def __getitem__(self, i):
                return {"input_ids": ids[i], "attention_mask": masks[i]}

        return _T()

    def save_pretrained(self, path):
        pass


class _StubTrainer:
    """Pretends to train: validates the plumbing, returns fake metrics."""

    def __init__(self, model=None, args=None, train_dataset=None,
                 eval_dataset=None, compute_metrics=None):
        self.args = args
        self.n_train = len(train_dataset) if train_dataset else 0
        self.n_eval = len(eval_dataset) if eval_dataset else 0

    def train(self):
        print(f"[dry-run] fake-train on {self.n_train} examples "
              f"({self.args.num_train_epochs} epochs)")

    def evaluate(self):
        return {f"eval_{c}_{m}": 0.5
                for c in LABELS for m in ("precision", "recall", "f1")} | \
               {"eval_macro_f1": 0.5}

    def save_model(self, path):
        os.makedirs(path, exist_ok=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Fine-tune the triage classifier on hand-corrected labels.")
    ap.add_argument("--input", required=True, help="corrected JSONL")
    ap.add_argument("--output", default=DEFAULT_OUTPUT, help="model dir")
    ap.add_argument("--base-model", default=DEFAULT_BASE_MODEL)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--eval-split", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true",
                    help="plumbing check with stub tokenizer/trainer "
                         "(no downloads, no training)")
    args = ap.parse_args(argv)

    if args.dry_run:
        from types import SimpleNamespace

        return train(
            args,
            tokenizer_factory=_StubTokenizer,
            model_factory=lambda: object(),
            trainer_factory=_StubTrainer,
            training_args_factory=lambda a: SimpleNamespace(
                output_dir=a.output, num_train_epochs=a.epochs,
                per_device_train_batch_size=a.batch_size, seed=a.seed),
            compute_metrics=None,  # stub trainer ignores it; avoids sklearn import
        )

    from transformers import (AutoModelForSequenceClassification,
                              AutoTokenizer, Trainer, TrainingArguments)

    def _real_training_args(a):
        return TrainingArguments(
            output_dir=a.output,
            num_train_epochs=a.epochs,
            per_device_train_batch_size=a.batch_size,
            per_device_eval_batch_size=a.batch_size,
            learning_rate=a.lr,
            eval_strategy="epoch",
            save_strategy="epoch",
            load_best_model_at_end=True,
            metric_for_best_model="macro_f1",
            greater_is_better=True,
            save_total_limit=2,
            seed=a.seed,
            report_to="none",
            logging_steps=10,
        )

    return train(
        args,
        tokenizer_factory=lambda: AutoTokenizer.from_pretrained(args.base_model),
        model_factory=lambda: AutoModelForSequenceClassification.from_pretrained(
            args.base_model, num_labels=len(LABELS),
            problem_type="multi_label_classification"),
        trainer_factory=Trainer,
        training_args_factory=_real_training_args,
        compute_metrics=compute_metrics_fn(),
    )


if __name__ == "__main__":
    main()
