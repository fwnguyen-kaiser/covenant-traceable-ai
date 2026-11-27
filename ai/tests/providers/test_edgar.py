import json
from datetime import date
from decimal import Decimal
import pytest

from covenant_ai.domain.bridge import build_peer_financials
from covenant_ai.domain.models import SourceType
from covenant_ai.domain.valuation import enterprise_value, ev_ebitda
from covenant_ai.providers.edgar import EdgarMarketDataProvider


@pytest.fixture
def edgar_provider(edgar_provider_factory):
    return edgar_provider_factory()


def test_edgar_provider_loads_adbe_from_cache(edgar_provider_factory):
    """Verify loading real SEC 10-K data for Adobe (ADBE) from local cache."""
    pts = edgar_provider_factory("ADBE").get_peer_financials("ADBE", period="FY")
    assert len(pts) >= 7

    by_metric = {d.metric: d for d in pts}
    assert "market_cap" in by_metric
    assert "total_debt" in by_metric
    assert "cash" in by_metric
    assert "income_continuing_ops" in by_metric
    assert "interest_expense_net" in by_metric
    assert "income_tax_expense" in by_metric
    assert "depreciation_amortization" in by_metric

    # Check provenance
    assert by_metric["income_continuing_ops"].source_type == SourceType.SEC_FILING
    assert "SEC 10-K" in by_metric["income_continuing_ops"].source_ref
    assert by_metric["market_cap"].value > 0

    # Build peer financials through bridge and verify EV & EBITDA
    peer, used_ids = build_peer_financials(pts, "ADBE", period="FY")
    assert peer.ticker == "ADBE"
    assert peer.ebitda > Decimal("8000000000")  # Adobe EBITDA is ~9.8B
    assert peer.cash > Decimal("4000000000")

    ev = enterprise_value(peer)
    assert ev > 0
    multiple = ev_ebitda(peer)
    # Adobe EV/EBITDA multiple is reasonable (between 8x and 35x)
    assert Decimal("8") <= multiple <= Decimal("35")


def test_edgar_provider_loads_crm_from_cache(edgar_provider_factory):
    """Verify loading real SEC 10-K data for Salesforce (CRM) from cache."""
    pts = edgar_provider_factory("CRM").get_peer_financials("CRM", period="FY")
    peer, used_ids = build_peer_financials(pts, "CRM", period="FY")

    assert peer.ticker == "CRM"
    assert peer.ebitda > Decimal("10000000000")  # CRM EBITDA is ~11B
    ev = enterprise_value(peer)
    assert ev > 0
    multiple = ev_ebitda(peer)
    assert Decimal("10") <= multiple <= Decimal("35")


def test_edgar_provider_composite_da_intuit(edgar_provider_factory):
    """Verify Intuit (INTU) composite D&A (Depreciation + AmortizationOfIntangibles)."""
    pts = edgar_provider_factory("INTU").get_peer_financials("INTU", period="FY")
    by_metric = {d.metric: d for d in pts}

    # Composite D&A should be Depreciation (187M) + Amortization (485M) = 672M
    da_pt = by_metric["depreciation_amortization"]
    assert da_pt.value == Decimal("672000000")
    assert "Depreciation+AmortizationOfIntangibleAssets" in da_pt.source_ref

    peer, _ = build_peer_financials(pts, "INTU", period="FY")
    assert peer.ebitda > 0


@pytest.mark.parametrize(
    ("ticker", "expected_period_end"),
    [("FLEX", date(2026, 3, 31))],
)
def test_edgar_provider_composite_da_fills_latest_missing_combined_period(
    edgar_provider_factory, ticker, expected_period_end
):
    pts = edgar_provider_factory(ticker).get_peer_financials(ticker, period="FY")
    by_metric = {point.metric: point for point in pts}
    peer, _ = build_peer_financials(pts, ticker, period="FY")

    assert peer.period_end == expected_period_end
    assert "Depreciation+AmortizationOfIntangibleAssets" in by_metric[
        "depreciation_amortization"
    ].source_ref


def test_edgar_provider_market_cap_uses_cover_shares_times_dated_close(edgar_provider_factory):
    """Market cap uses latest 10-K shares times closing price on the share-count date."""
    closing_price = Decimal("500")
    provider = edgar_provider_factory("ADBE", price=closing_price)
    pts = provider.get_peer_financials("ADBE", period="FY")
    by_metric = {d.metric: d for d in pts}

    assert by_metric["market_cap"].value == provider.test_share_counts["ADBE"] * closing_price
    assert "shares" in by_metric["market_cap"].source_ref
    assert "closing price $500" in by_metric["market_cap"].source_ref
    assert "synthetic test fixture price" in by_metric["market_cap"].source_ref


def test_edgar_provider_requires_a_closing_price_snapshot():
    provider = EdgarMarketDataProvider(snapshot_prices={})

    with pytest.raises(KeyError, match="no closing-price snapshot"):
        provider.get_peer_financials("ADBE", period="FY")


def test_edgar_provider_rejects_ticker_without_latest_10k_share_fact():
    provider = EdgarMarketDataProvider(snapshot_prices={})

    with pytest.raises(KeyError, match="no shares-outstanding fact in the latest 10-K"):
        provider.get_peer_financials("PTC", period="FY")


def test_edgar_provider_unknown_ticker_raises_key_error(edgar_provider):
    """Verify unknown ticker raises KeyError."""
    with pytest.raises(KeyError, match="not found in SEC company tickers"):
        edgar_provider.get_peer_financials("NONEXISTENT_TICKER_123")


def test_edgar_provider_inverts_net_interest_tags(edgar_provider_factory):
    """Verify net interest XBRL tags (negative=expense, positive=income) are inverted
    to match bridge.py's expense-positive convention, including InterestIncomeExpenseNonoperatingNet.
    """
    provider = edgar_provider_factory("WWW", "MANH", "GIS")

    www_pts = {d.metric: d for d in provider.get_peer_financials("WWW", period="FY")}
    assert www_pts["interest_expense_net"].value == Decimal("32800000")
    assert "sign inverted" in www_pts["interest_expense_net"].source_ref
    www_peer, _ = build_peer_financials(list(www_pts.values()), "WWW", period="FY")
    assert www_peer.ebitda == Decimal("180200000")

    manh_pts = {d.metric: d for d in provider.get_peer_financials("MANH", period="FY")}
    assert manh_pts["interest_expense_net"].value == Decimal("-4389000")
    assert "sign inverted" in manh_pts["interest_expense_net"].source_ref

    gis_pts = {d.metric: d for d in provider.get_peer_financials("GIS", period="FY")}
    assert gis_pts["interest_expense_net"].value == Decimal("538600000")
    assert "InterestIncomeExpenseNonoperatingNet" in gis_pts["interest_expense_net"].source_ref
    assert "sign inverted" in gis_pts["interest_expense_net"].source_ref


def test_edgar_provider_debt_no_double_count_and_cap_lease(edgar_provider_factory):
    """Verify mutually exclusive debt + lease extraction across differing XBRL filings."""
    provider = edgar_provider_factory("CRM", "CPB", "DXC", "LII", "YUM")

    # CRM: LongTermDebtNoncurrent (10.439B) + LongTermDebtCurrent (4B) + leases (3.272B), no 4B double count
    crm_pts = {d.metric: d for d in provider.get_peer_financials("CRM", period="FY")}
    assert crm_pts["total_debt"].value == Decimal("17711000000")

    # CPB: includes LongTermDebtAndCapitalLeaseObligations (6.16B) + DebtCurrent (977M) + op leases (313M)
    cpb_pts = {d.metric: d for d in provider.get_peer_financials("CPB", period="FY")}
    assert cpb_pts["total_debt"].value == Decimal("7450000000")

    # DXC: includes LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities (3.552B) + op leases (695M)
    dxc_pts = {d.metric: d for d in provider.get_peer_financials("DXC", period="FY")}
    assert dxc_pts["total_debt"].value == Decimal("4247000000")

    # LII: includes CommercialPaper (226M) + LongTermDebtAndCapitalLeaseObligations (1.1441B) + current (18.3M) + op leases (382.3M)
    lii_pts = {d.metric: d for d in provider.get_peer_financials("LII", period="FY")}
    assert lii_pts["total_debt"].value == Decimal("1770700000")

    # YUM: includes combined OperatingLeaseLiability (1.279B) + ShortTermBorrowings (38M) + LT debt/cap lease (11.976B)
    yum_pts = {d.metric: d for d in provider.get_peer_financials("YUM", period="FY")}
    assert yum_pts["total_debt"].value == Decimal("13293000000")


def test_edgar_provider_cash_sti_not_double_counted(monkeypatch):
    """If cash resolves to CashCashEquivalentsAndShortTermInvestments, do not add ShortTermInvestments again."""
    provider = EdgarMarketDataProvider(
        snapshot_prices={"ADBE": Decimal("100")},
        snapshot_price_dates={"ADBE": date(2026, 1, 9)},
        snapshot_price_source_refs={"ADBE": "synthetic"},
    )
    synthetic_facts = {
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K",
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "accn": "0001-26-000001",
                                "filed": "2026-02-01",
                                "val": 100,
                            }
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
                                "accn": "0001-26-000001",
                                "filed": "2026-02-01",
                                "val": 20,
                            }
                        ]
                    }
                },
                "InterestExpense": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K",
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "accn": "0001-26-000001",
                                "filed": "2026-02-01",
                                "val": 10,
                            }
                        ]
                    }
                },
                "DepreciationAndAmortization": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K",
                                "fp": "FY",
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "accn": "0001-26-000001",
                                "filed": "2026-02-01",
                                "val": 30,
                            }
                        ]
                    }
                },
                "CashCashEquivalentsAndShortTermInvestments": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K",
                                "end": "2025-12-31",
                                "accn": "0001-26-000001",
                                "filed": "2026-02-01",
                                "val": 500,
                            }
                        ]
                    }
                },
                "ShortTermInvestments": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-K",
                                "end": "2025-12-31",
                                "accn": "0001-26-000001",
                                "filed": "2026-02-01",
                                "val": 200,
                            }
                        ]
                    }
                },
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "form": "10-K",
                                "end": "2026-01-09",
                                "accn": "0001-26-000001",
                                "val": 10,
                            }
                        ]
                    }
                }
            },
        }
    }
    monkeypatch.setattr(provider, "_get_submissions", lambda cik: {"filings": {"recent": {
        "form": ["10-K", "10-Q"], "reportDate": ["2025-12-31", "2026-06-30"], "accessionNumber": ["01", "02"],
    }}})
    monkeypatch.setattr(provider, "_get_company_facts", lambda cik: synthetic_facts)
    pts = {d.metric: d for d in provider.get_peer_financials("ADBE", period="FY")}
    assert pts["cash"].value == Decimal("500")


def test_edgar_provider_composite_snapshot_index_supports_multiple_dates(tmp_path):
    """Distinct (ticker, as_of_date) snapshots coexist for FY and LTM without clobbering."""
    csv_file = tmp_path / "snapshots.csv"
    csv_file.write_text(
        "ticker,as_of_date,close_price,source_ref\n"
        "ADBE,2026-01-09,333.95,Yahoo FY close\n"
        "ADBE,2026-06-15,350.00,Yahoo MRQ close\n",
        encoding="utf-8",
    )
    provider = EdgarMarketDataProvider(snapshot_csv=csv_file)
    p_fy, d_fy, r_fy = provider._lookup_price_snapshot("ADBE", date(2026, 1, 9), form_label="10-K")
    p_ltm, d_ltm, r_ltm = provider._lookup_price_snapshot("ADBE", date(2026, 6, 15), form_label="10-Q")
    assert p_fy == Decimal("333.95")
    assert d_fy == date(2026, 1, 9)
    assert r_fy == "Yahoo FY close"
    assert p_ltm == Decimal("350.00")
    assert d_ltm == date(2026, 6, 15)
    assert r_ltm == "Yahoo MRQ close"
    # Convenience attribute retains most recent row
    assert provider.snapshot_prices["ADBE"] == Decimal("350.00")

    # Non-trading weekend/holiday fallback (<=5d prior) works cleanly with marker,
    # whereas an arbitrary off-date on a normal weekday without marker is rejected.
    holiday_csv = tmp_path / "holiday_snapshots.csv"
    holiday_csv.write_text(
        "ticker,as_of_date,close_price,source_ref\n"
        "ROL,2026-01-30,49.50,Yahoo close; prior_trading_day_for_non_trading_cover_date=2026-01-31\n"
        "ADBE,2026-01-08,330.00,Manual off-date close\n",
        encoding="utf-8",
    )
    h_prov = EdgarMarketDataProvider(snapshot_csv=holiday_csv)
    hp, hd, _ = h_prov._lookup_price_snapshot("ROL", date(2026, 1, 31), form_label="10-K")
    assert hp == Decimal("49.50")
    assert hd == date(2026, 1, 30)
    with pytest.raises(KeyError, match="does not match"):
        h_prov._lookup_price_snapshot("ADBE", date(2026, 1, 9), form_label="10-K")

    dup_csv = tmp_path / "dup_snapshots.csv"
    dup_csv.write_text(
        "ticker,as_of_date,close_price,source_ref\n"
        "ADBE,2026-01-09,333.95,Yahoo A\n"
        "ADBE,2026-01-09,334.00,Yahoo B\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate market-price snapshot for ADBE on 2026-01-09"):
        EdgarMarketDataProvider(snapshot_csv=dup_csv)


def test_edgar_provider_loads_ticker_map_with_exchange_cache_present(tmp_path):
    """Having sec.gov_files_company_tickers_exchange.json in cache must not break ticker map."""
    (tmp_path / "sec.gov_files_company_tickers_exchange.json").write_text(
        '{"fields":["cik","name","ticker","exchange"],"data":[[796343,"ADOBE INC.","ADBE","Nasdaq"]]}',
        encoding="utf-8",
    )
    (tmp_path / "sec.gov_files_company_tickers.json").write_text(
        '{"0":{"cik_str":796343,"ticker":"ADBE","title":"ADOBE INC."}}',
        encoding="utf-8",
    )
    p = EdgarMarketDataProvider(cache_dir=tmp_path, snapshot_prices={})
    assert p._ticker_to_cik["ADBE"] == 796343

    # Even if only the exchange schema file exists, parser handles both formats
    (tmp_path / "sec.gov_files_company_tickers.json").unlink()
    p2 = EdgarMarketDataProvider(cache_dir=tmp_path, snapshot_prices={})
    assert p2._ticker_to_cik["ADBE"] == 796343


def test_edgar_provider_real_ltm_calendarization_and_10k_mrq_case():
    """Verify real SEC LTM = FY + Curr_YTD - Prior_YTD (ADBE Q3) and FY==LTM (SBUX 10-K >= 10-Q)."""
    provider = EdgarMarketDataProvider()
    adbe_ltm = {d.metric: d for d in provider.get_peer_financials("ADBE", period="LTM")}
    assert adbe_ltm["income_continuing_ops"].period == "LTM"
    assert adbe_ltm["income_continuing_ops"].period_end == date(2026, 8, 28)
    # 7130M + 5428M - 5274M = 7284M
    assert adbe_ltm["income_continuing_ops"].value == Decimal("7284000000")
    assert "10-Q" in adbe_ltm["market_cap"].source_ref
    adbe_peer, _ = build_peer_financials(list(adbe_ltm.values()), "ADBE", period="LTM")
    assert adbe_peer.ebitda > Decimal("9000000000")

    sbux_fy = {d.metric: d for d in provider.get_peer_financials("SBUX", period="FY")}
    sbux_ltm = {d.metric: d for d in provider.get_peer_financials("SBUX", period="LTM")}
    assert sbux_ltm["income_continuing_ops"].value == sbux_fy["income_continuing_ops"].value
    assert sbux_ltm["market_cap"].value == sbux_fy["market_cap"].value
    assert "no subsequent 10-Q" in sbux_ltm["income_continuing_ops"].source_ref


def test_edgar_provider_ltm_rejects_standalone_quarter_and_mixed_tags(monkeypatch):
    """LTM must use cumulative YTD frames (not ~90d standalone Q2) and same-tag series (FIN-05)."""
    provider = EdgarMarketDataProvider(
        snapshot_prices={"ADBE": Decimal("100")},
        snapshot_price_dates={"ADBE": date(2026, 7, 10)},
        snapshot_price_source_refs={"ADBE": "synthetic"},
    )
    facts_with_standalone_and_ytd = {
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {"form": "10-K", "fp": "FY", "start": "2025-01-01", "end": "2025-12-31", "accn": "01", "filed": "2026-02-01", "val": 1000},
                            # Standalone 90d Q2 MUST be ignored in favor of 181d cumulative YTD:
                            {"form": "10-Q", "fp": "Q2", "start": "2026-04-01", "end": "2026-06-30", "accn": "02", "filed": "2026-07-15", "val": 999999},
                            {"form": "10-Q", "fp": "Q2", "start": "2026-01-01", "end": "2026-06-30", "accn": "02", "filed": "2026-07-15", "val": 600},
                            {"form": "10-Q", "fp": "Q2", "start": "2025-04-01", "end": "2025-06-30", "accn": "02", "filed": "2026-07-15", "val": 888888},
                            {"form": "10-Q", "fp": "Q2", "start": "2025-01-01", "end": "2025-06-30", "accn": "02", "filed": "2026-07-15", "val": 500},
                        ]
                    }
                },
                "IncomeTaxExpenseBenefit": {
                    "units": {
                        "USD": [
                            {"form": "10-K", "fp": "FY", "start": "2025-01-01", "end": "2025-12-31", "accn": "01", "filed": "2026-02-01", "val": 200},
                            {"form": "10-Q", "fp": "Q2", "start": "2026-01-01", "end": "2026-06-30", "accn": "02", "filed": "2026-07-15", "val": 110},
                            {"form": "10-Q", "fp": "Q2", "start": "2025-01-01", "end": "2025-06-30", "accn": "02", "filed": "2026-07-15", "val": 100},
                        ]
                    }
                },
                "InterestExpense": {
                    "units": {
                        "USD": [
                            {"form": "10-K", "fp": "FY", "start": "2025-01-01", "end": "2025-12-31", "accn": "01", "filed": "2026-02-01", "val": 50},
                            {"form": "10-Q", "fp": "Q2", "start": "2026-01-01", "end": "2026-06-30", "accn": "02", "filed": "2026-07-15", "val": 30},
                            {"form": "10-Q", "fp": "Q2", "start": "2025-01-01", "end": "2025-06-30", "accn": "02", "filed": "2026-07-15", "val": 25},
                        ]
                    }
                },
                "DepreciationAndAmortization": {
                    "units": {
                        "USD": [
                            {"form": "10-K", "fp": "FY", "start": "2025-01-01", "end": "2025-12-31", "accn": "01", "filed": "2026-02-01", "val": 100},
                            {"form": "10-Q", "fp": "Q2", "start": "2026-01-01", "end": "2026-06-30", "accn": "02", "filed": "2026-07-15", "val": 60},
                            {"form": "10-Q", "fp": "Q2", "start": "2025-01-01", "end": "2025-06-30", "accn": "02", "filed": "2026-07-15", "val": 50},
                        ]
                    }
                },
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {"USD": [{"form": "10-Q", "end": "2026-06-30", "accn": "02", "filed": "2026-07-15", "val": 400}]}
                },
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {"shares": [{"form": "10-Q", "end": "2026-07-10", "accn": "02", "val": 10}]}
                }
            },
        }
    }
    monkeypatch.setattr(provider, "_get_submissions", lambda cik: {"filings": {"recent": {
        "form": ["10-K", "10-Q"], "reportDate": ["2025-12-31", "2026-06-30"], "accessionNumber": ["01", "02"],
    }}})
    monkeypatch.setattr(provider, "_get_company_facts", lambda cik: facts_with_standalone_and_ytd)
    ltm = {d.metric: d for d in provider.get_peer_financials("ADBE", period="LTM")}
    assert ltm["income_continuing_ops"].value == Decimal("1100")  # 1000 + 600 - 500, NOT 999999

    # Remove NetIncomeLoss 10-K FY and put it on ProfitLoss only -> mixed-tag guard must reject
    facts_mixed = json.loads(json.dumps(facts_with_standalone_and_ytd))
    fy_ni = facts_mixed["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"].pop(0)
    facts_mixed["facts"]["us-gaap"]["ProfitLoss"] = {"units": {"USD": [fy_ni]}}
    monkeypatch.setattr(provider, "_get_company_facts", lambda cik: facts_mixed)
    with pytest.raises(KeyError, match="lacks matched cumulative YTD/FY"):
        provider.get_peer_financials("ADBE", period="LTM")


def test_edgar_provider_picks_10ka_and_10qa_restatements_including_nabl():
    """Amended filings (10-K/A, 10-Q/A) supersede earlier unamended 10-K/10-Q facts (FIN-04 #5)."""
    provider = EdgarMarketDataProvider()
    nabl_fy = {d.metric: d for d in provider.get_peer_financials("NABL", period="FY")}
    # NABL FY 2025-12-31 10-K/A (filed 2026-08-10, accn 0001834488-26-000046) restated NetIncomeLoss to -$18,815,000
    assert nabl_fy["income_continuing_ops"].value == Decimal("-18815000")
    assert "0001834488-26-000046" in nabl_fy["income_continuing_ops"].source_ref


def test_edgar_provider_restated_10qa_overrides_orig_accn_and_ltm_da_parity(monkeypatch):
    """10-Q/A restating cash/flows overrides older 10-Q even if anchor q_accn matches;
    LTM composite D&A requires matching fy_accn, accn, prior_accn, and prior_end.
    """
    provider = EdgarMarketDataProvider(
        snapshot_prices={"ADBE": Decimal("100")},
        snapshot_price_dates={"ADBE": date(2026, 7, 10)},
        snapshot_price_source_refs={"ADBE": "synthetic"},
    )
    facts = {
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {"form": "10-K", "fp": "FY", "start": "2025-01-01", "end": "2025-12-31", "accn": "fy-01", "filed": "2026-02-01", "val": 1000},
                            {"form": "10-K/A", "fp": "FY", "start": "2025-01-01", "end": "2025-12-31", "accn": "fy-01a", "filed": "2026-03-01", "val": 1200},
                            {"form": "10-Q", "fp": "Q2", "start": "2026-01-01", "end": "2026-06-30", "accn": "q-02", "filed": "2026-07-15", "val": 600},
                            {"form": "10-Q", "fp": "Q2", "start": "2025-01-01", "end": "2025-06-30", "accn": "q-02", "filed": "2026-07-15", "val": 500},
                        ]
                    }
                },
                "IncomeTaxExpenseBenefit": {
                    "units": {
                        "USD": [
                            {"form": "10-K", "fp": "FY", "start": "2025-01-01", "end": "2025-12-31", "accn": "fy-01", "filed": "2026-02-01", "val": 200},
                            {"form": "10-Q", "fp": "Q2", "start": "2026-01-01", "end": "2026-06-30", "accn": "q-02", "filed": "2026-07-15", "val": 110},
                            {"form": "10-Q", "fp": "Q2", "start": "2025-01-01", "end": "2025-06-30", "accn": "q-02", "filed": "2026-07-15", "val": 100},
                        ]
                    }
                },
                "InterestExpense": {
                    "units": {
                        "USD": [
                            {"form": "10-K", "fp": "FY", "start": "2025-01-01", "end": "2025-12-31", "accn": "fy-01", "filed": "2026-02-01", "val": 50},
                            {"form": "10-Q", "fp": "Q2", "start": "2026-01-01", "end": "2026-06-30", "accn": "q-02", "filed": "2026-07-15", "val": 30},
                            {"form": "10-Q", "fp": "Q2", "start": "2025-01-01", "end": "2025-06-30", "accn": "q-02", "filed": "2026-07-15", "val": 25},
                        ]
                    }
                },
                "Depreciation": {
                    "units": {
                        "USD": [
                            {"form": "10-K", "fp": "FY", "start": "2025-01-01", "end": "2025-12-31", "accn": "fy-01", "filed": "2026-02-01", "val": 80},
                            {"form": "10-Q", "fp": "Q2", "start": "2026-01-01", "end": "2026-06-30", "accn": "q-02", "filed": "2026-07-15", "val": 40},
                            {"form": "10-Q", "fp": "Q2", "start": "2025-01-01", "end": "2025-06-30", "accn": "q-02", "filed": "2026-07-15", "val": 35},
                        ]
                    }
                },
                "AmortizationOfIntangibleAssets": {
                    "units": {
                        "USD": [
                            {"form": "10-K", "fp": "FY", "start": "2025-01-01", "end": "2025-12-31", "accn": "fy-01", "filed": "2026-02-01", "val": 20},
                            {"form": "10-Q", "fp": "Q2", "start": "2026-01-01", "end": "2026-06-30", "accn": "q-02", "filed": "2026-07-15", "val": 20},
                            {"form": "10-Q", "fp": "Q2", "start": "2025-01-01", "end": "2025-06-30", "accn": "q-02", "filed": "2026-07-15", "val": 15},
                        ]
                    }
                },
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {
                        "USD": [
                            {"form": "10-Q", "end": "2026-06-30", "accn": "q-02", "filed": "2026-07-15", "val": 400},
                            # Newer 10-Q/A must beat older q-02 accn match!
                            {"form": "10-Q/A", "end": "2026-06-30", "accn": "q-02a", "filed": "2026-08-01", "val": 450},
                        ]
                    }
                },
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {"shares": [{"form": "10-Q", "end": "2026-07-10", "accn": "q-02", "val": 10}]}
                }
            },
        }
    }
    monkeypatch.setattr(provider, "_get_submissions", lambda cik: {"filings": {"recent": {
        "form": ["10-K", "10-Q"], "reportDate": ["2025-12-31", "2026-06-30"], "accessionNumber": ["01", "02"],
    }}})
    monkeypatch.setattr(provider, "_get_company_facts", lambda cik: facts)
    ltm = {d.metric: d for d in provider.get_peer_financials("ADBE", period="LTM")}
    assert ltm["income_continuing_ops"].value == Decimal("1300")  # 1200 (10-K/A) + 600 - 500
    assert ltm["cash"].value == Decimal("450")  # 10-Q/A superseded 10-Q despite q_accn == "q-02"
    assert ltm["depreciation_amortization"].value == Decimal("110")  # (80+20) + (40+20) - (35+15)

    # Break LTM composite D&A accession parity between Depreciation and Amortization -> must reject
    facts_mismatch = json.loads(json.dumps(facts))
    facts_mismatch["facts"]["us-gaap"]["AmortizationOfIntangibleAssets"]["units"]["USD"][1]["accn"] = "q-other"
    monkeypatch.setattr(provider, "_get_company_facts", lambda cik: facts_mismatch)
    with pytest.raises(KeyError, match="lacks matched cumulative YTD/FY"):
        provider.get_peer_financials("ADBE", period="LTM")


def test_edgar_price_fallback_rejects_trading_day_and_uses_decimal(tmp_path):
    """Prior-day close fallback works on NYSE holidays (2026-02-16) and rejects trading weekdays."""
    import ast
    from inspect import getsource
    from covenant_ai.providers.market_calendar import parse_yahoo_chart_closes

    # 1. Zero-float Yahoo parser preserves Decimal cents cleanly from float-tailed JSON
    tree = ast.parse(getsource(parse_yahoo_chart_closes))
    assert not any(isinstance(n, ast.Name) and n.id == "float" for n in ast.walk(tree))
    raw = json.dumps({
        "chart": {
            "result": [{
                "timestamp": [1770998400, 1771344000],  # 2026-02-13 (Fri), 2026-02-17 (Tue) in EST
                "indicators": {"quote": [{"close": [79.80000305175781, 81.25]}]},
            }]
        }
    }).encode("utf-8")
    parsed = parse_yahoo_chart_closes(raw, "TEST", "https://example.com/chart/TEST")
    assert parsed[date(2026, 2, 13)][0] == Decimal("79.80")
    assert parsed[date(2026, 2, 17)][0] == Decimal("81.25")

    # 2. CSV _lookup_price_snapshot allows Presidents' Day 2026-02-16 fallback, rejects Tuesday 2026-02-17 marker
    csv_file = tmp_path / "snaps.csv"
    csv_file.write_text(
        "ticker,as_of_date,close_price,source_ref\n"
        "HOL,2026-02-13,50.00,Yahoo close; prior_trading_day_for_non_trading_cover_date=2026-02-16\n"
        "TRD,2026-02-13,50.00,Yahoo close; prior_trading_day_for_non_trading_cover_date=2026-02-17\n",
        encoding="utf-8",
    )
    prov = EdgarMarketDataProvider(snapshot_csv=csv_file, allow_on_demand_yahoo=False)
    px, dt, _ = prov._lookup_price_snapshot("HOL", date(2026, 2, 16), form_label="10-K")
    assert (px, dt) == (Decimal("50.00"), date(2026, 2, 13))
    with pytest.raises(KeyError, match="does not match"):
        prov._lookup_price_snapshot("TRD", date(2026, 2, 17), form_label="10-K")

    # 3. On-demand Yahoo fetch with SPY tape rejects fallback when SPY traded on cover_date
    prov2 = EdgarMarketDataProvider(snapshot_prices={}, allow_on_demand_yahoo=True)
    prov2._spy_trading_dates = {date(2026, 2, 13), date(2026, 2, 17)}
    prov2._fetch_yahoo_chart_closes = lambda t: {date(2026, 2, 13): (Decimal("50.00"), "Yahoo TEST")}
    # Tuesday 2026-02-17 was active on SPY -> must refuse prior Friday 2026-02-13 close
    assert prov2._fetch_on_demand_yahoo_close("MISS", date(2026, 2, 17)) is None
    # Monday 2026-02-16 absent from SPY -> allowed cleanly
    resolved = prov2._fetch_on_demand_yahoo_close("MISS", date(2026, 2, 16))
    assert resolved is not None and resolved[0] == Decimal("50.00") and resolved[1] == date(2026, 2, 13)
