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

from covenant_ai.app.errors import (
    DuplicateThreadError,
    LocalPolicyError,
    RunNotFoundError,
    ValidationAppError,
)
from covenant_ai.app.exports import export_packet_files, format_packet_banner
from covenant_ai.app.reviews import ReviewService
from covenant_ai.app.runs import ValuationRunnerService, ensure_local_db_url
from covenant_ai.domain.amounts import parse_amount
from covenant_ai.domain.intake_questions import Stage, questions
from covenant_ai.domain.models import ExplainMode
from covenant_ai.llm.client import LLMClient
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
    report = export_packet_files(packet, markdown_path=markdown, json_path=json_path)
    print(f"Thread {packet['thread_id']} · {str(packet['release_status']).upper()}")
    print(report)


def _prompt_legacy_field(field: str) -> Decimal | None:
    while True:
        raw = input(f"Confirm target {field} in USD (0 if zero, Enter if unknown): ").strip()
        if not raw:
            return None
        try:
            amount = Decimal(raw)
            if not amount.is_finite() or amount < 0:
                raise ValueError
            return amount
        except (ArithmeticError, ValueError):
            print("Enter a nonnegative USD amount.")


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
    try:
        db_url = ensure_local_db_url(args.db_url)
    except LocalPolicyError as exc:
        parser.error(str(exc))

    repo = PostgresCaseRepository(db_url=db_url)
    if args.review_report:
        reviews = ReviewService(repo)
        packet = (
            reviews.approve_legacy_windows_sid(args.review_report, args.approve_digest)
            if args.approve_digest
            else reviews.get_frozen_packet(args.review_report)
        )
        _export_packet(packet, args.export_md, args.export_json)
        return 0

    runner = ValuationRunnerService(db_url=db_url, repo=repo, llm_client=llm_client)
    thread_id = args.resume or args.thread_id or f"run-{uuid4().hex}"

    try:
        if args.resume:
            legacy_cb = None if args.auto_approve else _prompt_legacy_field
            step = runner.inspect_or_recover_run(
                thread_id,
                explain_mode=args.explain_mode,
                legacy_confirm_fn=legacy_cb,
            )
            if step is None:
                print("Legacy target fields need confirmation. Resume interactively; Enter keeps EV only.")
                return 0
        else:
            if args.prompt:
                step = runner.start_run(
                    raw_input=args.prompt,
                    thread_id=thread_id,
                    explain_mode=args.explain_mode,
                )
            else:
                answers = (
                    json.loads(args.input.read_text(encoding="utf-8"), parse_float=Decimal)
                    if args.input
                    else collect_interactive_intake()
                )
                step = runner.start_run(
                    intake_answers=answers,
                    thread_id=thread_id,
                    explain_mode=args.explain_mode,
                )
    except (DuplicateThreadError, RunNotFoundError, ValidationAppError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    while not step.terminal:
        if not args.resume and args.pause_after_intake:
            print(f"Paused after intake. Resume with: --resume {thread_id}")
            return 0
        assert step.pending_prompt is not None
        try:
            decision = _prompt_hitl_decision(step.pending_prompt.payload, args.auto_approve)
        except PauseRequested:
            print(f"Paused. Resume with: --resume {thread_id}")
            return 0
        step = runner.submit_hitl_decision(
            thread_id,
            decision,
            explain_mode=args.explain_mode,
        )

    if step.packet is not None:
        _export_packet(step.packet, args.export_md, args.export_json)
        return 0

    state = step.state
    explanation = state.get("explanation") or "Workflow completed without explanation."
    if args.export_md:
        args.export_md.write_text(explanation, encoding="utf-8")
        print(f"Wrote Markdown report to {args.export_md}")
    if args.export_json:
        args.export_json.write_text(
            json.dumps(
                {
                    "thread_id": step.thread_id,
                    "run_id": str(step.run_id) if step.run_id else None,
                    "case_id": str(step.case_id) if step.case_id else None,
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
