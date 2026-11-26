"""PostgreSQL Domain Audit Repository for Project Covenant.

Persists cases, versioned company profiles, analysis runs, ranked/dropped peer
candidates, run-scoped financial datapoints (FIN-06), valuation results, and
append-only audit logs matching db/init.sql and docs/covenant-erd.mmd.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg

from covenant_ai.domain.models import CompanyProfile, FinancialDatapoint
from covenant_ai.domain.screening import PeerScreeningScore
from covenant_ai.providers.db_schema import ensure_domain_schema, normalize_db_url


def _coerce_uuid(raw_id: Any) -> UUID:
    try:
        return UUID(str(raw_id))
    except Exception:
        return uuid4()


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


class PostgresCaseRepository:
    """Transactional audit repository backed by Docker PostgreSQL."""

    def __init__(self, db_url: str | None = None, auto_migrate: bool = True) -> None:
        self.db_url = normalize_db_url(db_url)
        if auto_migrate:
            ensure_domain_schema(self.db_url)

    def create_case_and_profile(
        self,
        profile: CompanyProfile,
        intake_answers: dict[str, Any] | None = None,
        *,
        case_id: UUID | None = None,
        actor_id: UUID | None = None,
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
        }

        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
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
            conn.commit()
        return _coerce_uuid(resolved_case_id), _coerce_uuid(profile_id)

    def start_analysis_run(
        self,
        case_id: UUID,
        profile_id: UUID | None,
        thread_id: str,
        *,
        ai_run_id: str | None = None,
        actor_id: UUID | None = None,
    ) -> UUID:
        """Create a new analysis_runs record bound to a LangGraph thread_id."""
        with psycopg.connect(self.db_url, connect_timeout=5) as conn:
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
            conn.commit()
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
                    "UPDATE analysis_runs SET profile_id = %s WHERE id = %s;",
                    (str(profile_id), str(run_id)),
                )
            conn.commit()

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
                    WHERE id = %s;
                    """,
                    (status, str(run_id)),
                )

                # Replace any prior candidates/datapoints belonging strictly to this run_id
                cur.execute("DELETE FROM peer_candidates WHERE run_id = %s;", (str(run_id),))
                cur.execute("DELETE FROM financial_datapoints WHERE run_id = %s;", (str(run_id),))

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
                            _to_jsonb({"source": "manual_or_fallback"}),
                            failed_peers.get(tu),
                        ),
                    )

                # Persist frozen run-scoped financial datapoints (FIN-06)
                for dp in ledger:
                    cur.execute(
                        """
                        INSERT INTO financial_datapoints (
                            id, run_id, peer_ticker, metric, value, currency,
                            period, period_end, source_type, source_ref, retrieved_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                        """,
                        (
                            str(uuid4()),
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
                        ON CONFLICT (run_id) DO UPDATE SET
                            ev_low = EXCLUDED.ev_low,
                            ev_mid = EXCLUDED.ev_mid,
                            ev_high = EXCLUDED.ev_high,
                            equity_low = EXCLUDED.equity_low,
                            equity_mid = EXCLUDED.equity_mid,
                            equity_high = EXCLUDED.equity_high,
                            multiples_used = EXCLUDED.multiples_used,
                            sales_multiples_used = EXCLUDED.sales_multiples_used,
                            peer_stats = EXCLUDED.peer_stats,
                            outlier_flags = EXCLUDED.outlier_flags,
                            excluded_peers = EXCLUDED.excluded_peers,
                            limitations = EXCLUDED.limitations,
                            explanation_md = EXCLUDED.explanation_md,
                            narrative_md = EXCLUDED.narrative_md,
                            audit_verdict = EXCLUDED.audit_verdict,
                            audit_findings = EXCLUDED.audit_findings,
                            prompt_version = EXCLUDED.prompt_version;
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
