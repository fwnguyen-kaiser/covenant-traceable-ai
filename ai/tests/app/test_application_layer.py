"""Tests for P1 Application Service Layer & CLI caller parity."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from covenant_ai.app.cases import CaseService
from covenant_ai.app.errors import DuplicateThreadError, VersionConflictError
from covenant_ai.app.exports import render_json_report, render_markdown_report
from covenant_ai.app.reviews import ReviewService
from covenant_ai.app.runs import ValuationRunnerService
from covenant_ai.cli import run_cli
from covenant_ai.providers.postgres_repo import PostgresCaseRepository


def _cleanup_threads_and_cases(db_url: str, threads: list[str], case_ids: list[str]) -> None:
    with psycopg.connect(db_url) as conn:
        for tid in threads:
            conn.execute("DELETE FROM checkpoint_writes WHERE thread_id=%s", (tid,))
            conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id=%s", (tid,))
            conn.execute("DELETE FROM checkpoints WHERE thread_id=%s", (tid,))
        for cid in case_ids:
            conn.execute(
                "DELETE FROM audit_logs WHERE entity_id=%s OR detail->>'case_id'=%s",
                (cid, cid),
            )
            conn.execute("DELETE FROM cases WHERE id=%s", (cid,))
        conn.commit()


def test_app_runner_and_cli_parity_plus_second_run_isolation(tmp_path):
    """P1 verification:
    - CLI flow and ValuationRunnerService produce identical EV/equity, sources, and digest inputs.
    - Starting Run 2 after adding profile v2 on the SAME case produces 2 runs on 1 case
      without altering Run 1's profile_id or frozen snapshot.
    - Calling finish_analysis_run twice is strictly idempotent.
    """
    repo = PostgresCaseRepository()
    cases = CaseService(repo)
    runner = ValuationRunnerService(db_url=repo.db_url, repo=repo)
    reviews = ReviewService(repo)

    answers_v1 = {
        "company_name": "Parity Controls Inc.",
        "business_description": "Precision valves and industrial pumps.",
        "industry": "industrial_machinery",
        "geography": "US",
        "ebitda_period": "FY",
        "revenue": "32000000",
        "ebitda": "4800000",
        "total_debt": "6000000",
        "cash": "1500000",
    }

    cli_tid = f"p1-cli-{uuid4().hex[:8]}"
    app_tid1 = f"p1-app1-{uuid4().hex[:8]}"
    app_tid2 = f"p1-app2-{uuid4().hex[:8]}"
    created_case_ids: list[str] = []

    try:
        # 1. Run via CLI
        intake_file = tmp_path / "intake.json"
        out_json = tmp_path / "cli_out.json"
        intake_file.write_text(json.dumps(answers_v1), encoding="utf-8")
        assert run_cli(
            [
                "--input",
                str(intake_file),
                "--thread-id",
                cli_tid,
                "--auto-approve",
                "--export-json",
                str(out_json),
            ]
        ) == 0
        cli_packet = json.loads(out_json.read_text(encoding="utf-8"))
        created_case_ids.append(cli_packet["case_id"])

        # 2. Run 1 via Application Service on pre-created case + v1 profile
        case_rec = cases.create_case("Parity Controls Inc.", intake_answers=answers_v1)
        created_case_ids.append(str(case_rec.case_id))
        assert case_rec.latest_profile is not None
        assert case_rec.latest_profile.version == 1

        step = runner.start_run(
            intake_answers=answers_v1,
            thread_id=app_tid1,
            case_id=case_rec.case_id,
            profile_id=case_rec.latest_profile.profile_id,
        )
        while not step.terminal:
            assert step.pending_prompt is not None
            action = step.pending_prompt.action
            if action == "confirm_industry":
                dec = step.pending_prompt.payload["proposed"]
            elif action == "peer_review":
                dec = step.pending_prompt.payload["selected_peers"]
            else:
                dec = {}
            step = runner.submit_hitl_decision(app_tid1, dec)

        assert step.terminal and step.packet is not None
        assert step.case_id == case_rec.case_id
        app_pkt1 = step.packet

        # Parity checks: exact same valuation, sources count, status, and decisions count
        assert app_pkt1["valuation"] == cli_packet["valuation"]
        assert len(app_pkt1["sources"]) == len(cli_packet["sources"])
        assert len(app_pkt1["review_decisions"]) == len(cli_packet["review_decisions"])
        assert app_pkt1["release_status"] == cli_packet["release_status"] == "draft"
        assert "Parity Controls Inc." in render_markdown_report(app_pkt1)
        assert json.loads(render_json_report(app_pkt1))["digest"] == app_pkt1["digest"]

        # Duplicate finalize call adds zero datapoints
        before_dp = psycopg.connect(repo.db_url).execute(
            "SELECT count(*) FROM financial_datapoints WHERE run_id=%s", (str(step.run_id),)
        ).fetchone()[0]
        repo.finish_analysis_run(step.run_id, step.state)
        after_dp = psycopg.connect(repo.db_url).execute(
            "SELECT count(*) FROM financial_datapoints WHERE run_id=%s", (str(step.run_id),)
        ).fetchone()[0]
        assert before_dp == after_dp

        # 3. Add profile v2 with stale-version guard and start Run 2 on the SAME case
        with pytest.raises(VersionConflictError) as vc:
            cases.add_profile_version(case_rec.case_id, answers_v1, expected_version=99)
        assert vc.value.latest_version == 1

        answers_v2 = {**answers_v1, "ebitda": "5200000", "cash": "2000000"}
        v2 = cases.add_profile_version(case_rec.case_id, answers_v2, expected_version=1)
        assert v2.version == 2

        step2 = runner.start_run(
            intake_answers=answers_v2,
            thread_id=app_tid2,
            case_id=case_rec.case_id,
            profile_id=v2.profile_id,
        )
        while not step2.terminal:
            assert step2.pending_prompt is not None
            action = step2.pending_prompt.action
            dec = (
                step2.pending_prompt.payload["proposed"]
                if action == "confirm_industry"
                else step2.pending_prompt.payload["selected_peers"]
            )
            step2 = runner.submit_hitl_decision(app_tid2, dec)

        assert step2.terminal and step2.packet is not None
        assert Decimal(step2.packet["valuation"]["ev_mid"]) > Decimal(app_pkt1["valuation"]["ev_mid"])

        runs = cases.list_runs(case_rec.case_id)
        assert len(runs) == 2
        assert {r["profile_version"] for r in runs} == {1, 2}

        # Run 1's frozen packet and digest remain completely unchanged
        assert reviews.get_frozen_packet(app_tid1) == app_pkt1
    finally:
        _cleanup_threads_and_cases(repo.db_url, [cli_tid, app_tid1, app_tid2], created_case_ids)


def test_parallel_start_same_thread_has_deterministic_conflict():
    """Two concurrent writers targeting the same thread_id allow exactly one start."""
    repo = PostgresCaseRepository()
    runner = ValuationRunnerService(db_url=repo.db_url, repo=repo)
    tid = f"p1-race-{uuid4().hex[:8]}"
    answers = {
        "company_name": "Race Guard Co",
        "business_description": "Industrial machinery manufacturer.",
        "industry": "industrial_machinery",
        "geography": "US",
        "ebitda_period": "FY",
        "revenue": "20000000",
        "ebitda": "3000000",
    }

    def attempt():
        try:
            res = runner.start_run(intake_answers=answers, thread_id=tid)
            return ("ok", str(res.case_id))
        except (DuplicateThreadError, ValueError) as exc:
            return ("blocked", type(exc).__name__)

    case_ids: list[str] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(attempt)
            f2 = pool.submit(attempt)
            outcomes = [f1.result(), f2.result()]
        statuses = sorted(o[0] for o in outcomes)
        assert statuses == ["blocked", "ok"], f"Unexpected parallel race result: {outcomes}"
        for kind, val in outcomes:
            if kind == "ok" and val != "None":
                case_ids.append(val)
    finally:
        row = repo.get_run_by_thread_id(tid)
        if row and str(row[1]) not in case_ids:
            case_ids.append(str(row[1]))
        _cleanup_threads_and_cases(repo.db_url, [tid], case_ids)
