from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg

from covenant_ai.domain.models import CompanyProfile, FinancialDatapoint, SourceType
from covenant_ai.domain.screening import PeerScreeningScore
from covenant_ai.providers.postgres_repo import PostgresCaseRepository


def test_postgres_case_repository_roundtrip_live_db():
    """Verify full transactional persistence across cases, profiles, runs, candidates,
    run-scoped datapoints, valuation_results, and audit_logs without polluting baseline.
    """
    repo = PostgresCaseRepository()
    profile = CompanyProfile(
        company_name="Repo Smoke Test Co",
        industry="restaurants",
        geography="US",
        revenue=Decimal("25000000"),
        ebitda=Decimal("4000000"),
        ebitda_period="FY",
        customer_concentration_pct=Decimal("15"),
        revenue_growth_pct=Decimal("12"),
        total_debt=Decimal("5000000"),
        cash=Decimal("1000000"),
    )

    case_id = None
    run_id = None
    try:
        case_id, profile_id = repo.create_case_and_profile(profile, {"company_name": "Repo Smoke Test Co"})
        run_id = repo.start_analysis_run(case_id, profile_id, thread_id=f"test-thread-{case_id}")

        dp = FinancialDatapoint(
            metric="market_cap",
            value=Decimal("500000000"),
            period="FY",
            period_end=date(2026, 2, 25),
            source_type=SourceType.MARKET_DATA,
            source_ref="test snapshot",
            retrieved_at=datetime.now(UTC),
            peer_ticker="BJRI",
        )
        score = PeerScreeningScore(
            ticker="BJRI",
            rank_order=1,
            total_score=Decimal("0.850000"),
            business_score=Decimal("0.500000"),
            financial_score=Decimal("0.850000"),
            size_distance=Decimal("1.230000"),
            ev_tier="small_cap",
            status="selected",
            rationale="closest size match",
        )
        state = {
            "selected_peers": ["BJRI"],
            "failed_peers": {"BAD": "missing_10k"},
            "peer_screening_results": [score],
            "ledger": [dp],
            "valuation": {
                "ev_low": "32000000",
                "ev_mid": "40000000",
                "ev_high": "48000000",
                "equity_low": "28000000",
                "equity_mid": "36000000",
                "equity_high": "44000000",
                "multiples_used": {"BJRI": "10.0"},
                "sales_multiples_used": {"BJRI": "1.2"},
                "peer_stats": {"ev_ebitda": {"median": "10.0"}},
                "outlier_flags": {},
                "excluded_peers": {},
                "limitations": ["test limitation"],
            },
        }
        repo.finish_analysis_run(run_id, state)

        with psycopg.connect(repo.db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM analysis_runs WHERE id = %s;", (str(run_id),))
                assert cur.fetchone()[0] == "completed"
                cur.execute("SELECT ticker, status FROM peer_candidates WHERE run_id = %s ORDER BY ticker;", (str(run_id),))
                assert cur.fetchall() == [("BAD", "failed"), ("BJRI", "selected")]
                cur.execute("SELECT COUNT(*) FROM financial_datapoints WHERE run_id = %s;", (str(run_id),))
                assert cur.fetchone()[0] == 1
                cur.execute("SELECT ev_mid FROM valuation_results WHERE run_id = %s;", (str(run_id),))
                assert Decimal(cur.fetchone()[0]) == Decimal("40000000")
    finally:
        if case_id is not None:
            with psycopg.connect(repo.db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM audit_logs WHERE entity_id = %s;", (str(case_id),))
                    if run_id is not None:
                        cur.execute("DELETE FROM audit_logs WHERE entity_id = %s;", (str(run_id),))
                    cur.execute("DELETE FROM audit_logs WHERE detail->>'case_id' = %s;", (str(case_id),))
                    cur.execute("DELETE FROM cases WHERE id = %s;", (str(case_id),))
                conn.commit()
