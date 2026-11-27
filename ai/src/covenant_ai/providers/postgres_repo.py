"""PostgreSQL Domain Audit Repository for Project Covenant.

Persists cases, versioned company profiles, analysis runs, ranked/dropped peer
candidates, run-scoped financial datapoints (FIN-06), valuation results, and
append-only audit logs matching db/init.sql and docs/covenant-erd.mmd.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

import psycopg

from covenant_ai.domain.bridge import build_peer_financials
from covenant_ai.domain.models import CompanyProfile, FinancialDatapoint
from covenant_ai.domain.screening import PeerScreeningScore
from covenant_ai.providers.reviewer import authorized_windows_reviewer
from covenant_ai.providers.db_schema import ensure_domain_schema, normalize_db_url


def _coerce_uuid(raw_id: Any) -> UUID:
    return UUID(str(raw_id))


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def _to_jsonb(data: Any) -> str:
    return json.dumps(data, default=_json_default)


def _profile_digest(profile: CompanyProfile, answers: dict | None) -> str:
    encoded = json.dumps([profile.model_dump(mode="json"), answers or {}], sort_keys=True, default=_json_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def make_review_packet(run_id: UUID, state: dict) -> dict:
    keys = ("valuation", "company_profile", "selected_peers", "peer_screening_results", "failed_peers",
            "audit_verdict", "audit_findings", "audit_fallback_used", "explain_mode", "prompt_version",
            "inconsistencies", "explanation", "peer_review_rationale", "review_decisions", "intake_provenance")
    valuation = state.get("valuation") or {}
    ledger = state.get("ledger") or []
    by_peer = {}
    for ticker in valuation.get("multiples_used", {}):
        _, ids = build_peer_financials(ledger, ticker, valuation["ebitda_period"])
        by_peer[ticker] = [str(uuid5(run_id, origin)) for origin in ids]
    return {
        "lineage": {
            "ev_mid": {"formula": "median(peer EV/EBITDA) * target normalized EBITDA",
                       "peer_snapshot_ids": by_peer,
                       "target_snapshot_ids": [str(uuid5(run_id, str(d.id))) for d in ledger if not d.peer_ticker]},
            "equity_mid": {"formula": "max(0, ev_mid - target debt + target cash)",
                           "inputs": ["company_profile.total_debt", "company_profile.cash"]},
        },
        **{key: state.get(key) for key in keys},
        "method_version": "cover-gaap-v2",
        "release_status": "draft",
        "sources": [
            {**dp.model_dump(mode="json"), "snapshot_id": str(uuid5(run_id, str(dp.id)))}
            for dp in state.get("ledger") or []
        ],
    }


class PostgresCaseRepository:
    """Transactional audit repository backed by Docker PostgreSQL."""

    def __init__(self, db_url: str | None = None, auto_migrate: bool = True) -> None:
        self.db_url = normalize_db_url(db_url)
        if auto_migrate:
            ensure_domain_schema(self.db_url)

    @contextmanager
    def thread_lock(self, thread_id: str):
        """Serialize starts, checkpoint transitions and publication of the domain snapshot."""
        with psycopg.connect(self.db_url, autocommit=True, connect_timeout=5) as conn:
            acquired = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (thread_id,)
            ).fetchone()[0]
            if not acquired:
                raise ValueError("Another session is already operating on this thread.")
            yield

    def sync_profile(self, thread_id: str, profile: CompanyProfile, answers: dict | None) -> tuple:
        """Reconcile against the committed profile version in one transaction."""
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT r.id, r.case_id, r.status, p.extra->>'profile_digest' "
                "FROM analysis_runs r LEFT JOIN company_profiles p ON p.id=r.profile_id "
                "WHERE r.thread_id=%s FOR UPDATE OF r", (thread_id,)
            ).fetchone()
            if row is None:
                case_id, profile_id = self.create_case_and_profile(profile, answers, connection=conn)
                run_id = self.start_analysis_run(case_id, profile_id, thread_id, connection=conn)
                return case_id, run_id
            run_id, case_id, status, digest = row
            if status == "running" and digest != _profile_digest(profile, answers):
                _, profile_id = self.create_case_and_profile(profile, answers, case_id=case_id, connection=conn)
                conn.execute("UPDATE analysis_runs SET profile_id=%s WHERE id=%s", (profile_id, run_id))
            return case_id, run_id

    def create_case_and_profile(
        self,
        profile: CompanyProfile,
        intake_answers: dict[str, Any] | None = None,
        *,
        case_id: UUID | None = None,
        actor_id: UUID | None = None,
        connection: psycopg.Connection | None = None,
    ) -> tuple[UUID, UUID]:
        """Insert/update a case row and append a new versioned company_profiles row."""
        extra_payload = {
            "ebitda_period": profile.ebitda_period,
            "target_fiscal_year": profile.target_fiscal_year,
            "period_end_date": (
                profile.period_end_date.isoformat() if profile.period_end_date is not None else None
            ),
            "reported_ebitda": str(profile.reported_ebitda) if profile.reported_ebitda is not None else None,
            "business_description": profile.business_description,
            "geography_region": (
                profile.geography_region.value if profile.geography_region is not None else None
            ),
            "revenue_model": profile.revenue_model,
            "revenue_model_type": (
                profile.revenue_model_type.value if profile.revenue_model_type is not None else None
            ),
            "revenue_model_note": profile.revenue_model_note,
            "customer_concentration_pct": (
                str(profile.customer_concentration_pct)
                if profile.customer_concentration_pct is not None
                else None
            ),
            "revenue_growth_pct": (
                str(profile.revenue_growth_pct) if profile.revenue_growth_pct is not None else None
            ),
            "capex": str(profile.capex) if profile.capex is not None else None,
            "credit_rating": profile.credit_rating,
            "adjustment_schedule": (
                profile.adjustment_schedule.model_dump(mode="json")
                if profile.adjustment_schedule is not None
                else None
            ),
            "business_profile": (
                profile.model_dump(mode="json").get("business_profile")
                if profile.business_profile is not None
                else None
            ),
            "inference_provenance": (
                {k: v.model_dump(mode="json") for k, v in profile.inference_provenance.items()}
                if profile.inference_provenance
                else {}
            ),
            "inconsistencies": list(profile.inconsistencies),
            "intake_answers": intake_answers or {},
            "profile_digest": _profile_digest(profile, intake_answers),
        }

        with (nullcontext(connection) if connection is not None else psycopg.connect(self.db_url, connect_timeout=5)) as conn:
            with conn.cursor() as cur:
                if case_id is None:
                    cur.execute(
                        """
                        INSERT INTO cases (owner_id, company_name, status)
                        VALUES (%s, %s, 'active')
                        RETURNING id;
                        """,
                        (str(actor_id) if actor_id else None, profile.company_name),
                    )
                    resolved_case_id = cur.fetchone()[0]
                    next_version = 1
                else:
                    resolved_case_id = case_id
                    cur.execute(
                        "UPDATE cases SET company_name = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s;",
                        (profile.company_name, str(resolved_case_id)),
                    )
                    cur.execute(
                        "SELECT COALESCE(MAX(version), 0) + 1 FROM company_profiles WHERE case_id = %s;",
                        (str(resolved_case_id),),
                    )
                    next_version = int(cur.fetchone()[0])

                cur.execute(
                    """
                    INSERT INTO company_profiles (
                        case_id, version, industry, geography, revenue,
                        ebitda, total_debt, cash, extra
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    RETURNING id;
                    """,
                    (
                        str(resolved_case_id),
                        next_version,
                        profile.industry,
                        profile.geography,
                        profile.revenue,
                        profile.ebitda,
                        profile.total_debt,
                        profile.cash,
                        _to_jsonb(extra_payload),
                    ),
                )
                profile_id = cur.fetchone()[0]

                cur.execute(
                    """
                    INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                    VALUES (%s, %s, %s, %s, %s::jsonb);
                    """,
                    (
                        str(actor_id) if actor_id else None,
                        "profile_created",
                        "company_profiles",
                        str(profile_id),
                        _to_jsonb({"case_id": str(resolved_case_id), "version": next_version}),
                    ),
                )
        return _coerce_uuid(resolved_case_id), _coerce_uuid(profile_id)

    def start_analysis_run(
        self,
        case_id: UUID,
        profile_id: UUID | None,
        thread_id: str,
        *,
        ai_run_id: str | None = None,
        actor_id: UUID | None = None,
        connection: psycopg.Connection | None = None,
    ) -> UUID:
        """Create a new analysis_runs record bound to a LangGraph thread_id."""
        with (nullcontext(connection) if connection is not None else psycopg.connect(self.db_url, connect_timeout=5)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO analysis_runs (case_id, profile_id, status, ai_run_id, thread_id)
                    VALUES (%s, %s, 'running', %s, %s)
                    RETURNING id;
                    """,
                    (
                        str(case_id),
                        str(profile_id) if profile_id else None,
                        ai_run_id or thread_id,
                        thread_id,
                    ),
                )
                run_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                    VALUES (%s, %s, %s, %s, %s::jsonb);
                    """,
                    (
                        str(actor_id) if actor_id else None,
                        "run_started",
                        "analysis_runs",
                        str(run_id),
                        _to_jsonb({"case_id": str(case_id), "thread_id": thread_id}),
                    ),
                )
        return _coerce_uuid(run_id)

    def get_run_by_thread_id(self, thread_id: str) -> tuple[UUID, UUID, str] | None:
        """Look up (run_id, case_id, status) for an existing LangGraph thread_id."""
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, case_id, status
                    FROM analysis_runs
                    WHERE thread_id = %s
                    ORDER BY started_at DESC
                    LIMIT 1;
                    """,
                    (thread_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return _coerce_uuid(row[0]), _coerce_uuid(row[1]), str(row[2])

    def update_run_profile(self, run_id: UUID, profile_id: UUID) -> None:
        """Update analysis_runs.profile_id when a HITL edit creates a new profile version."""
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE analysis_runs SET profile_id = %s WHERE id = %s AND status = 'running';",
                    (str(profile_id), str(run_id)),
                )
            conn.commit()

    def review_report(self, thread_id: str, digest: str | None = None) -> dict:
        """Read a frozen packet or confirm the exact version seen by the authorized reviewer."""
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT id, case_id, review_packet FROM analysis_runs WHERE thread_id=%s AND status='completed' FOR UPDATE",
                (thread_id,),
            ).fetchone()
            if not row or not row[2]:
                raise ValueError("No versioned report is available for this thread.")
            run_id, case_id, packet = row
            encoded = json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            if digest is not None:
                reviewer = authorized_windows_reviewer()
                if fingerprint != digest or packet.get("audit_verdict") not in {"warn", "pass"}:
                    raise ValueError("Approval requires the reviewed digest, authorized identity and a completed audit.")
                conn.execute(
                    "INSERT INTO report_approvals(run_id, digest, reviewer_sid) VALUES (%s,%s,%s) "
                    "ON CONFLICT (run_id) DO NOTHING", (run_id, fingerprint, reviewer)
                )
            approval = conn.execute(
                "SELECT reviewer_sid, approved_at FROM report_approvals WHERE run_id=%s AND digest=%s",
                (run_id, fingerprint)
            ).fetchone()
            return {**packet, "thread_id": thread_id, "run_id": str(run_id), "case_id": str(case_id),
                    "digest": fingerprint, "release_status": "approved" if approval else "draft",
                    "approved_by": approval[0] if approval else None,
                    "approved_at": approval[1].isoformat() if approval else None}

    def sync_decisions(self, run_id: UUID, state: dict) -> None:
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            for index, event in enumerate(state.get("review_decisions") or []):
                conn.execute(
                    "INSERT INTO audit_logs(action,entity_type,entity_id,detail,decision_index) "
                    "VALUES (%s,'analysis_runs',%s,%s::jsonb,%s) ON CONFLICT DO NOTHING",
                    (event["action"], run_id, _to_jsonb({"decision": event["decision"]}), index),
                )

    def append_audit_log(
        self,
        action: str,
        entity_type: str,
        entity_id: UUID | None,
        detail: dict[str, Any] | None = None,
        *,
        actor_id: UUID | None = None,
    ) -> None:
        """Append an immutable entry to audit_logs."""
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                    VALUES (%s, %s, %s, %s, %s::jsonb);
                    """,
                    (
                        str(actor_id) if actor_id else None,
                        action,
                        entity_type,
                        str(entity_id) if entity_id else None,
                        _to_jsonb(detail or {}),
                    ),
                )
            conn.commit()

    def finish_analysis_run(
        self,
        run_id: UUID,
        state: dict[str, Any],
        *,
        actor_id: UUID | None = None,
    ) -> None:
        """Persist final run status, peer_candidates, frozen ledger datapoints, and valuation."""
        val = state.get("valuation")
        insufficient = bool(state.get("insufficient_peers"))
        status = "completed" if val else ("insufficient_peers" if insufficient else "failed")

        selected_set = set(state.get("selected_peers") or [])
        failed_peers: dict[str, str] = state.get("failed_peers") or {}
        disqualified_set = set(state.get("disqualified_peers") or [])
        screening_rows: list[Any] = state.get("peer_screening_results") or []
        ledger: list[FinancialDatapoint] = state.get("ledger") or []

        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE analysis_runs
                    SET status = %s, finished_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND status = 'running'
                    RETURNING id;
                    """,
                    (status, str(run_id)),
                )
                if cur.fetchone() is None:
                    return

                seen_tickers: set[str] = set()
                for row in screening_rows:
                    if isinstance(row, PeerScreeningScore):
                        ticker = row.ticker
                        rank_order = row.rank_order
                        total_score = row.total_score
                        business_score = row.business_score
                        financial_score = row.financial_score
                        size_distance = row.size_distance
                        rationale_obj = {"summary": row.rationale, "ev_tier": row.ev_tier}
                    elif isinstance(row, dict):
                        ticker = str(row.get("ticker", "")).upper()
                        rank_order = row.get("rank_order")
                        total_score = row.get("total_score")
                        business_score = row.get("business_score")
                        financial_score = row.get("financial_score")
                        size_distance = row.get("size_distance")
                        rationale_obj = row
                    else:
                        continue

                    if not ticker:
                        continue
                    seen_tickers.add(ticker)
                    drop_reason = failed_peers.get(ticker)
                    if ticker in failed_peers:
                        cand_status = "failed"
                    elif ticker in disqualified_set:
                        cand_status = "disqualified"
                    elif ticker in selected_set:
                        cand_status = "selected"
                    else:
                        cand_status = "reserve"

                    cur.execute(
                        """
                        INSERT INTO peer_candidates (
                            run_id, ticker, rank_order, total_score, business_score,
                            financial_score, size_distance, status, rationale, drop_reason
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s);
                        """,
                        (
                            str(run_id),
                            ticker,
                            rank_order,
                            total_score,
                            business_score,
                            financial_score,
                            size_distance,
                            cand_status,
                            _to_jsonb(rationale_obj),
                            drop_reason,
                        ),
                    )

                # Capture any custom / fallback selected or failed tickers absent from screening_rows
                for t in list(selected_set) + list(failed_peers.keys()):
                    tu = t.upper()
                    if tu in seen_tickers:
                        continue
                    seen_tickers.add(tu)
                    cand_status = "failed" if tu in failed_peers else "selected"
                    cur.execute(
                        """
                        INSERT INTO peer_candidates (
                            run_id, ticker, status, rationale, drop_reason
                        ) VALUES (%s, %s, %s, %s::jsonb, %s);
                        """,
                        (
                            str(run_id),
                            tu,
                            cand_status,
                            _to_jsonb({"source": "manual_or_fallback", "reason": (state.get("peer_review_rationale") or {}).get(tu)}),
                            failed_peers.get(tu),
                        ),
                    )

                # Persist frozen run-scoped financial datapoints (FIN-06)
                for dp in ledger:
                    cur.execute(
                        """
                        INSERT INTO financial_datapoints (
                            id, origin_id, run_id, peer_ticker, metric, value, currency,
                            period, period_end, source_type, source_ref, retrieved_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                        """,
                        (
                            str(uuid5(run_id, str(dp.id))), str(dp.id),
                            str(run_id),
                            dp.peer_ticker,
                            dp.metric,
                            dp.value,
                            dp.currency,
                            dp.period,
                            dp.period_end,
                            dp.source_type.value,
                            dp.source_ref,
                            dp.retrieved_at or datetime.now(UTC),
                        ),
                    )

                if status == "completed" and state.get("explanation"):
                    packet = make_review_packet(run_id, state) if state.get("integrity_version") == 2 else None
                    cur.execute(
                        "UPDATE analysis_runs SET review_packet=%s::jsonb WHERE id=%s",
                        (_to_jsonb(packet) if packet else None, run_id),
                    )

                if isinstance(val, dict) and val.get("ev_low") is not None:
                    cur.execute(
                        """
                        INSERT INTO valuation_results (
                            run_id, ev_low, ev_mid, ev_high, equity_low, equity_mid, equity_high,
                            multiples_used, sales_multiples_used, peer_stats, outlier_flags,
                            excluded_peers, limitations,
                            explanation_md, narrative_md, audit_verdict, audit_findings, prompt_version
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s,
                            %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s,
                            %s, %s, %s, %s::jsonb, %s
                        )
                        """,
                        (
                            str(run_id),
                            Decimal(str(val["ev_low"])),
                            Decimal(str(val["ev_mid"])),
                            Decimal(str(val["ev_high"])),
                            Decimal(str(val["equity_low"])) if val.get("equity_low") is not None else None,
                            Decimal(str(val["equity_mid"])) if val.get("equity_mid") is not None else None,
                            Decimal(str(val["equity_high"])) if val.get("equity_high") is not None else None,
                            _to_jsonb(val.get("multiples_used") or {}),
                            _to_jsonb(val.get("sales_multiples_used") or {}),
                            _to_jsonb(val.get("peer_stats") or {}),
                            _to_jsonb(val.get("outlier_flags") or {}),
                            _to_jsonb(val.get("excluded_peers") or {}),
                            list(val.get("limitations") or []),
                            state.get("explanation"),
                            state.get("narrative_md"),
                            state.get("audit_verdict"),
                            _to_jsonb(state.get("audit_findings") or []),
                            state.get("prompt_version"),
                        ),
                    )

                cur.execute(
                    """
                    INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail)
                    VALUES (%s, %s, %s, %s, %s::jsonb);
                    """,
                    (
                        str(actor_id) if actor_id else None,
                        f"run_{status}",
                        "analysis_runs",
                        str(run_id),
                        _to_jsonb({
                            "selected_peers": sorted(selected_set),
                            "failed_peers": failed_peers,
                            "datapoint_count": len(ledger),
                            "audit_verdict": state.get("audit_verdict"),
                            "inconsistencies": list(state.get("inconsistencies") or []),
                        }),
                    ),
                )
            conn.commit()
