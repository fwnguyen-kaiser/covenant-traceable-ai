"""P2 unit & parity tests for decomposed EDGAR components without HTTP or disk I/O."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from covenant_ai.domain.evidence import EvidenceStatus, MethodologyVersion
from covenant_ai.providers.datapoint_builder import PeerDatapointBuilder
from covenant_ai.providers.edgar import (
    ANNUAL_FORMS,
    EdgarMarketDataProvider,
    _load_price_snapshot_index,
    _load_price_snapshots,
)
from covenant_ai.providers.edgar_client import EdgarClient
from covenant_ai.providers.edgar_tags import PeerCatalog
from covenant_ai.providers.price_store import PriceSnapshotStore
from covenant_ai.providers.xbrl_reader import XbrlFactReader


def test_p2_xbrl_reader_and_builder_work_purely_in_memory():
    """XbrlFactReader and PeerDatapointBuilder operate cleanly on in-memory fixtures (no HTTP/disk)."""
    facts = {
        "us-gaap": {
            "IncomeLossFromContinuingOperations": {
                "units": {
                    "USD": [
                        {
                            "form": "10-K",
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "accn": "0001-26-000010",
                            "filed": "2026-02-01",
                            "val": 200,
                        },
                        {
                            "form": "10-Q",
                            "fp": "Q1",
                            "start": "2026-01-01",
                            "end": "2026-03-31",
                            "accn": "0001-26-000020",
                            "filed": "2026-04-20",
                            "val": 60,
                        },
                        {
                            "form": "10-Q",
                            "fp": "Q1",
                            "start": "2025-01-01",
                            "end": "2025-03-31",
                            "accn": "0001-26-000020",
                            "filed": "2026-04-20",
                            "val": 50,
                        },
                    ]
                }
            },
            "IncomeTaxExpenseBenefit": {
                "units": {
                    "USD": [
                        {
                            "form": "10-K",
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "accn": "0001-26-000010",
                            "filed": "2026-02-01",
                            "val": 40,
                        },
                        {
                            "form": "10-Q",
                            "fp": "Q1",
                            "start": "2026-01-01",
                            "end": "2026-03-31",
                            "accn": "0001-26-000020",
                            "filed": "2026-04-20",
                            "val": 12,
                        },
                        {
                            "form": "10-Q",
                            "fp": "Q1",
                            "start": "2025-01-01",
                            "end": "2025-03-31",
                            "accn": "0001-26-000020",
                            "filed": "2026-04-20",
                            "val": 10,
                        },
                    ]
                }
            },
            "InterestIncomeExpenseNet": {
                "units": {
                    "USD": [
                        {
                            "form": "10-K",
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "accn": "0001-26-000010",
                            "filed": "2026-02-01",
                            "val": -10,
                        },
                        {
                            "form": "10-Q",
                            "fp": "Q1",
                            "start": "2026-01-01",
                            "end": "2026-03-31",
                            "accn": "0001-26-000020",
                            "filed": "2026-04-20",
                            "val": -3,
                        },
                        {
                            "form": "10-Q",
                            "fp": "Q1",
                            "start": "2025-01-01",
                            "end": "2025-03-31",
                            "accn": "0001-26-000020",
                            "filed": "2026-04-20",
                            "val": -2,
                        },
                    ]
                }
            },
            "Depreciation": {
                "units": {
                    "USD": [
                        {
                            "form": "10-K",
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "accn": "0001-26-000010",
                            "filed": "2026-02-01",
                            "val": 20,
                        },
                        {
                            "form": "10-Q",
                            "fp": "Q1",
                            "start": "2026-01-01",
                            "end": "2026-03-31",
                            "accn": "0001-26-000020",
                            "filed": "2026-04-20",
                            "val": 6,
                        },
                        {
                            "form": "10-Q",
                            "fp": "Q1",
                            "start": "2025-01-01",
                            "end": "2025-03-31",
                            "accn": "0001-26-000020",
                            "filed": "2026-04-20",
                            "val": 5,
                        },
                    ]
                }
            },
            "AmortizationOfIntangibleAssets": {
                "units": {
                    "USD": [
                        {
                            "form": "10-K",
                            "fp": "FY",
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "accn": "0001-26-000010",
                            "filed": "2026-02-01",
                            "val": 5,
                        },
                        {
                            "form": "10-Q",
                            "fp": "Q1",
                            "start": "2026-01-01",
                            "end": "2026-03-31",
                            "accn": "0001-26-000020",
                            "filed": "2026-04-20",
                            "val": 2,
                        },
                        {
                            "form": "10-Q",
                            "fp": "Q1",
                            "start": "2025-01-01",
                            "end": "2025-03-31",
                            "accn": "0001-26-000020",
                            "filed": "2026-04-20",
                            "val": 1,
                        },
                    ]
                }
            },
            "LongTermDebtNoncurrent": {
                "units": {
                    "USD": [
                        {"form": "10-K", "end": "2025-12-31", "accn": "0001-26-000010", "filed": "2026-02-01", "val": 80},
                        {"form": "10-Q", "end": "2026-03-31", "accn": "0001-26-000020", "filed": "2026-04-20", "val": 75},
                    ]
                }
            },
            "CashAndCashEquivalentsAtCarryingValue": {
                "units": {
                    "USD": [
                        {"form": "10-K", "end": "2025-12-31", "accn": "0001-26-000010", "filed": "2026-02-01", "val": 30},
                        {"form": "10-Q", "end": "2026-03-31", "accn": "0001-26-000020", "filed": "2026-04-20", "val": 35},
                    ]
                }
            },
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        {"form": "10-K", "end": "2026-01-15", "accn": "0001-26-000010", "val": 10},
                        {"form": "10-Q", "end": "2026-04-15", "accn": "0001-26-000020", "val": 10},
                    ]
                }
            }
        },
    }
    store = PriceSnapshotStore(
        {
            ("MEM", date(2026, 1, 15)): (Decimal("50"), "in-memory-fy"),
            ("MEM", date(2026, 4, 15)): (Decimal("55"), "in-memory-q1"),
        }
    )
    fixed_clock = lambda: datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    reader = XbrlFactReader()
    builder = PeerDatapointBuilder(reader, store.lookup, clock=fixed_clock)

    fy_pts = {
        d.metric: d
        for d in builder.build_fy(
            "MEM",
            facts["us-gaap"],
            facts["dei"],
            required_end="2025-12-31",
            method_version=MethodologyVersion.V3,
        )
    }
    assert fy_pts["market_cap"].value == Decimal("500")
    assert fy_pts["total_debt"].value == Decimal("80")
    assert fy_pts["interest_expense_net"].value == Decimal("10")
    assert fy_pts["depreciation_amortization"].value == Decimal("25")
    assert fy_pts["market_cap"].retrieved_at == fixed_clock()

    ltm_pts = {
        d.metric: d
        for d in builder.build_ltm(
            "MEM",
            facts["us-gaap"],
            facts["dei"],
            required_fy="2025-12-31",
            latest_q="2026-03-31",
            method_version=MethodologyVersion.V3,
        )
    }
    assert ltm_pts["income_continuing_ops"].value == Decimal("210")  # 200 + 60 - 50
    assert ltm_pts["depreciation_amortization"].value == Decimal("27")  # 25 + 8 - 6
    assert ltm_pts["total_debt"].value == Decimal("75")
    assert ltm_pts["market_cap"].value == Decimal("550")


def test_p2_price_store_separates_observations_and_cover_links_without_network():
    """Two distinct non-trading cover dates sharing one Friday session retain distinct provenance."""
    store = PriceSnapshotStore(allow_on_demand_yahoo=False)
    store.record_observation(
        "DUO",
        date(2026, 1, 30),
        Decimal("49.50"),
        "Yahoo close; prior_trading_day_for_non_trading_cover_date=2026-01-31",
        cover_date=date(2026, 1, 31),
    )
    store.record_observation(
        "DUO",
        date(2026, 1, 30),
        Decimal("49.50"),
        "Yahoo close; prior_trading_day_for_non_trading_cover_date=2026-02-01",
        cover_date=date(2026, 2, 1),
    )
    px_sat, dt_sat, ref_sat = store.lookup("DUO", date(2026, 1, 31))
    px_sun, dt_sun, ref_sun = store.lookup("DUO", date(2026, 2, 1))
    assert (px_sat, dt_sat) == (Decimal("49.50"), date(2026, 1, 30))
    assert (px_sun, dt_sun) == (Decimal("49.50"), date(2026, 1, 30))
    assert "2026-01-31" in ref_sat
    assert "2026-02-01" in ref_sun


def test_p2_edgar_client_transport_injection_and_throttle_compliance(tmp_path):
    """EdgarClient enforces >=0.15s SEC CDN / >=0.35s browse-edgar sleeps with injected transport."""
    sleeps: list[float] = []
    calls: list[str] = []

    def _fake_transport(url: str, headers: dict[str, str], timeout: float) -> bytes:
        calls.append(url)
        assert "User-Agent" in headers
        return b'{"ok": 1}'

    client = EdgarClient(
        cache_dir=tmp_path,
        user_agent="TestAgent a@b.com",
        rate_limit_sleep=0.01,  # intentionally below 0.15s floor
        browse_rate_limit_sleep=0.10,  # intentionally below 0.35s floor
        transport=_fake_transport,
        sleeper=sleeps.append,
    )
    res = client.fetch_json("https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json")
    assert res == {"ok": Decimal("1")}
    assert sleeps[-1] >= 0.15
    client.fetch_bytes(
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&SIC=7372&output=atom",
        cache_name="browse.xml",
    )
    assert sleeps[-1] >= 0.35


def test_p2_catalog_caches_validated_parse_and_legacy_snapshot_wrappers(tmp_path):
    """PeerCatalog parses once until explicit reload or file update; snapshot helpers keep parity."""
    csv_file = tmp_path / "snaps.csv"
    csv_file.write_text(
        "ticker,as_of_date,close_price,source_ref\n"
        "AAA,2026-01-10,12.34,Yahoo AAA old\n"
        "AAA,2026-02-10,15.67,Yahoo AAA new\n",
        encoding="utf-8",
    )
    idx = _load_price_snapshot_index(csv_file)
    assert len(idx) == 2
    prices, dates, refs = _load_price_snapshots(csv_file)
    assert prices["AAA"] == Decimal("15.67")
    assert dates["AAA"] == date(2026, 2, 10)
    assert refs["AAA"] == "Yahoo AAA new"

    cat_path = tmp_path / "cat.json"
    cat_path.write_text(
        json.dumps({"industries": {"software": [{"ticker": "AAA", "verified": True, "exchange": "Nasdaq"}]}}),
        encoding="utf-8",
    )
    cat = PeerCatalog(cat_path)
    assert cat.get_candidate_peers("software") == ["AAA"]
    cat_path.write_text(
        json.dumps({"industries": {"software": [{"ticker": "BBB", "verified": True, "exchange": "NYSE"}]}}),
        encoding="utf-8",
    )
    cat.reload()
    assert cat.get_candidate_peers("software") == ["BBB"]


def test_p2_fresh_process_dependency_direction():
    """Verify domain layer does not import providers and all refactored modules import cleanly."""
    code = (
        "import sys; "
        "import covenant_ai.domain.bridge, covenant_ai.domain.valuation, covenant_ai.domain.evidence; "
        "loaded_providers = [m for m in sys.modules if m.startswith('covenant_ai.providers')]; "
        "assert not loaded_providers, f'domain imported providers: {loaded_providers}'; "
        "import covenant_ai.providers.edgar, covenant_ai.cli, covenant_ai.scripts.discover_peers; "
        "print('OK')"
    )
    ai_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ai_root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "OK" in proc.stdout
