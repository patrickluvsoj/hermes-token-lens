"""Evolution guardrails: rubric caps, threshold immutability, score history,
rule-proposal validation, EVOLUTION.md logging."""
import json
import time

import pytest

import engine
import token_lens_core as core


@pytest.fixture(autouse=True)
def evolution_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def test_rubric_v1_seeded_from_file(db):
    version, md = engine.active_rubric(db)
    assert version == 1
    assert "Usefulness" in md


def test_amendment_applies_as_new_version_with_log(db):
    engine.active_rubric(db)
    new_md = (
        "# rubric v2\n"
        "| Usefulness | 0–4 | … |\n| Specificity | 0–2 | … |\n"
        "| Savings credibility | 0–2 | … |\n| Actionability | 0–2 | … |\n"
    )
    v = engine.apply_rubric_amendment(db, rubric_md=new_md, rationale="reweight usefulness")
    assert v == 2
    version, md = engine.active_rubric(db)
    assert version == 2 and "0–4" in md
    log = engine.evolution_log_path().read_text()
    assert "rubric v1 → v2" in log and "reweight usefulness" in log


def test_amendment_rejected_over_7_criteria(db):
    engine.active_rubric(db)
    rows = "\n".join(f"| Criterion{i} | 0–1 | x |" for i in range(8))
    assert engine.apply_rubric_amendment(db, rubric_md=rows, rationale="bloat") is None
    assert engine.active_rubric(db)[0] == 1  # unchanged


def test_amendment_rejected_touching_threshold(db):
    engine.active_rubric(db)
    md = "| Usefulness | 0–3 | x |\nset score_threshold to 2 for leniency"
    assert engine.apply_rubric_amendment(db, rubric_md=md, rationale="sneaky") is None


def test_prior_scores_never_rewritten(db):
    engine.active_rubric(db)
    with core.write_txn(db):
        sid = core.insert_suggestion(
            db, run_id=None, fingerprint="x", title="t", evidence="e",
            plan_md="p", category="memory", est_savings_pct=5,
            kind="llm", scores={"total": 7},
        )
    engine.apply_rubric_amendment(
        db, rubric_md="| Usefulness | 0–3 | x |", rationale="v2")
    row = db.execute("SELECT scores_json FROM suggestions WHERE id=?", (sid,)).fetchone()
    assert json.loads(row["scores_json"])["total"] == 7  # untouched


def test_rule_proposal_applies_and_recompute_is_lazy(db):
    v = engine.apply_rule_proposal(db, {
        "kind": "system_block", "category": "memory",
        "pattern": r"(?ms)^## Recalled Facts.*?(?=^## |\Z)",
        "rationale": "unattributed share was 14%",
    })
    assert v == 2
    rules_version, rules = core.load_rules(db)
    assert rules_version == 2
    assert any("Recalled Facts" in b["pattern"] for b in rules["system_blocks"])
    assert "rules v1 → v2" in engine.evolution_log_path().read_text()


def test_rule_proposal_rejects_new_category_id(db):
    assert engine.apply_rule_proposal(db, {
        "kind": "system_block", "category": "brand_new_top_level",
        "pattern": "x", "rationale": "nope",
    }) is None


def test_rule_proposal_rejects_bad_regex(db):
    assert engine.apply_rule_proposal(db, {
        "kind": "system_block", "category": "memory",
        "pattern": "([unclosed", "rationale": "broken",
    }) is None


def test_rule_proposal_dedupes_identical_pattern(db):
    p = {"kind": "system_block", "category": "memory",
         "pattern": r"^## X.*$", "rationale": "r"}
    assert engine.apply_rule_proposal(db, p) == 2
    assert engine.apply_rule_proposal(db, p) is None
