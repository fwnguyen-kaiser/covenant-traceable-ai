"""Verify version pinning, screening failures, and immutable packet exports."""
from contextlib import nullcontext
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import openpyxl
import psycopg
import pytest

from covenant_ai.app.exports import format_packet_banner, render_xlsx_bytes
from covenant_ai.domain.evidence import inspect_v2_packet_remediation_warnings
from covenant_ai.graph.nodes import make_fetch_data_node, make_peer_candidates_node, make_peer_rank_node
from covenant_ai.providers.db_schema import normalize_db_url
from covenant_ai.providers.edgar import ReplayMarketDataProvider
from covenant_ai.providers.edgar_errors import MissingEvidenceError
from covenant_ai.providers.postgres import PostgresMarketDataProvider
from covenant_ai.providers.postgres_repo import make_review_packet, packet_sha256_digest


def test_run_pins_method_before_screening_and_freezes_diagnostics():
    def unavailable(industry, period):
        raise TimeoutError("upstream screening timed out")

    provider = SimpleNamespace(method_version="cover-gaap-v3",
                               get_candidate_peers=lambda industry: ["A", "B", "C"],
                               get_peer_screening_candidates=unavailable)
    state = {"industry_enum": "industrial_machinery"}
    state.update(make_peer_candidates_node(provider)(state))
    state.update(make_peer_rank_node(provider)(state))
    failure = state["screening_failures"]["__screening__"]
    assert failure["reason_code"] == "network_transient"
    assert failure["retryable"]
    packet = make_review_packet(uuid4(), state)
    assert packet["screening_failures"]["__screening__"] == failure
    digest = packet_sha256_digest(packet)
    assert packet["method_version"] == "cover-gaap-v3"
    assert packet["method_version"] in format_packet_banner(packet)
    book = openpyxl.load_workbook(BytesIO(render_xlsx_bytes(packet)), read_only=True)
    assert any(row[0] == "Method Version" and row[1] == packet["method_version"]
               for row in book.worksheets[0].values)
    assert packet_sha256_digest(packet) == digest

    provider.method_version = "cover-gaap-v2"
    with pytest.raises(ValueError, match="Checkpoint method"):
        make_fetch_data_node(provider)(state)


def test_legacy_packet_does_not_acquire_a_method_from_export_or_replay():
    packet = {"sources": []}
    digest = packet_sha256_digest(packet)
    assert "method unrecorded" in format_packet_banner(packet)
    assert ReplayMarketDataProvider(packet).method_version is None
    assert "does not record" in inspect_v2_packet_remediation_warnings(packet)[0]
    assert packet_sha256_digest(packet) == digest


def test_seed_method_filters_all_database_reads(monkeypatch):
    # The temp tables shadow the real schema on just this connection.
    with psycopg.connect(normalize_db_url(None)) as conn:
        conn.execute("CREATE TEMP TABLE peer_companies (LIKE public.peer_companies INCLUDING DEFAULTS)")
        conn.execute("CREATE TEMP TABLE financial_datapoints (LIKE public.financial_datapoints INCLUDING DEFAULTS)")
        conn.execute("CREATE TEMP TABLE peer_baseline_batches (id uuid PRIMARY KEY, method_version text)")
        for ticker, version in (("OLD", None), ("NEW", "cover-gaap-v3")):
            batch = uuid4()
            if version:
                conn.execute("INSERT INTO peer_baseline_batches VALUES (%s,%s)", (batch, version))
            conn.execute(
                "INSERT INTO peer_companies (baseline_id,ticker,cik,company_name,industry,exchange) "
                "VALUES (%s,%s,'1',%s,'test','NYSE')", (batch, ticker, ticker),
            )
            conn.execute(
                "INSERT INTO financial_datapoints (baseline_id,peer_ticker,metric,value,period,source_type) "
                "VALUES (%s,%s,'revenue',100,'FY','sec_filing')", (batch, ticker),
            )
        monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: nullcontext(conn))
        for version, match, other in (("cover-gaap-v2", "OLD", "NEW"), ("cover-gaap-v3", "NEW", "OLD")):
            provider = PostgresMarketDataProvider(method_version=version, fallback_to_edgar=False)
            assert provider.get_candidate_peers("test") == [match]
            assert provider.is_verified_peer(match) and not provider.is_verified_peer(other)
            assert [c.ticker for c in provider.get_peer_screening_candidates("test")] == [match]
            assert provider.get_peer_financials(match)[0].peer_ticker == match
            with pytest.raises(MissingEvidenceError, match="incompatible"):
                provider.get_peer_financials(other)


def test_default_method_is_v3_and_postgres_fallback_respects_method_mismatch(monkeypatch):
    """Default provider uses cover-gaap-v3 while explicit rollback to v2 works cleanly across mismatched DB seeds."""
    from covenant_ai.domain.evidence import DEFAULT_METHOD_VERSION, MethodologyVersion

    assert DEFAULT_METHOD_VERSION == MethodologyVersion.V3.value
    with psycopg.connect(normalize_db_url(None)) as conn:
        conn.execute("CREATE TEMP TABLE peer_companies (LIKE public.peer_companies INCLUDING DEFAULTS)")
        conn.execute("CREATE TEMP TABLE financial_datapoints (LIKE public.financial_datapoints INCLUDING DEFAULTS)")
        conn.execute("CREATE TEMP TABLE peer_baseline_batches (id uuid PRIMARY KEY, method_version text)")
        batch = uuid4()
        conn.execute("INSERT INTO peer_baseline_batches VALUES (%s,%s)", (batch, "cover-gaap-v3"))
        conn.execute(
            "INSERT INTO peer_companies (baseline_id,ticker,cik,company_name,industry,exchange) "
            "VALUES (%s,'ADBE','796343','Adobe Inc.','application_software','Nasdaq')",
            (batch,),
        )
        conn.execute(
            "INSERT INTO financial_datapoints (baseline_id,peer_ticker,metric,value,period,source_type) "
            "VALUES (%s,'ADBE','revenue',100,'FY','sec_filing')",
            (batch,),
        )
        monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: nullcontext(conn))
        # When DB only holds cover-gaap-v3, an explicit cover-gaap-v2 rollback falls back to EDGAR v2 cleanly.
        v2_provider = PostgresMarketDataProvider(method_version="cover-gaap-v2", fallback_to_edgar=True)
        v2_pts = {d.metric: d.value for d in v2_provider.get_peer_financials("ADBE", period="FY")}
        assert v2_pts["total_debt"] == Decimal("6648000000")
        assert v2_pts["interest_expense_net"] == Decimal("263000000")
