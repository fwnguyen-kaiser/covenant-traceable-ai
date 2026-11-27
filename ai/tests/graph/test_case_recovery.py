import json
from uuid import uuid4

import psycopg
import pytest

from covenant_ai.cli import run_cli
from covenant_ai.graph import workflow
from covenant_ai.domain.bridge import build_peer_financials
from covenant_ai.domain.models import FinancialDatapoint, CompanyProfile
from covenant_ai.graph.nodes import valuation_node
from covenant_ai.graph.checkpointer import open_postgres_checkpointer
from covenant_ai.providers.fake import FakeProvider
from covenant_ai.providers.postgres_repo import PostgresCaseRepository


def test_pending_task_recovery_frozen_approval_and_snapshot_replay(tmp_path, monkeypatch):
    tid = f"recovery-{uuid4().hex}"
    repo = PostgresCaseRepository()
    source = tmp_path / "intake.json"
    source.write_text(json.dumps(dict(company_name="Recovery Co", business_description="Precision machinery",
        industry="industrial_machinery", geography="US", revenue="20000000", ebitda="3000000",
        ebitda_period="FY", total_debt="0")), encoding="utf-8")
    original = workflow.make_self_audit_node
    def fail_once(_):
        def crash(state):
            raise RuntimeError("simulated interruption before audit")
        return crash
    try:
        with repo.thread_lock(tid), pytest.raises(ValueError, match="Another session"):
            with repo.thread_lock(tid):
                pytest.fail("Second writer acquired lock")
        monkeypatch.setattr(workflow, "make_self_audit_node", fail_once)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            run_cli(["--input", str(source), "--thread-id", tid, "--auto-approve"])
        run_id, case_id, status = repo.get_run_by_thread_id(tid)
        assert status == "running"
        assert run_cli(["--input", str(source), "--thread-id", tid]) == 1

        monkeypatch.setattr(workflow, "make_self_audit_node", original)
        assert run_cli(["--resume", tid, "--auto-approve"]) == 0
        report = repo.review_report(tid)
        assert report["release_status"] == "draft" and report["audit_verdict"] == "pass"
        assert report["valuation"]["equity_mid"] is None
        assert set(report["lineage"]["ev_mid"]["peer_snapshot_ids"]) == set(report["valuation"]["multiples_used"])
        with psycopg.connect(repo.db_url) as db:
            ids = db.execute("SELECT id FROM financial_datapoints WHERE run_id=%s", (run_id,)).fetchall()
            assert {str(r[0]) for r in ids} == {r["snapshot_id"] for r in report["sources"]}
        frozen = [FinancialDatapoint.model_validate(row) for row in report["sources"]]
        peers = [build_peer_financials(frozen, t, report["valuation"]["ebitda_period"])[0]
                 for t in report["valuation"]["multiples_used"]]
        rebuilt = valuation_node({"company_profile": CompanyProfile.model_validate(report["company_profile"]),
                                  "peer_financials": peers, "failed_peers": report["failed_peers"]})
        assert rebuilt["valuation"] == report["valuation"]
        repo.finish_analysis_run(run_id, {"insufficient_peers": True})
        assert repo.review_report(tid) == report
        with pytest.raises(ValueError):
            repo.review_report(tid, "wrong digest")
        monkeypatch.setattr("covenant_ai.providers.postgres_repo.authorized_windows_reviewer", lambda: "demo-sid")
        approved = repo.review_report(tid, report["digest"])
        assert approved["release_status"] == "approved" and approved["approved_by"] == "demo-sid"
        assert approved == repo.review_report(tid, report["digest"])
    finally:
        row = repo.get_run_by_thread_id(tid)
        if row:
            with psycopg.connect(repo.db_url) as db:
                db.execute("DELETE FROM cases WHERE id=%s", (row[1],))

def test_legacy_unknown_zeros_ask_once_and_preserve_explicit_zero(tmp_path, monkeypatch):
    tid = f"legacy-{uuid4().hex}"
    repo = PostgresCaseRepository()
    cfg = {"configurable": {"thread_id": tid}}
    answers = dict(company_name="Legacy", business_description="Machines", industry="industrial_machinery",
                   geography="US", revenue="20000000", ebitda="3000000", ebitda_period="FY", cash="0")
    with open_postgres_checkpointer(repo.db_url) as saver:
        app = workflow.create_valuation_graph(FakeProvider({}), checkpointer=saver)
        state = app.invoke({"intake_answers": answers}, cfg)
        profile = state["company_profile"].model_copy(update={"total_debt": state["company_profile"].cash})
        app.update_state(cfg, {"company_profile": profile, "integrity_version": 0}, as_node="intake")
    try:
        assert run_cli(["--resume", tid, "--auto-approve"]) == 0
        prompts = []
        def reply(prompt):
            prompts.append(prompt)
            return ""
        monkeypatch.setattr("builtins.input", reply)
        assert run_cli(["--resume", tid]) == 0
        packet = repo.review_report(tid)
        assert packet["company_profile"]["total_debt"] is None
        assert packet["company_profile"]["cash"] == "0"
        assert packet["valuation"]["equity_mid"] is None
        assert sum("Confirm target" in p for p in prompts) == 1
        assert {p["metric"] for p in packet["sources"] if p["peer_ticker"] is None} >= {"cash", "revenue"}
    finally:
        row = repo.get_run_by_thread_id(tid)
        if row:
            with psycopg.connect(repo.db_url) as db:
                db.execute("DELETE FROM cases WHERE id=%s", (row[1],))
