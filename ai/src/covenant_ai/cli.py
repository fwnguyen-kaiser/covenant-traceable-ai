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
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.types import Command, Overwrite
from psycopg.conninfo import conninfo_to_dict

from covenant_ai.domain.intake_questions import Stage, questions
from covenant_ai.domain.models import ExplainMode, FinancialDatapoint, SourceType
from datetime import UTC, datetime
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
        if q.id == "one_off_items":
            items = []
            while description := input("One-off item description (Enter to finish): ").strip():
                kind = input("Does this remove income or add back a cost? (income/cost): ").strip().lower()
                if kind not in {"income", "cost"}:
                    print("Enter income or cost. Please re-enter this item.")
                    continue
                from covenant_ai.domain.amounts import parse_amount
                try:
                    amount = parse_amount(input("Amount in USD, as a positive magnitude: "))
                    if amount < 0:
                        raise ValueError
                except ValueError:
                    print("Invalid USD amount. Please re-enter this item.")
                    continue
                items.append({"category": "non_recurring_income" if kind == "income" else "non_recurring_charges",
                              "description": description, "amount": str(amount)})
            if items:
                answers["adjustment_items"] = items
            continue
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
        if payload.get("structured"):
            print("Enter corrected fields as a JSON object, or the value of the single missing field.")
        resp = input("Enter clarification details (or type 'pause'): ").strip()
        if resp.lower() == "pause":
            raise PauseRequested()
        if payload.get("structured"):
            missing = payload.get("missing_fields") or []
            if len(missing) == 1 and not resp.startswith("{"):
                return {missing[0]: resp}
            try:
                return json.loads(resp, parse_float=Decimal)
            except ValueError:
                return resp
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
        tickers = [s.strip().upper() for s in resp.split(",") if s.strip()] if resp else selected
        outside = set(tickers) - set(payload.get("candidate_tickers", tickers))
        if len(tickers) >= payload.get("min_required", 3) and outside:
            reasons = {t: input(f"Why include {t} outside the industry pool? ").strip() for t in sorted(outside)}
            return {"approved_peers": tickers, "rationale": reasons}
        return tickers

    if auto_approve:
        return None
    resp = input(f"HITL prompt ({action}): ").strip()
    return resp


def _export_packet(packet: dict, markdown: Path | None, json_path: Path | None) -> None:
    status = packet["release_status"].upper()
    banner = f"**{status}** · {packet['method_version']} · SHA-256 `{packet['digest']}`"
    report = f"{banner}\n\n{packet['explanation']}"
    if markdown:
        markdown.write_text(report, encoding="utf-8")
    if json_path:
        json_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Thread {packet['thread_id']} · {status}")
    print(report)


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
    parser.add_argument("--review-report", metavar="THREAD_ID", help="Open the frozen report and its approval digest")
    parser.add_argument("--approve-digest", metavar="SHA256", help="Confirm the frozen report as the enrolled Windows reviewer")
    args = parser.parse_args(argv)
    if args.approve_digest and not args.review_report:
        parser.error("--approve-digest requires --review-report")
    if args.review_report and any((args.input, args.prompt, args.resume, args.thread_id, args.auto_approve)):
        parser.error("Review one frozen report separately from starting or resuming a run.")

    if args.resume and (args.input or args.prompt or args.thread_id):
        parser.error("A resume cannot also start a new case.")
    if args.input and args.prompt:
        parser.error("Choose one intake source.")
    db_url = normalize_db_url(args.db_url)
    connection = conninfo_to_dict(db_url)
    addresses = [connection.get("host"), connection.get("hostaddr")]
    if connection.get("service") or not any(addresses) or any(
        addr and addr.lower() not in {"localhost", "127.0.0.1", "::1"} for addr in addresses
    ):
        parser.error("Private case data requires a PostgreSQL server on this machine.")
    repo = PostgresCaseRepository(db_url=db_url)
    if args.review_report:
        packet = repo.review_report(args.review_report, args.approve_digest)
        _export_packet(packet, args.export_md, args.export_json)
        return 0
    provider = PostgresMarketDataProvider(db_url=db_url, allow_on_demand_yahoo=True)
    active_llm = llm_client if llm_client is not None else build_default_llm_client()
    default_mode = args.explain_mode or (
        ExplainMode.LLM_HYBRID.value if active_llm is not None else ExplainMode.DETERMINISTIC.value
    )

    thread_id = args.resume or args.thread_id or f"run-{uuid4().hex}"
    config = {"configurable": {"thread_id": thread_id}}
    run_id = case_id = None
    with repo.thread_lock(thread_id), open_postgres_checkpointer(db_url) as checkpointer:
        app = create_valuation_graph(provider, checkpointer=checkpointer,
                                     llm_client=active_llm, default_explain_mode=default_mode)
        snap = app.get_state(config)
        run_row = repo.get_run_by_thread_id(thread_id)
        if args.resume:
            if not snap.created_at:
                print(f"No checkpoint found for thread_id={thread_id!r}", file=sys.stderr)
                return 1
            if run_row:
                run_id, case_id, status = run_row
            terminal = bool(run_row and status != "running")
            state = dict(snap.values)
            if terminal and snap.next:
                raise ValueError("Historical run and checkpoint disagree on completion. Manual review required.")
            if not terminal and state.get("company_profile") and not state.get("integrity_version"):
                profile = state["company_profile"]
                answers = dict(state.get("intake_answers") or {})
                updates = {}
                for field in ("total_debt", "cash"):
                    if getattr(profile, field) == 0 and answers.get(field) in (None, ""):
                        if args.auto_approve:
                            print(f"Legacy {field} needs confirmation. Resume interactively; Enter keeps EV only.")
                            return 0
                        while True:
                            raw = input(f"Confirm target {field} in USD (0 if zero, Enter if unknown): ").strip()
                            try:
                                amount = Decimal(raw) if raw else None
                                if amount is not None and (not amount.is_finite() or amount < 0):
                                    raise ValueError
                                break
                            except ArithmeticError:
                                print("Enter a nonnegative USD amount.")
                            except ValueError:
                                print("Enter a nonnegative USD amount.")
                        updates[field] = amount
                        answers[field] = str(amount) if amount is not None else None
                profile = profile.model_copy(update=updates)
                # Old ranking may depend on an invented zero. Re-run screening and peer review.
                target_points = [d for d in state.get("ledger", [])
                                 if not d.peer_ticker and d.metric not in {"cash", "total_debt", "revenue"}]
                for metric in ("cash", "total_debt", "revenue"):
                    amount = getattr(profile, metric)
                    if amount is not None:
                        target_points.append(FinancialDatapoint(
                            metric=metric, value=amount, period=profile.ebitda_period, period_end=profile.period_end_date,
                            source_type=SourceType.USER_PROVIDED, source_ref=f"Target intake field {metric}",
                            retrieved_at=datetime.now(UTC),
                        ))
                change = {"company_profile": profile, "intake_answers": answers, "integrity_version": 2,
                          "ledger": Overwrite(target_points),
                          "failed_peers": {}, "disqualified_peers": [], "valuation": {}, "retry_count": 0,
                          "peer_review_confirmed": False, "insufficient_peers": False}
                app.update_state(config, change, as_node="confirm_industry" if state.get("industry_confirmed") else "intake")
                snap = app.get_state(config)
                state = dict(snap.values)
            if not terminal:
                pending = [i for task in snap.tasks for i in task.interrupts]
                if pending:
                    state["__interrupt__"] = pending
                elif snap.next:
                    state = app.invoke(None, config=config)
        else:
            if snap.created_at or run_row:
                print("This thread is already in use. Resume it or start a new thread.", file=sys.stderr)
                return 1
            if args.prompt and active_llm is None:
                print("Free-text intake needs a local extraction client. Use local JSON or interactive intake.", file=sys.stderr)
                return 1
            if args.prompt:
                init_state = {"raw_input": args.prompt}
            else:
                answers = (json.loads(args.input.read_text(encoding="utf-8"), parse_float=Decimal)
                           if args.input else collect_interactive_intake())
                init_state = {"intake_answers": answers}
            init_state.update(integrity_version=2, explain_mode=default_mode)
            state = app.invoke(init_state, config=config)
            terminal = False

        while not terminal:
            if state.get("company_profile") is not None:
                case_id, run_id = repo.sync_profile(thread_id, state["company_profile"], state.get("intake_answers"))
                repo.sync_decisions(run_id, state)
            if "__interrupt__" not in state:
                if app.get_state(config).next:
                    raise RuntimeError("Cannot finalize while graph work remains.")
                if run_id is None:
                    raise RuntimeError("No persisted case for terminal checkpoint.")
                repo.finish_analysis_run(run_id, state)
                break
            if not args.resume and args.pause_after_intake:
                print(f"Paused after intake. Resume with: --resume {thread_id}")
                return 0
            payload = state["__interrupt__"][0].value
            try:
                decision = _prompt_hitl_decision(payload, args.auto_approve)
            except PauseRequested:
                print(f"Paused. Resume with: --resume {thread_id}")
                return 0
            state = app.invoke(Command(resume=decision), config=config)

    if run_id and state.get("valuation") and state.get("integrity_version") == 2:
        _export_packet(repo.review_report(thread_id), args.export_md, args.export_json)
        return 0
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
