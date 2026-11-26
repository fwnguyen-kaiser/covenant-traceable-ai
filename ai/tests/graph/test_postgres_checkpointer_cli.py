import json
from uuid import uuid4

import psycopg

from covenant_ai.cli import run_cli
from covenant_ai.providers.db_schema import normalize_db_url


def test_cli_pause_resume_and_full_postgres_persistence(tmp_path, monkeypatch):
    """End-to-end persistence integration test:
    1. Start CLI run from JSON intake -> pause at first HITL interrupt (--pause-after-intake)
    2. Resume via PostgresSaver (--resume <thread_id>) with auto-approve -> export MD/JSON
    3. Verify PostgresSaver checkpoints + domain audit tables without warnings
    """
    monkeypatch.setenv("COVENANT_DISABLE_LLM", "1")
    db_url = normalize_db_url()
    thread_id = f"cli-e2e-{uuid4().hex[:10]}"
    intake_file = tmp_path / "intake.json"
    out_md = tmp_path / "report.md"
    out_json = tmp_path / "result.json"

    intake_file.write_text(
        json.dumps(
            {
                "company_name": "Apex Industrial Controls",
                "business_description": "Precision valves and industrial pumps.",
                "industry": "industrial_machinery",
                "geography": "US",
                "revenue": "32000000",
                "ebitda": "4800000",
                "ebitda_period": "LTM",
                "revenue_model": "OEM + aftermarket parts",
                "customer_concentration": "18%",
                "revenue_growth": "14%",
                "capex": "900000",
                "total_debt": "6000000",
                "cash": "1500000",
            }
        ),
        encoding="utf-8",
    )

    run_id = None
    case_id = None
    try:
        rc1 = run_cli(["--input", str(intake_file), "--thread-id", thread_id, "--pause-after-intake"])
        assert rc1 == 0

        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, case_id, status FROM analysis_runs WHERE thread_id = %s;", (thread_id,))
                row = cur.fetchone()
                assert row is not None
                run_id, case_id, status = row
                assert status == "running"

        rc2 = run_cli([
            "--resume",
            thread_id,
            "--auto-approve",
            "--export-md",
            str(out_md),
            "--export-json",
            str(out_json),
        ])
        assert rc2 == 0
        assert out_md.exists() and "Apex Industrial Controls" in out_md.read_text(encoding="utf-8")
        assert "| Peer Ticker | LTM End |" in out_md.read_text(encoding="utf-8")
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["valuation"] is not None
        assert payload["valuation"]["ebitda_period"] == "LTM"

        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM analysis_runs WHERE id = %s;", (str(run_id),))
                assert cur.fetchone()[0] == "completed"
                cur.execute("SELECT COUNT(*) FROM peer_candidates WHERE run_id = %s;", (str(run_id),))
                assert cur.fetchone()[0] >= 5
                cur.execute("SELECT COUNT(*) FROM financial_datapoints WHERE run_id = %s;", (str(run_id),))
                assert cur.fetchone()[0] >= 35
                cur.execute("SELECT COUNT(*) FROM valuation_results WHERE run_id = %s;", (str(run_id),))
                assert cur.fetchone()[0] == 1
                cur.execute("SELECT COUNT(*) FROM audit_logs WHERE entity_id = %s;", (str(run_id),))
                assert cur.fetchone()[0] >= 4
    finally:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s;", (thread_id,))
                cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s;", (thread_id,))
                cur.execute("DELETE FROM checkpoints WHERE thread_id = %s;", (thread_id,))
                if run_id is not None:
                    cur.execute("DELETE FROM audit_logs WHERE entity_id = %s;", (str(run_id),))
                if case_id is not None:
                    cur.execute("DELETE FROM audit_logs WHERE detail->>'case_id' = %s;", (str(case_id),))
                    cur.execute("DELETE FROM cases WHERE id = %s;", (str(case_id),))
            conn.commit()


def test_cli_does_not_audit_log_rejected_hitl_retries(tmp_path, monkeypatch):
    """Rejected HITL validation retries must NOT record false confirmation audit rows."""
    monkeypatch.setenv("COVENANT_DISABLE_LLM", "1")
    db_url = normalize_db_url()
    thread_id = f"cli-hitl-retry-{uuid4().hex[:10]}"
    intake_file = tmp_path / "intake.json"
    intake_file.write_text(
        json.dumps(
            {
                "company_name": "ValveCo Retry Test",
                "business_description": "Industrial control valves.",
                "industry": "industrial_machinery",
                "geography": "US",
                "revenue": "32000000",
                "ebitda": "4800000",
                "ebitda_period": "LTM",
            }
        ),
        encoding="utf-8",
    )

    responses = iter([
        "not_a_real_industry",  # rejected by confirm_industry
        "",                     # accept proposed industrial_machinery
        "AIT",                  # rejected by peer_review (< 3 peers)
        "",                     # accept proposed top peers
    ])
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: next(responses))

    run_id = None
    case_id = None
    try:
        rc = run_cli(["--input", str(intake_file), "--thread-id", thread_id])
        assert rc == 0

        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, case_id FROM analysis_runs WHERE thread_id = %s;", (thread_id,))
                row = cur.fetchone()
                assert row is not None
                run_id, case_id = row

                cur.execute(
                    "SELECT action, detail FROM audit_logs WHERE entity_id = %s ORDER BY id;",
                    (str(run_id),),
                )
                logs = cur.fetchall()
                ind_logs = [d for a, d in logs if a == "industry_confirmed"]
                peer_logs = [d for a, d in logs if a == "peers_reviewed"]
                assert ind_logs == [{"decision": "industrial_machinery"}]
                assert len(peer_logs) == 1
                assert len(peer_logs[0]["decision"]) >= 3
    finally:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s;", (thread_id,))
                cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s;", (thread_id,))
                cur.execute("DELETE FROM checkpoints WHERE thread_id = %s;", (thread_id,))
                if run_id is not None:
                    cur.execute("DELETE FROM audit_logs WHERE entity_id = %s;", (str(run_id),))
                if case_id is not None:
                    cur.execute("DELETE FROM audit_logs WHERE detail->>'case_id' = %s;", (str(case_id),))
                    cur.execute("DELETE FROM cases WHERE id = %s;", (str(case_id),))
            conn.commit()
