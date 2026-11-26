from decimal import Decimal

import psycopg

from covenant_ai.providers.postgres import PostgresMarketDataProvider


class _DummyCursor:
    def __init__(self, responses):
        self._responses = list(responses)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        self._current = self._responses.pop(0)

    def fetchall(self):
        return self._current

    def fetchone(self):
        return self._current


class _DummyConn:
    def __init__(self, responses):
        self._responses = responses

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _DummyCursor(self._responses)


def test_postgres_get_candidate_peers_uses_verified_db_rows(monkeypatch):
    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda *a, **kw: _DummyConn([[("ADBE",), ("CRM",), ("INTU",)]]),
    )
    provider = PostgresMarketDataProvider()
    assert provider.get_candidate_peers("application_software") == ["ADBE", "CRM", "INTU"]


def test_postgres_get_candidate_peers_returns_empty_for_unseeded_industry_when_db_seeded(
    monkeypatch,
):
    monkeypatch.setattr(
        psycopg,
        "connect",
        lambda *a, **kw: _DummyConn([[], (True,)]),
    )
    provider = PostgresMarketDataProvider()
    assert provider.get_candidate_peers("commercial_services") == []


def test_postgres_get_candidate_peers_falls_back_to_edgar_on_db_error(monkeypatch):
    def _fail(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", _fail)
    provider = PostgresMarketDataProvider()
    peers = provider.get_candidate_peers("restaurants")
    # Edgar fallback yields snapshot-backed verified restaurant peers
    assert len(peers) >= 8
    assert {"YUM", "SBUX", "BJRI"}.issubset(set(peers))


def test_postgres_screening_candidates_handles_nullable_and_curated_enums(monkeypatch):
    rows = [
        ("CACI", "mid_cap", "gov_defense_it", "gov", "curated", "revenue", Decimal("8000000000")),
        ("NULL_PEER", "small_cap", None, None, None, "revenue", Decimal("200000000")),
    ]
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: _DummyConn([rows]))
    provider = PostgresMarketDataProvider()
    cands = provider.get_peer_screening_candidates("it_services", period="FY")
    by_t = {c.ticker: c for c in cands}

    assert by_t["CACI"].business_profile is not None
    assert by_t["CACI"].business_profile.sub_industry == "gov_defense_it"
    assert by_t["CACI"].business_profile.customers == ("gov",)
    assert by_t["NULL_PEER"].business_profile is None
