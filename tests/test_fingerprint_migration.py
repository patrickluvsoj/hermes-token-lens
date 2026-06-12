"""M3-T1: schema v2 fingerprint rewrite + cross-kind inheritance."""
import time

import token_lens_core as core


def _v1_db_with_old_fingerprints(db_path):
    """Build a DB at schema v1 carrying pre-M3 fingerprints, then reopen at
    current version so the v2 migration runs against real old rows."""
    conn = core.connect(db_path)  # current schema; we'll backdate it
    rows = [
        ("mcp_disable:gbrain", "detector"),
        ("llm:mcp:gbrain", "llm"),
        ("llm:config:skills-index", "llm"),
        ("cache_prefix", "detector"),
        ("system_prompt_trim", "detector"),
        ("turn_cap", "detector"),
        ("tool_results_weight", "detector"),
    ]
    with core.write_txn(conn):
        for fp, kind in rows:
            conn.execute(
                "INSERT INTO suggestions (run_id, fingerprint, title, evidence,"
                " plan_md, category, est_savings_pct, risk, kind, scores_json,"
                " created_at, status) VALUES (NULL, ?, 't', 'e', 'p', 'c', 5,"
                " 'low', ?, '{}', ?, 'shown')",
                (fp, kind, time.time()),
            )
        conn.execute("PRAGMA user_version=1")  # backdate: pretend pre-M3
    conn.close()


def test_v2_migration_rewrites_old_fingerprints(db_path):
    _v1_db_with_old_fingerprints(db_path)
    conn = core.connect(db_path)  # migration ladder runs v2
    fps = sorted(r["fingerprint"] for r in conn.execute(
        "SELECT fingerprint FROM suggestions").fetchall())
    assert fps == sorted([
        "mcp:gbrain",            # detector mcp_disable: rewritten
        "mcp:gbrain",            # llm:mcp: rewritten — now COLLIDES (the point)
        "config:skills-index",   # llm: prefix stripped
        "config:cache-prefix",
        "config:system-prompt",
        "behavior:turn-cap",
        "behavior:tool-results",
    ])
    assert conn.execute("PRAGMA user_version").fetchone()[0] == core.SCHEMA_VERSION
    conn.close()


def test_v2_migration_idempotent(db_path):
    _v1_db_with_old_fingerprints(db_path)
    core.connect(db_path).close()
    conn = core.connect(db_path)  # second open: patterns no longer match
    assert conn.execute(
        "SELECT COUNT(*) FROM suggestions WHERE fingerprint='mcp:gbrain'"
    ).fetchone()[0] == 2
    conn.close()


def test_canonical_fingerprint_normalization():
    assert core.canonical_fingerprint("mcp:Dead Server!") == "mcp:dead-server"
    assert core.canonical_fingerprint("config:cache-prefix") == "config:cache-prefix"
    assert core.canonical_fingerprint("  Weird  (Target)  ") == "weird-target"


def test_cross_kind_inheritance_dismiss_detector_blocks_llm(db):
    """The bug M3-T1 fixes: dismissing the detector's card must retire a
    later LLM regeneration about the same target (same canonical fp)."""
    with core.write_txn(db):
        det_id = core.insert_suggestion(
            db, run_id=None, fingerprint="mcp:gbrain", title="detector finding",
            evidence="e", plan_md="p", category="tool_schemas.mcp.gbrain",
            est_savings_pct=4.2, kind="detector",
        )
    with core.write_txn(db):
        core.set_suggestion_status(db, det_id, "dismissed")
    with core.write_txn(db):
        llm_id = core.insert_suggestion(
            db, run_id=None, fingerprint=core.canonical_fingerprint("mcp:gbrain"),
            title="LLM finding about the same server", evidence="e", plan_md="p",
            category="tool_schemas.mcp", est_savings_pct=3.5, kind="llm",
        )
    status = db.execute(
        "SELECT status FROM suggestions WHERE id=?", (llm_id,)
    ).fetchone()["status"]
    assert status == "dismissed"


def test_cross_kind_done_cascades_both_ways(db):
    with core.write_txn(db):
        det_id = core.insert_suggestion(
            db, run_id=None, fingerprint="mcp:x", title="d", evidence="e",
            plan_md="p", category="c", est_savings_pct=5, kind="detector",
        )
        llm_id = core.insert_suggestion(
            db, run_id=None, fingerprint="mcp:x", title="l", evidence="e",
            plan_md="p", category="c", est_savings_pct=5, kind="llm",
        )
    with core.write_txn(db):
        core.set_suggestion_status(db, llm_id, "done")
    det_status = db.execute(
        "SELECT status FROM suggestions WHERE id=?", (det_id,)
    ).fetchone()["status"]
    assert det_status == "done"  # cascade retired the sibling card
