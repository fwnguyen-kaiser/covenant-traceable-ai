"""Interactive CLI runner for Covenant AI valuation workflows.

Supports:
  - Creating new cases from JSON intake (--input) or interactive QUESTION_BANK Q&A
  - Durable pause / resume across HITL checkpoints via PostgresSaver (--resume <thread_id>)
  - Full audit persistence via PostgresCaseRepository (cases, profiles, runs, peer_candidates,
    frozen ledger datapoints, valuation_results, audit_logs)
  - Exporting finished Markdown reports (--export-md) and Valuation JSON (--export-json)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from covenant_ai.domain.intake import create_company_profile_from_intake
from covenant_ai.domain.intake_questions import Stage, questions
from covenant_ai.domain.models import ExplainMode
from covenant_ai.graph.checkpointer import open_postgres_checkpointer
from covenant_ai.graph.workflow import create_valuation_graph
from covenant_ai.llm.client import LLMClient, build_default_llm_client
from covenant_ai.providers.db_schema import normalize_db_url
from covenant_ai.providers.postgres import PostgresMarketDataProvider
from covenant_ai.providers.postgres_repo import PostgresCaseRepository


class PauseRequested(Exception):
    """Raised cleanly when user pauses an HITL step to resume later via --resume."""


def collect_interactive_intake() -> dict[str, Any]:
    """Prompt user interactively using the QUESTION_BANK MVP questions."""
    answers: dict[str, Any] = {}
    print("\n=== Covenant AI — Target Company Intake ===\n")
    for q in questions(Stage.MVP):
        if q.choices:
            print(f"Choices: {', '.join(q.choices)}")
        default_hint = f" [default: {q.default}]" if q.default else (" [required]" if q.required else " [optional, Enter to skip]")
        while True:
            raw = input(f"{q.prompt}{default_hint}\n> ").strip()
            if not raw and q.default is not None:
                raw = str(q.default)
            if not raw and q.required:
                print("This field is required.")
                continue
            if raw:
                answers[q.id] = raw
            break
    return answers


def _prompt_hitl_decision(payload: dict[str, Any], auto_approve: bool) -> Any:
    action = payload.get("action")
    if action == "intake_clarify":
        if auto_approve:
            raise PauseRequested()
        print("\n--- Intake Clarification Needed ---")
        if payload.get("error"):
            print(f"Validation error: {payload['error']}")
        draft = payload.get("extracted_draft") or {}
        if draft:
            print(f"Extracted so far: {json.dumps(draft)}")
        for q in payload.get("questions") or []:
            print(f"- {q}")
        resp = input("Enter clarification details (or type 'pause'): ").strip()
        if resp.lower() == "pause":
            raise PauseRequested()
        return resp

    if action == "confirm_industry":
        proposed = payload.get("proposed")
        if auto_approve:
            return proposed
        print("\n--- HITL Checkpoint 1: Confirm Industry ---")
        if payload.get("error"):
            print(f"Validation error: {payload['error']}")
        print(f"Proposed industry: {proposed}")
        if payload.get("sub_industry") or payload.get("customer_type"):
            print(
                f"Sub-industry / Customer type: {payload.get('sub_industry') or 'none'} / "
                f"{payload.get('customer_type') or 'unspecified'}"
            )
        eb_sum = payload.get("ebitda_summary")
        if eb_sum:
            yr = f" ({eb_sum['target_fiscal_year']})" if eb_sum.get("target_fiscal_year") else ""
            print(
                f"EBITDA Summary ({eb_sum.get('ebitda_period')}{yr}): "
                f"Normalized=${eb_sum.get('normalized_ebitda')} "
                f"(Reported={eb_sum.get('reported_ebitda')}, Net Adj={eb_sum.get('total_adjustments')})"
            )
        prov = payload.get("provenance") or {}
        if prov:
            print("Inferred field provenance:")
            for k, v in prov.items():
                q_tag = "verified" if v.get("quote_verified") else "UNVERIFIED"
                print(
                    f"  - {k}={v.get('value')} (conf={v.get('confidence')}, {q_tag}, "
                    f"quote={v.get('evidence_quote')!r})"
                )
        for inc in payload.get("inconsistencies") or []:
            print(f"  [Inconsistency Note] {inc}")
        print(f"Allowed options : {', '.join(payload.get('allowed', []))}")
        resp = input("Press Enter to confirm, type an industry slug, or type 'pause': ").strip()
        if resp.lower() == "pause":
            raise PauseRequested()
        return resp or proposed

    if action == "peer_review":
        selected = payload.get("selected_peers") or []
        if auto_approve:
            return selected
        print("\n--- HITL Checkpoint 2: Peer Review ---")
        if payload.get("error"):
            print(f"Validation error: {payload['error']}")
        peer_scores = payload.get("peer_scores") or []
        if peer_scores:
            print(f"{'Rank':<5} {'Ticker':<7} {'Score':<8} {'Tier':<11} Rationale")
            for row in peer_scores:
                print(
                    f"#{row.get('rank_order', '-'):<4} {row.get('ticker', ''):<7} "
                    f"{str(row.get('total_score', '')):<8} {str(row.get('ev_tier') or ''):<11} "
                    f"{row.get('rationale', '')}"
                )
        else:
            print(f"Proposed peers ({len(selected)}): {', '.join(selected)}")
        resp = input(
            f"Press Enter to approve [{', '.join(selected)}], enter comma-separated tickers "
            f"(min {payload.get('min_required', 3)}), or type 'pause': "
        ).strip()
        if resp.lower() == "pause":
            raise PauseRequested()
        return [s.strip().upper() for s in resp.split(",") if s.strip()] if resp else selected

    if auto_approve:
        return None
    resp = input(f"HITL prompt ({action}): ").strip()
    return resp


def _run_hitl_step(
    app: Any,
    config: dict[str, Any],
    payload: dict[str, Any],
    *,
    auto_approve: bool,
    thread_id: str,
    repo: PostgresCaseRepository,
    run_id: Any,
) -> dict[str, Any] | None:
    """Prompt for one HITL checkpoint, resume graph, and audit-log accepted inputs."""
    try:
        decision = _prompt_hitl_decision(payload, auto_approve)
    except PauseRequested:
        print(f"Paused cleanly. Resume anytime with: --resume {thread_id}")
        return None

    action = payload.get("action")
    next_state = app.invoke(Command(resume=decision), config=config)
    next_interrupts = next_state.get("__interrupt__") or []
    next_payload = next_interrupts[0].value if next_interrupts else None
    rejected = bool(
        isinstance(next_payload, dict)
        and next_payload.get("action") == action
        and next_payload.get("error")
    )
    if not rejected and run_id is not None:
        if action == "confirm_industry":
            repo.append_audit_log("industry_confirmed", "analysis_runs", run_id, {"decision": decision})
        elif action == "peer_review":
            repo.append_audit_log("peers_reviewed", "analysis_runs", run_id, {"decision": decision})
    return next_state


def run_cli(
    argv: list[str] | None = None,
    *,
    llm_client: LLMClient | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Covenant AI Valuation CLI Runner")
    parser.add_argument("--input", type=Path, help="Path to JSON file containing intake answers")
    parser.add_argument("--prompt", help="Natural-language company profile prompt (English USD)")
    parser.add_argument(
        "--explain-mode",
        choices=[m.value for m in ExplainMode],
        help="Override report narrative mode (llm_hybrid or deterministic)",
    )
    parser.add_argument("--resume", metavar="THREAD_ID", help="Resume an interrupted run from PostgreSQL")
    parser.add_argument("--thread-id", help="Explicit thread_id for a new run")
    parser.add_argument("--db-url", help="PostgreSQL connection URI override")
    parser.add_argument("--auto-approve", action="store_true", help="Auto-confirm proposed industry and top peers")
    parser.add_argument("--pause-after-intake", action="store_true", help="Run intake, persist checkpoint, and exit at first HITL pause")
    parser.add_argument("--export-md", type=Path, help="Write Markdown explanation report to file")
    parser.add_argument("--export-json", type=Path, help="Write final valuation JSON payload to file")
    args = parser.parse_args(argv)

    db_url = normalize_db_url(args.db_url)
    repo = PostgresCaseRepository(db_url=db_url)
    provider = PostgresMarketDataProvider(db_url=db_url, allow_on_demand_yahoo=True)
    active_llm = llm_client if llm_client is not None else build_default_llm_client()
    default_mode = args.explain_mode or (
        ExplainMode.LLM_HYBRID.value if active_llm is not None else ExplainMode.DETERMINISTIC.value
    )

    run_id = None
    case_id = None
    already_completed = False
    synced_profile_sig: tuple[Any, ...] | None = None

    def _profile_sig(prof: Any) -> tuple[Any, ...] | None:
        if prof is None:
            return None
        sub = prof.business_profile.sub_industry if getattr(prof, "business_profile", None) else None
        return (prof.industry, str(prof.ebitda), sub)

    def _ensure_or_sync_case_and_run(st: dict[str, Any], tid: str) -> None:
        nonlocal case_id, run_id, synced_profile_sig
        prof = st.get("company_profile")
        if prof is None:
            return
        sig = _profile_sig(prof)
        if run_id is None:
            case_id, profile_id = repo.create_case_and_profile(prof, st.get("intake_answers"))
            run_id = repo.start_analysis_run(case_id, profile_id, thread_id=tid)
            synced_profile_sig = sig
        elif synced_profile_sig is not None and sig != synced_profile_sig and case_id is not None:
            case_id, new_profile_id = repo.create_case_and_profile(
                prof, st.get("intake_answers"), case_id=case_id
            )
            repo.update_run_profile(run_id, new_profile_id)
            synced_profile_sig = sig

    with open_postgres_checkpointer(db_url) as checkpointer:
        app = create_valuation_graph(
            provider,
            checkpointer=checkpointer,
            llm_client=active_llm,
            default_explain_mode=default_mode,
        )

        if args.resume:
            thread_id = args.resume
            config = {"configurable": {"thread_id": thread_id}}
            run_row = repo.get_run_by_thread_id(thread_id)
            if run_row is not None:
                run_id, case_id, existing_status = run_row
                already_completed = existing_status == "completed"
            snap = app.get_state(config)
            if not snap or not snap.values:
                print(f"No checkpoint found for thread_id={thread_id!r}", file=sys.stderr)
                return 1
            state = dict(snap.values)
            if run_id is not None:
                synced_profile_sig = _profile_sig(state.get("company_profile"))
            pending_interrupts = [
                intr.value
                for task in (snap.tasks or ())
                for intr in getattr(task, "interrupts", ())
            ]
            if pending_interrupts:
                already_completed = False
                state = _run_hitl_step(
                    app,
                    config,
                    pending_interrupts[0],
                    auto_approve=args.auto_approve,
                    thread_id=thread_id,
                    repo=repo,
                    run_id=run_id,
                )
                if state is None:
                    return 0
                _ensure_or_sync_case_and_run(state, thread_id)
        elif args.prompt:
            if active_llm is None:
                print(
                    "Natural-language --prompt mode requires GEMINI_API_KEY or an injected LLMClient.",
                    file=sys.stderr,
                )
                return 1
            thread_id = args.thread_id or f"run-{uuid4().hex[:12]}"
            config = {"configurable": {"thread_id": thread_id}}
            init_state: dict[str, Any] = {"raw_input": args.prompt}
            if args.explain_mode:
                init_state["explain_mode"] = args.explain_mode
            state = app.invoke(init_state, config=config)
            _ensure_or_sync_case_and_run(state, thread_id)
            if run_id is not None:
                print(f"Started case={case_id} run={run_id} thread_id={thread_id}")
            if args.pause_after_intake and "__interrupt__" in state:
                print(f"Paused cleanly after intake. Resume with: --resume {thread_id}")
                return 0
        else:
            answers = (
                json.loads(args.input.read_text(encoding="utf-8"))
                if args.input
                else collect_interactive_intake()
            )
            profile, _ = create_company_profile_from_intake(answers)
            case_id, profile_id = repo.create_case_and_profile(profile, answers)
            synced_profile_sig = _profile_sig(profile)
            thread_id = args.thread_id or f"run-{uuid4().hex[:12]}"
            run_id = repo.start_analysis_run(case_id, profile_id, thread_id=thread_id)
            config = {"configurable": {"thread_id": thread_id}}
            print(f"Started case={case_id} run={run_id} thread_id={thread_id}")
            init_state = {"intake_answers": answers}
            if args.explain_mode:
                init_state["explain_mode"] = args.explain_mode
            state = app.invoke(init_state, config=config)
            if args.pause_after_intake and "__interrupt__" in state:
                print(f"Paused cleanly after intake. Resume with: --resume {thread_id}")
                return 0

        while "__interrupt__" in state:
            already_completed = False
            payload = state["__interrupt__"][0].value
            state = _run_hitl_step(
                app,
                config,
                payload,
                auto_approve=args.auto_approve,
                thread_id=thread_id,
                repo=repo,
                run_id=run_id,
            )
            if state is None:
                return 0
            _ensure_or_sync_case_and_run(state, thread_id)

    if run_id is not None and not already_completed:
        repo.finish_analysis_run(run_id, state)

    explanation = state.get("explanation") or "Workflow completed without explanation."
    if args.export_md:
        args.export_md.write_text(explanation, encoding="utf-8")
        print(f"Wrote Markdown report to {args.export_md}")
    if args.export_json:
        args.export_json.write_text(
            json.dumps(
                {
                    "thread_id": thread_id,
                    "run_id": str(run_id) if run_id else None,
                    "case_id": str(case_id) if case_id else None,
                    "valuation": state.get("valuation"),
                    "peer_screening_results": state.get("peer_screening_results"),
                    "failed_peers": state.get("failed_peers"),
                    "audit_verdict": state.get("audit_verdict"),
                    "audit_findings": state.get("audit_findings"),
                    "audit_fallback_used": state.get("audit_fallback_used"),
                    "inconsistencies": state.get("inconsistencies"),
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"Wrote JSON output to {args.export_json}")

    print("\n" + explanation)
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
