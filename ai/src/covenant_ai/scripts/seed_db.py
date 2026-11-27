"""Seed SEC EDGAR market and peer financial data into PostgreSQL.

Reads verified company profiles from peer_verification.json, parses their
10-K (FY) and cumulative YTD + MRQ 10-Q (LTM) XBRL facts via
EdgarMarketDataProvider, and populates:
  - peer_companies: verified ticker universe, SIC, exchange, EV tier, and rationale
  - financial_datapoints: baseline GAAP + screening datapoints (run_id IS NULL)
"""
from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg

from covenant_ai.providers.db_schema import ensure_domain_schema, normalize_db_url
from covenant_ai.providers.edgar import EdgarMarketDataProvider

DEFAULT_DB_URL = "postgresql://covenant_user:covenant_secure_pass_2026@localhost:5432/covenant_ai"
MIN_SEED_PEERS_PER_INDUSTRY = 8
SKIPPED_INDUSTRIES: dict[str, str] = {}


def _to_decimal_or_none(raw: object) -> Decimal | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return Decimal(s) if s else None


def seed_database(db_url: str | None = None) -> None:
    conn_str = normalize_db_url(db_url)
    ensure_domain_schema(conn_str)

    mod_dir = Path(__file__).resolve().parent.parent
    veri_path = mod_dir / "peer_verification.json"
    if not veri_path.exists():
        raise FileNotFoundError(f"Verification file not found at {veri_path}")

    veri_data = json.loads(veri_path.read_text(encoding="utf-8"))["industries"]
    provider = EdgarMarketDataProvider()

    parsed_db_url = urlsplit(conn_str)
    db_location = f"{parsed_db_url.hostname}:{parsed_db_url.port or 5432}/{parsed_db_url.path.lstrip('/')}"
    print(f"Connecting to database {db_location}...")
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            batch_id = uuid4()
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('covenant_seed_baseline', 0))")
            cur.execute("UPDATE peer_companies SET verified=FALSE")
            total_companies = 0
            total_fy_companies = 0
            total_ltm_companies = 0
            total_datapoints = 0

            print("Seeding peer companies and FY + LTM financial datapoints...")
            for industry, peers in veri_data.items():
                if industry in SKIPPED_INDUSTRIES:
                    print(f"  - {industry}: skipped ({SKIPPED_INDUSTRIES[industry]})")
                    continue

                ind_seeded = 0
                ind_ebitda_eligible = 0
                ind_ltm = 0

                for p in peers:
                    if not p.get("verified"):
                        continue

                    ticker = p["ticker"].upper().strip()
                    cik = str(p.get("cik", ""))
                    name = p.get("name", ticker)
                    sic = str(p.get("sic", ""))
                    period_end = p.get("latest_period_end")
                    da_basis = p.get("da_basis", "combined")
                    exchange = p.get("exchange")
                    ev_tier = p.get("ev_tier")
                    sub_industry = p.get("sub_industry")
                    customer_type = p.get("customer_type")
                    classification_source = p.get("classification_source")
                    ev_ebitda_eligible = bool(p.get("ev_ebitda_eligible", True))
                    latest_ev = _to_decimal_or_none(p.get("latest_ev_usd"))
                    latest_eb = _to_decimal_or_none(p.get("latest_ebitda_usd"))
                    latest_rev = _to_decimal_or_none(p.get("latest_revenue_usd"))
                    rationale = p.get("selection_rationale") or {}

                    try:
                        fy_pts = provider.get_peer_financials(ticker, period="FY")
                    except Exception as exc:
                        print(f"  Warning: failed to extract FY datapoints for {ticker}: {exc}")
                        continue

                    errors = {}
                    ltm_pts = []
                    try:
                        ltm_pts = provider.get_peer_financials(ticker, period="LTM")
                    except Exception as exc:
                        errors["LTM"] = str(exc)

                    cur.execute(
                        """
                        INSERT INTO peer_companies (
                            baseline_id, baseline_errors, ticker, cik, company_name, industry, sic_code,
                            exchange, ev_tier, sub_industry, customer_type,
                            classification_source, ev_ebitda_eligible,
                            latest_ev_usd, latest_ebitda_usd, latest_revenue_usd,
                            latest_period_end, da_basis, verified, selection_rationale
                        ) VALUES (
                            %s, %s::jsonb, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s,
                            %s, %s, %s,
                            %s, %s, %s, %s::jsonb
                        )
                        ON CONFLICT (ticker) DO UPDATE SET
                            baseline_id = EXCLUDED.baseline_id, baseline_errors = EXCLUDED.baseline_errors,
                            company_name = EXCLUDED.company_name,
                            industry = EXCLUDED.industry,
                            sic_code = EXCLUDED.sic_code,
                            exchange = EXCLUDED.exchange,
                            ev_tier = EXCLUDED.ev_tier,
                            sub_industry = EXCLUDED.sub_industry,
                            customer_type = EXCLUDED.customer_type,
                            classification_source = EXCLUDED.classification_source,
                            ev_ebitda_eligible = EXCLUDED.ev_ebitda_eligible,
                            latest_ev_usd = EXCLUDED.latest_ev_usd,
                            latest_ebitda_usd = EXCLUDED.latest_ebitda_usd,
                            latest_revenue_usd = EXCLUDED.latest_revenue_usd,
                            latest_period_end = EXCLUDED.latest_period_end,
                            da_basis = EXCLUDED.da_basis,
                            verified = EXCLUDED.verified,
                            selection_rationale = EXCLUDED.selection_rationale;
                        """,
                        (
                            batch_id, json.dumps(errors), ticker,
                            cik,
                            name,
                            industry,
                            sic,
                            exchange,
                            ev_tier,
                            sub_industry,
                            customer_type,
                            classification_source,
                            ev_ebitda_eligible,
                            latest_ev,
                            latest_eb,
                            latest_rev,
                            date.fromisoformat(period_end) if period_end else None,
                            da_basis,
                            True,
                            json.dumps(rationale),
                        ),
                    )
                    for dp in fy_pts + ltm_pts:
                        cur.execute(
                            """
                            INSERT INTO financial_datapoints (
                                baseline_id, id, run_id, peer_ticker, metric, value,
                                currency, period, period_end, source_type,
                                source_ref, retrieved_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                            """,
                            (
                                batch_id, dp.id,
                                None,
                                ticker,
                                dp.metric,
                                dp.value,
                                dp.currency,
                                dp.period,
                                dp.period_end,
                                dp.source_type.value,
                                dp.source_ref,
                                dp.retrieved_at,
                            ),
                        )
                        total_datapoints += 1

                    total_companies += 1
                    total_fy_companies += 1
                    ind_seeded += 1
                    if ev_ebitda_eligible:
                        ind_ebitda_eligible += 1
                    if ltm_pts:
                        total_ltm_companies += 1
                        ind_ltm += 1

                print(
                    f"  - {industry:<26}: {ind_seeded:>2} total "
                    f"({ind_ebitda_eligible:>2} EV/EBITDA eligible, {ind_ltm:>2} LTM-ready)"
                )
                if ind_ebitda_eligible < MIN_SEED_PEERS_PER_INDUSTRY:
                    raise RuntimeError(
                        f"Only {ind_ebitda_eligible} EV/EBITDA-eligible peers were seeded for {industry}; "
                        f"at least {MIN_SEED_PEERS_PER_INDUSTRY} are required. "
                        "The seed transaction was rolled back."
                    )

            conn.commit()

            print("\n========================================================")
            print("Seeding completed successfully!")
            print(f"Total verified peer companies : {total_companies} (FY={total_fy_companies}, LTM={total_ltm_companies})")
            print(f"Total baseline datapoints     : {total_datapoints}")
            print("========================================================")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else None
    seed_database(url)
