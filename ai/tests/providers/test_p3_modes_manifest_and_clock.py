"""P3 verification suite: collect/offline/replay modes, atomic manifest, price rules, and clock."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from covenant_ai.providers.edgar import EdgarMarketDataProvider, ReplayMarketDataProvider
from covenant_ai.providers.edgar_errors import (
    MalformedSourceError,
    MissingEvidenceError,
    OfflineCacheMissError,
)
from covenant_ai.providers.edgar_manifest import (
    ManifestStore,
    ProviderMode,
    sha256_bytes,
)
from covenant_ai.providers.price_store import PriceSnapshotStore


def _write_minimal_ticker_map(cache_dir: Path, ticker: str = "DEMO", cik: int = 999999) -> None:
    (cache_dir / "sec.gov_files_company_tickers.json").write_text(
        json.dumps({"0": {"cik_str": cik, "ticker": ticker, "title": "Demo Corp"}}),
        encoding="utf-8",
    )


def test_p3_offline_mode_never_calls_transport_or_creates_dir(tmp_path):
    """Offline mode fails immediately without calling transport or creating missing directories."""
    transport_calls: list[str] = []

    def _exploding_transport(url: str, headers: dict[str, str], timeout: float) -> bytes:
        transport_calls.append(url)
        raise AssertionError(f"Transport must never be called in offline mode: {url}")

    missing_dir = tmp_path / "does_not_exist"
    with pytest.raises(OfflineCacheMissError):
        EdgarMarketDataProvider(
            cache_dir=missing_dir,
            mode=ProviderMode.OFFLINE,
            transport=_exploding_transport,
            snapshot_prices={},
        )
    assert not missing_dir.exists()
    assert transport_calls == []

    # Even with ticker map present, missing submissions/facts/Yahoo raises OfflineCacheMissError with 0 network calls
    live_dir = tmp_path / "offline_cache"
    live_dir.mkdir()
    _write_minimal_ticker_map(live_dir, "DEMO", 999999)
    prov = EdgarMarketDataProvider(
        cache_dir=live_dir,
        mode=ProviderMode.OFFLINE,
        transport=_exploding_transport,
        snapshot_prices={},
        allow_on_demand_yahoo=True,
    )
    assert prov.allow_on_demand_yahoo is False
    with pytest.raises(OfflineCacheMissError):
        prov.get_peer_financials("DEMO", "FY")
    assert transport_calls == []


def test_p3_collect_atomic_write_manifest_checksum_and_refresh_generations(tmp_path):
    """Collect writes canonical .json cleanly, verifies SHA-256 manifest, and tracks generations."""
    fixed_clock = lambda: datetime(2026, 7, 1, 9, 30, tzinfo=UTC)
    cache_dir = tmp_path / "collect_cache"
    version_counter = {"n": 0}

    def _fake_transport(url: str, headers: dict[str, str], timeout: float) -> bytes:
        if "company_tickers" in url:
            return json.dumps({"0": {"cik_str": 796343, "ticker": "ADBE", "title": "Adobe"}}).encode("utf-8")
        version_counter["n"] += 1
        return json.dumps({"gen": version_counter["n"], "val": 123}).encode("utf-8")

    prov = EdgarMarketDataProvider(
        cache_dir=cache_dir,
        mode=ProviderMode.COLLECT,
        rate_limit_sleep=0.0,
        transport=_fake_transport,
        snapshot_prices={},
        clock=fixed_clock,
        generation="gen-r1",
    )
    # Canonical key removes duplicate .json suffix
    payload1 = prov._get_company_facts(796343)
    assert payload1["gen"] == Decimal("1")
    canon_file = cache_dir / "data.sec.gov_api_xbrl_companyfacts_CIK0000796343.json"
    assert canon_file.is_file()
    assert not (cache_dir / "data.sec.gov_api_xbrl_companyfacts_CIK0000796343.json.json").exists()

    manifest = ManifestStore(cache_dir).load()
    assert manifest is not None
    assert manifest.generation == "gen-r1"
    entry = manifest.entries["data.sec.gov_api_xbrl_companyfacts_CIK0000796343.json"]
    assert entry.sha256 == sha256_bytes(canon_file.read_bytes())
    assert entry.fetched_at == fixed_clock()

    # Corrupt cache file: offline raises MalformedSourceError, collect recovers atomically with notice
    canon_file.write_text('{"gen": 99, "val": 123}', encoding="utf-8")
    off = EdgarMarketDataProvider(
        cache_dir=cache_dir,
        mode=ProviderMode.OFFLINE,
        snapshot_prices={},
    )
    with pytest.raises(MalformedSourceError, match="checksum mismatch"):
        off._get_company_facts(796343)

    prov_refresh = EdgarMarketDataProvider(
        cache_dir=cache_dir,
        mode=ProviderMode.COLLECT,
        rate_limit_sleep=0.0,
        transport=_fake_transport,
        snapshot_prices={},
        clock=fixed_clock,
        generation="gen-r2",
    )
    recovered = prov_refresh._get_company_facts(796343)
    assert recovered["gen"] == Decimal("2")
    assert any("Recovered corrupt" in w for w in prov_refresh.client.cache_warnings)
    m2 = ManifestStore(cache_dir).load()
    assert m2 is not None and m2.generation == "gen-r2"

    # A malformed upstream refresh must leave the previous valid input readable.
    prov_refresh.client._raw_transport = lambda *_: b"{incomplete"
    with pytest.raises(MalformedSourceError):
        prov_refresh.client.fetch_json(
            "https://data.sec.gov/api/xbrl/companyfacts/CIK0000796343.json", refresh=True
        )
    assert off._get_company_facts(796343) == recovered
    assert ManifestStore(cache_dir).load() == m2


def test_p3_legacy_double_json_fallback_and_concurrent_writes(tmp_path):
    """Legacy .json.json reads cleanly via one fallback; concurrent writes keep manifest intact."""
    cache_dir = tmp_path / "concurrent_cache"
    cache_dir.mkdir()
    _write_minimal_ticker_map(cache_dir, "LEG", 123456)
    legacy_path = cache_dir / "data.sec.gov_submissions_CIK0000123456.json.json"
    legacy_path.write_text('{"legacy": true}', encoding="utf-8")

    off = EdgarMarketDataProvider(cache_dir=cache_dir, mode=ProviderMode.OFFLINE, snapshot_prices={})
    assert off._get_submissions(123456) == {"legacy": True}

    def _transport(url: str, headers: dict[str, str], timeout: float) -> bytes:
        return json.dumps({"url": url}).encode("utf-8")

    def _worker(i: int) -> str:
        c = EdgarMarketDataProvider(
            cache_dir=cache_dir,
            mode=ProviderMode.COLLECT,
            rate_limit_sleep=0.0,
            transport=_transport,
            snapshot_prices={},
            generation=f"gen-{i}",
        )
        c.client.fetch_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{i:010d}.json")
        return f"CIK{i:010d}"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_worker, range(100, 112)))
    assert len(results) == 12
    manifest = ManifestStore(cache_dir).load()
    assert manifest is not None
    for i in range(100, 112):
        k = f"data.sec.gov_api_xbrl_companyfacts_CIK{i:010d}.json"
        assert k in manifest.entries
        assert manifest.entries[k].sha256 == sha256_bytes((cache_dir / k).read_bytes())


def test_p3_forbids_future_price_and_preserves_fy_ltm_order_and_cwd(tmp_path, monkeypatch):
    """Forbids look-ahead future price, verifies FY<->LTM commutativity, and works from any CWD."""
    store = PriceSnapshotStore(
        {
            ("FUT", date(2026, 2, 2)): (Decimal("99.00"), "Monday close after Jan 31 cover"),
        },
        offline=True,
    )
    with pytest.raises(MissingEvidenceError, match="look-ahead prices are forbidden"):
        store.lookup("FUT", date(2026, 1, 31))

    # Order invariance under default cover-gaap-v3: FY -> LTM vs LTM -> FY yield exact identical Decimal outputs
    p_a = EdgarMarketDataProvider(mode=ProviderMode.OFFLINE)
    fy_first = {d.metric: d.value for d in p_a.get_peer_financials("ADSK", "FY")}
    ltm_second = {d.metric: d.value for d in p_a.get_peer_financials("ADSK", "LTM")}

    p_b = EdgarMarketDataProvider(mode=ProviderMode.OFFLINE)
    ltm_first = {d.metric: d.value for d in p_b.get_peer_financials("ADSK", "LTM")}
    fy_second = {d.metric: d.value for d in p_b.get_peer_financials("ADSK", "FY")}
    assert fy_first == fy_second
    assert ltm_first == ltm_second

    # Running offline provider from an arbitrary working directory resolves bundled resources cleanly
    monkeypatch.chdir(tmp_path)
    p_cwd = EdgarMarketDataProvider(mode=ProviderMode.OFFLINE)
    assert p_cwd.is_verified_peer("ADSK")
    assert {d.metric: d.value for d in p_cwd.get_peer_financials("ADSK", "FY")} == fy_first


def test_p3_replay_mode_uses_only_frozen_packet_without_cache_or_network():
    """ReplayMarketDataProvider reconstructs peer financials strictly from frozen review packet."""
    prov = EdgarMarketDataProvider(mode=ProviderMode.OFFLINE)
    adbe_fy = prov.get_peer_financials("ADBE", "FY")
    packet = {
        "method_version": "cover-gaap-v2",
        "selected_peers": ["ADBE"],
        "sources": [dp.model_dump(mode="json") for dp in adbe_fy],
    }
    replay = EdgarMarketDataProvider.from_frozen_packet(packet)
    assert isinstance(replay, ReplayMarketDataProvider)
    assert replay.is_verified_peer("ADBE")
    replayed = {d.metric: d.value for d in replay.get_peer_financials("ADBE", "FY")}
    assert replayed == {d.metric: d.value for d in adbe_fy}
    with pytest.raises(OfflineCacheMissError):
        replay.get_peer_financials("CRM", "FY")
