"""ML triage model: backends, wiring, fallbacks, and adversarial probes.

Uses stub models throughout — no transformers/torch needed on this VM.
The real zero-shot/finetuned models are exercised on real hardware via
scripts/triage_label_batch.py and scripts/finetune_triage.py.
"""

import asyncio
import importlib.util
import json
import os
import time

import pytest

from porter import triage
from porter import triage_model as tm
from porter.config import settings
from porter.embeddings import HashEmbedder
from porter.repositories import InMemoryMemoryRepository
from porter.triage_model import (
    BaseTriageModel,
    HeuristicTriageModel,
    TriageScore,
    build_triage_model,
)


# ------------------------------------------------------------------ fixtures
class StubTriageModel(BaseTriageModel):
    """Canned scores: {text_substring: {category: score}}."""

    def __init__(self, canned=None, delay=0.0, backend="stub"):
        self.canned = canned or {}
        self.delay = delay
        self._backend = backend
        self.calls = 0

    @property
    def backend(self):
        return self._backend

    def score(self, text, tags=()):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        cats = {}
        for needle, scores in self.canned.items():
            if needle in text:
                cats.update(scores)
        return TriageScore(
            sensitive=bool(cats), categories=cats, latency_ms=1.0, backend=self._backend
        )


@pytest.fixture(autouse=True)
def _clean_caches():
    tm.clear_score_cache()
    tm.clear_model_cache()
    yield
    tm.clear_score_cache()
    tm.clear_model_cache()


def _repo():
    return InMemoryMemoryRepository(HashEmbedder())


async def _write(repo, content, model=None, **kw):
    kw.setdefault("type", "fact")
    kw.setdefault("scope_tags", ["general"])
    kw.setdefault("confidence", 0.9)
    kw.setdefault("source", {"via": "test"})
    if model is not None:
        import porter.triage_model as _tmm

        _orig = _tmm.get_triage_model
        _tmm.get_triage_model = lambda: model
        try:
            return await triage.create_with_triage(repo, content=content, via="test", **kw)
        finally:
            _tmm.get_triage_model = _orig
    return await triage.create_with_triage(repo, content=content, via="test", **kw)


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------- heuristic backend
def test_heuristic_wraps_rules():
    m = HeuristicTriageModel()
    s = m.score("I take insulin daily", ["general"])
    assert s.sensitive and s.categories.get("health") == 1.0
    s = m.score("I prefer dark mode", ["general"])
    assert not s.sensitive
    s = m.score("   ", ["general"])
    assert not s.sensitive
    # never raises on hostile input
    s = m.score("\x00\ud800", ["health "])
    assert isinstance(s.sensitive, bool)


def test_heuristic_tag_signal():
    m = HeuristicTriageModel()
    s = m.score("hello world", ["finance"])
    assert s.sensitive and "finance" in s.categories


# ------------------------------------------------------- selection logic
def test_build_kinds_and_invalid():
    assert isinstance(build_triage_model("heuristic"), HeuristicTriageModel)
    with pytest.raises(ValueError):
        build_triage_model("nope")


def test_zeroshot_missing_deps_errors_clearly():
    if tm.zeroshot_importable():
        pytest.skip("transformers installed here")
    with pytest.raises(RuntimeError, match="pip install transformers torch"):
        build_triage_model("zeroshot")


def test_auto_falls_back_to_heuristic_without_deps(monkeypatch):
    monkeypatch.setattr(tm, "zeroshot_importable", lambda: False)
    monkeypatch.setattr(tm, "_finetuned_path_exists", lambda: False)
    m = build_triage_model("auto")
    assert isinstance(m, HeuristicTriageModel)


def test_resolve_auto_priority(monkeypatch):
    # finetuned path wins when the directory exists
    monkeypatch.setattr(tm, "_finetuned_path_exists", lambda: True)
    monkeypatch.setattr(tm, "zeroshot_importable", lambda: True)
    assert tm._resolve_auto_kind() == "finetuned"
    # then zeroshot
    monkeypatch.setattr(tm, "_finetuned_path_exists", lambda: False)
    assert tm._resolve_auto_kind() == "zeroshot"
    # then heuristic
    monkeypatch.setattr(tm, "zeroshot_importable", lambda: False)
    assert tm._resolve_auto_kind() == "heuristic"


def test_finetuned_missing_path_errors_clearly(tmp_path):
    with pytest.raises(RuntimeError, match="no model at"):
        build_triage_model("finetuned", model_path=str(tmp_path / "does-not-exist"))


# ------------------------------------------------------- decide() ML signal
def test_decide_ml_categories_queue():
    d = triage.decide(
        content="I prefer dark mode",
        scope_tags=["general"],
        confidence=0.9,
        encrypted=False,
        similar=[],
        ml_categories=["health"],
    )
    assert d.action == "queue"
    assert "ml_sensitive:health" in d.reasons


def test_decide_ml_blocks_auto_merge():
    from uuid import uuid4

    target = uuid4()
    d = triage.decide(
        content="I prefer dark mode",
        scope_tags=["general"],
        confidence=0.9,
        encrypted=False,
        similar=[(target, 0.99)],
        ml_categories=["finance"],
    )
    assert d.action == "queue"
    assert d.merge_target_id is None


# ------------------------------------------------------- two-domain rules
def test_decide_personal_domain_queues():
    d = triage.decide(
        content="I prefer dark mode",
        scope_tags=["general"],
        confidence=0.9,
        encrypted=False,
        similar=[],
        domain="personal",
    )
    assert d.action == "queue"
    assert "personal_domain" in d.reasons


def test_decide_project_domain_auto_approves_with_tag():
    d = triage.decide(
        content="Repo uses Ruff for linting",
        scope_tags=["project:porter"],
        confidence=0.9,
        encrypted=False,
        similar=[],
        domain="project",
    )
    assert d.action == "auto_approve"


def test_decide_project_domain_needs_project_tag():
    d = triage.decide(
        content="Repo uses Ruff for linting",
        scope_tags=["general"],
        confidence=0.9,
        encrypted=False,
        similar=[],
        domain="project",
    )
    assert d.action == "queue"
    assert "missing_project_scope" in d.reasons


def test_decide_project_conflict_is_per_project():
    from uuid import uuid4

    a, b = uuid4(), uuid4()
    # Same project tag, conflict band -> side-by-side queue.
    d = triage.decide(
        content="Use Ruff for linting",
        scope_tags=["project:porter"],
        confidence=0.9,
        encrypted=False,
        similar=[(a, 0.80, ["project:porter"])],
        domain="project",
    )
    assert d.action == "queue"
    assert "possible_conflict" in d.reasons
    # Same similarity but a DIFFERENT project tag -> not a conflict.
    d = triage.decide(
        content="Use Ruff for linting",
        scope_tags=["project:porter"],
        confidence=0.9,
        encrypted=False,
        similar=[(b, 0.80, ["project:other"])],
        domain="project",
    )
    assert d.action == "auto_approve"


def test_decide_unknown_domain_auto_approve_eligible():
    d = triage.decide(
        content="the sky is blue today",
        scope_tags=["general"],
        confidence=0.9,
        encrypted=False,
        similar=[],
        domain="unknown",
    )
    assert d.action == "auto_approve"


def test_heuristic_domain_broad_project():
    """The heuristic sees the whole project working memory — instructions,
    task state, decisions — not just AGENTS.md-style text."""
    m = HeuristicTriageModel()
    for text in (
        "AGENTS.md says: always run ruff before commit",
        "working on the sync refactor, blocked by the OAuth review",
        "decided: Postgres over SQLite for concurrent writers",
        "gotcha: the cursor expires after 60s of inactivity",
    ):
        s = m.score(text, ["general"])
        assert s.domain == "project", text
    s = m.score("I take insulin daily", ["general"])
    assert s.domain == "personal"
    s = m.score("I prefer dark mode", ["general"])
    assert s.domain == "unknown"  # no signal -> neutral


def test_explicit_project_type_end_to_end():
    """create_with_triage honors an explicit project domain; the ML
    second opinion can only ever pull toward personal, never away."""

    async def _go():
        repo = _repo()
        # untagged project write queues on first pass...
        rec, d = await triage.create_with_triage(
            repo,
            content="Repo uses Ruff",
            type="project",
            scope_tags=["general"],
            confidence=0.9,
            source={"via": "test"},
            via="test",
        )
        assert d.action == "queue"
        assert "missing_project_scope" in d.reasons
        # ...and a tagged one auto-approves (ML disabled -> unknown).
        rec2, d2 = await triage.create_with_triage(
            repo,
            content="Repo uses Ruff for lint",
            type="project",
            scope_tags=["project:porter"],
            confidence=0.9,
            source={"via": "test"},
            via="test",
        )
        assert d2.action == "auto_approve"
        assert rec2.status == "active"

    _run(_go())


def test_v1_preference_maps_to_personal():
    d = triage.decide(
        content="I prefer dark mode",
        scope_tags=["general"],
        confidence=0.9,
        encrypted=False,
        similar=[],
        domain=triage.OLD_TYPE_TO_NEW["preference"],
    )
    assert d.action == "queue"
    assert "personal_domain" in d.reasons


# ------------------------------------------------------- wiring
def test_ml_second_opinion_queues_rule_passed_write():
    async def _go():
        repo = _repo()
        model = StubTriageModel({"A1C": {"health": 0.9}})
        rec, d = await _write(repo, "my A1C came back at 9.2", model=model)
        assert d.action == "queue"
        assert "ml_sensitive:health" in d.reasons
        assert rec.status == "pending"
        # audited with triage_model meta
        audits = await repo.audit_list()
        m = next(a for a in audits if rec.id in a.memory_ids)
        assert m.meta["triage_model"]["backend"] == "stub"

    _run(_go())


def test_rules_prefilter_skips_model():
    """Rules are the fast pre-filter: an already-queued write never pays
    model latency."""

    async def _go():
        repo = _repo()
        model = StubTriageModel({"anything": {"health": 0.99}})
        rec, d = await _write(repo, "hello", scope_tags=["health"], model=model)
        assert d.action == "queue"
        assert "sensitive_tag" in d.reasons
        assert model.calls == 0

    _run(_go())


def test_model_missing_falls_back_to_rules(monkeypatch):
    """PORTER_TRIAGE_MODEL=auto with no transformers -> heuristic path,
    writes still work."""

    async def _go():
        monkeypatch.setattr(tm, "zeroshot_importable", lambda: False)
        monkeypatch.setattr(tm, "_finetuned_path_exists", lambda: False)
        repo = _repo()
        rec, d = await _write(repo, "I prefer dark mode")
        assert d.action == "auto_approve"

    _run(_go())


def test_slow_model_timeout_falls_back_and_audits(monkeypatch):
    async def _go():
        monkeypatch.setattr(settings, "triage_model_timeout", 0.2)
        repo = _repo()
        model = StubTriageModel(delay=5.0)  # would exceed any sane timeout
        rec, d = await _write(repo, "I prefer dark mode", model=model)
        assert d.action == "auto_approve"  # rule decision stands
        audits = await repo.audit_list()
        m = next(a for a in audits if rec.id in a.memory_ids)
        assert m.meta["triage_model"]["fallback"] == "timeout"

    _run(_go())


def test_e2ee_never_reaches_model():
    async def _go():
        repo = _repo()
        model = StubTriageModel({"x": {"health": 0.99}})
        rec, d = await _write(repo, "ciphertext-blob", model=model, encrypted=True, key_id="k1")
        assert d.action == "queue"
        assert d.reasons == ["encrypted"]
        assert model.calls == 0

    _run(_go())


def test_threshold_edge_deterministic():
    async def _go():
        for text, score_val, queued in [
            ("edge low", 0.64, False),
            ("edge high", 0.66, True),
        ]:
            tm.clear_score_cache()
            repo = _repo()
            model = StubTriageModel({text: {"health": score_val}})
            rec, d = await _write(repo, f"note about {text}", model=model)
            assert (d.action == "queue") == queued, text

    _run(_go())


def test_score_lru_cache():
    m = StubTriageModel({"hello": {"health": 0.9}})
    tm.cached_score(m, "hello world", ())
    tm.cached_score(m, "hello world", ())
    assert m.calls == 1
    # different content -> miss
    tm.cached_score(m, "hello there", ())
    assert m.calls == 2


def test_cached_score_never_raises():
    class Boom(BaseTriageModel):
        def score(self, text, tags=()):
            raise RuntimeError("boom")

    s = tm.cached_score(Boom(), "anything", ())
    assert not s.sensitive


# ------------------------------------------------------- adversarial probes
def test_probe_paraphrase_rules_catch_zh():
    """Rules catch what they know; the model is the backstop for the rest."""
    # rules know this one (pattern)
    d = triage.decide(
        content="我对花生过敏", scope_tags=["general"], confidence=0.9, encrypted=False, similar=[]
    )
    assert d.action == "queue" and "sensitive_content" in d.reasons

    # rules MISS this paraphrase ("blood sugar"/"A1C" aren't in the
    # patterns); the ML second opinion catches it
    async def _go():
        repo = _repo()
        model = StubTriageModel({"A1C": {"health": 0.91}, "blood sugar": {"health": 0.88}})
        for text in ("my A1C came back at 9.2", "my blood sugar has been running high"):
            tm.clear_score_cache()
            repo = _repo()
            rec, d = await _write(repo, text, model=model)
            assert d.action == "queue", text
            assert "ml_sensitive:health" in d.reasons, text
        # and the same text with no model signal auto-approves (rules pass)
        tm.clear_score_cache()
        repo2 = _repo()
        model2 = StubTriageModel({})
        rec2, d2 = await _write(repo2, "my A1C came back at 9.2", model=model2)
        assert d2.action == "auto_approve"

    _run(_go())


def test_probe_domain_evasion_model_backstop():
    """The heuristic misses some project task-state phrasings; the ML
    domain classifier is the backstop (simulated here with a stub —
    the real zero-shot hypothesis "this text is about an ongoing task,
    decision or work context" covers this class on real hardware)."""

    class DomainStub(StubTriageModel):
        def __init__(self, canned, domains):
            super().__init__(canned)
            self.domains = domains

        def score(self, text, tags=()):
            s = super().score(text, tags)
            for needle, dom in self.domains.items():
                if needle in text:
                    s.domain = dom
                    s.domain_scores = {dom: 0.9}
            return s

    async def _go():
        # heuristic alone: no project signal -> unknown -> would
        # auto-approve (no sensitivity); the model says project.
        text = "still waiting on review feedback for the deploy"
        assert HeuristicTriageModel().score(text, ["general"]).domain == "unknown"
        model = DomainStub({}, {"review feedback": "project"})
        repo = _repo()
        rec, d = await _write(repo, text, model=model, scope_tags=["project:porter"])
        # project + tagged -> auto-approve either way; what matters is the
        # audit trail records the model's domain vote.
        assert d.action == "auto_approve"
        audits = await repo.audit_list()
        m = next(a for a in audits if rec.id in a.memory_ids)
        assert m.meta["triage_model"]["predicted_domain"] == "project"

    _run(_go())


def test_probe_obfuscated_content_still_queued_by_rules():
    # zero-width / fullwidth smuggling defeated by NFKC normalization
    for sneaky in ("my ｓａｌａｒｙ is 200k", "my sal​ary is 200k"):
        d = triage.decide(
            content=sneaky, scope_tags=["general"], confidence=0.9, encrypted=False, similar=[]
        )
        assert d.action == "queue", sneaky


def test_probe_empty_and_hostile_input():
    m = HeuristicTriageModel()
    for bad in ("", "   ", "\x00", "�" * 5000):
        s = m.score(bad, [])
        assert isinstance(s.sensitive, bool)
    d = triage.decide(
        content="", scope_tags=[], confidence=0.9, encrypted=False, similar=[], ml_categories=[]
    )
    assert d.action in ("auto_approve", "queue")  # schema rejects empty upstream


# ------------------------------------------------------- scripts
def _load_script(name):
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", name)
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_label_batch_output_format(tmp_path):
    lb = _load_script("triage_label_batch.py")
    inp = tmp_path / "in.txt"
    inp.write_text("I was diagnosed with diabetes\nI prefer dark mode\n\n", encoding="utf-8")
    out = tmp_path / "out.jsonl"
    model = HeuristicTriageModel()
    n = lb.write_jsonl(lb.iter_input_file(str(inp)), str(out), model)
    assert n == 2
    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert set(first.keys()) == {"text", "domain", "project", "sensitivity", "predicted_by"}
    assert first["domain"] == "personal"  # health -> personal domain
    assert first["project"] is None
    assert set(first["sensitivity"].keys()) == {"health", "finance", "pii", "credentials"}
    assert all(v in (0, 1) for v in first["sensitivity"].values())
    assert first["sensitivity"]["health"] == 1
    assert first["predicted_by"] == "heuristic"
    second = json.loads(lines[1])
    assert second["domain"] == "personal"  # heuristic has no project signal
    assert second["sensitivity"]["health"] == 0


def test_label_batch_project_tags(tmp_path):
    lb = _load_script("triage_label_batch.py")
    out = tmp_path / "out.jsonl"
    model = HeuristicTriageModel()
    n = lb.write_jsonl([("Repo uses Ruff for linting", ["project:porter"])], str(out), model)
    assert n == 1
    first = json.loads(out.read_text(encoding="utf-8").strip())
    assert first["domain"] == "project"
    assert first["project"] == "porter"


def test_finetune_arg_parsing_and_dry_run(tmp_path, capsys):
    ft = _load_script("finetune_triage.py")
    # 8-example corrected fixture: (text, domain, sensitivity vec)
    inp = tmp_path / "labels.jsonl"
    rows = [
        ("my blood sugar is high", "personal", [1, 0, 0, 0]),
        ("血糖有点高", "personal", [1, 0, 0, 0]),
        ("my salary is 200k", "personal", [0, 1, 0, 0]),
        ("call me at 13800138000", "personal", [0, 0, 1, 0]),
        ("the password is hunter2", "personal", [0, 0, 0, 1]),
        ("I prefer dark mode", "personal", [0, 0, 0, 0]),
        ("we decided to use postgres", "project", [0, 0, 0, 0]),
        ("Repo uses Ruff for linting", "project", [0, 0, 0, 0]),
    ]
    with open(inp, "w", encoding="utf-8") as f:
        for text, domain, vec in rows:
            f.write(
                json.dumps(
                    {
                        "text": text,
                        "domain": domain,
                        "project": None,
                        "sensitivity": dict(zip(["health", "finance", "pii", "credentials"], vec)),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    out = tmp_path / "model"
    metrics = ft.main(["--input", str(inp), "--output", str(out), "--dry-run"])
    assert metrics["eval_macro_f1"] == 0.5
    captured = capsys.readouterr()
    assert "fake-train" in captured.out
    assert "deploy: PORTER_TRIAGE_MODEL_PATH=" in captured.out
    # 6 labels land in the right layout: 2 domain one-hot + 4 sensitivity.
    examples = ft.load_labeled_jsonl(str(inp))
    assert all(len(vec) == 6 for _, vec in examples)
    assert examples[6][1] == [0, 1, 0, 0, 0, 0]  # project one-hot
    assert examples[0][1] == [1, 0, 1, 0, 0, 0]  # personal + health


def test_finetune_rejects_malformed(tmp_path):
    ft = _load_script("finetune_triage.py")
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"text": "x", "domain": "personal"}\n' * 8, encoding="utf-8")
    with pytest.raises(ValueError, match="sensitivity must be 0/1"):
        ft.load_labeled_jsonl(str(bad))
    bad_domain = tmp_path / "bad_domain.jsonl"
    bad_domain.write_text(
        '{"text": "x", "domain": "task_state", '
        '"sensitivity": {"health": 0, "finance": 0, "pii": 0, '
        '"credentials": 0}}\n' * 8,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="domain must be one of"):
        ft.load_labeled_jsonl(str(bad_domain))
    few = tmp_path / "few.jsonl"
    few.write_text(
        '{"text": "x", "domain": "personal", '
        '"sensitivity": {"health": 0, "finance": 0, "pii": 0, '
        '"credentials": 0}}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="at least 8"):
        ft.load_labeled_jsonl(str(few))


def test_finetuned_selection_prefers_path(monkeypatch, tmp_path):
    from porter.config import settings as _settings

    monkeypatch.setattr(_settings, "triage_model_path", str(tmp_path))
    # tmp_path exists -> _finetuned_path_exists() is True

    created = {}

    class FakeFT(BaseTriageModel):
        def __init__(self, **kw):
            created.update(kw)

        def score(self, text, tags=()):
            return TriageScore(sensitive=False, backend="finetuned")

    monkeypatch.setattr(tm, "FinetunedTriageModel", FakeFT)
    m = build_triage_model("auto")
    assert isinstance(m, FakeFT)
