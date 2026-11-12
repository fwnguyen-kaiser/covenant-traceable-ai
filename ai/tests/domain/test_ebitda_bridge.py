"""Tests cho EBITDA bottom-up + cờ NCI.

`ebitda_inputs()` là helper để thay cho fixture cũ vốn nạp thẳng datapoint
"ebitda" vào ledger: giờ phải nạp 4 dòng thô.
"""

from datetime import datetime
from decimal import Decimal

import pytest

from covenant_ai.domain.bridge import (
    EBITDA_INPUTS,
    NCI_MATERIALITY,
    build_peer_financials,
    compute_ebit,
    compute_ebitda,
    enterprise_value,
    flag_peer,
)
from covenant_ai.domain.errors import MissingFieldError
from covenant_ai.domain.metrics import MetricKind, MetricsRegistry
from covenant_ai.domain.models import FinancialDatapoint, PeerFinancials, SourceType

D = Decimal
NOW = datetime(2026, 11, 12, 9, 0, 0)


def dp(ticker, metric, value, period="LTM", source=SourceType.SEC_FILING):
    return FinancialDatapoint(
        metric=metric,
        value=D(str(value)),
        period=period,
        source_type=source,
        source_ref="test",
        retrieved_at=NOW,
        peer_ticker=ticker,
    )


def ebitda_inputs(ticker, income_cont, interest_net, tax, da, period="LTM"):
    """4 dòng thô để dựng EBITDA cho một peer."""
    return [
        dp(ticker, "income_continuing_ops", income_cont, period),
        dp(ticker, "interest_expense_net", interest_net, period),
        dp(ticker, "income_tax_expense", tax, period),
        dp(ticker, "depreciation_amortization", da, period),
    ]


def full_ledger(ticker="NKE", **over):
    rows = ebitda_inputs(ticker, 3108, -50, 792, 600)  # D&A 600 là số minh họa
    rows += [
        dp(ticker, "market_cap", over.get("market_cap", 100_000)),
        dp(ticker, "total_debt", over.get("total_debt", 8_000)),
        dp(ticker, "cash", over.get("cash", 9_000)),
    ]
    if "nci" in over:
        rows.append(dp(ticker, "non_controlling_interests", over["nci"]))
    return rows


# --- công thức ---------------------------------------------------------------

def test_nike_ebit_matches_10k_reconciliation():
    # Nike FY2026: NI 3,108; interest (income) expense net (50); tax 792 -> EBIT 3,850
    assert compute_ebit(D(3108), D(-50), D(792)) == D(3850)


def test_ebitda_adds_da():
    assert compute_ebitda(D(3850), D(600)) == D(4450)


def test_ebit_ebitda_are_derived_in_registry():
    assert MetricsRegistry.get("ebit").kind == MetricKind.DERIVED
    assert MetricsRegistry.get("ebitda").kind == MetricKind.DERIVED
    assert set(MetricsRegistry.get("ebitda").inputs) == {"ebit", "depreciation_amortization"}


# --- bridge ------------------------------------------------------------------

def test_bridge_computes_ebitda_from_raw():
    peer, used = build_peer_financials(full_ledger(), "NKE")
    assert peer.ebitda == D(4450)
    assert peer.ebitda_period == "LTM"


def test_bridge_provenance_includes_all_ebitda_inputs():
    ledger = full_ledger()
    peer, used = build_peer_financials(ledger, "NKE")
    by_metric = {d.metric: d.id for d in ledger}
    for m in EBITDA_INPUTS:
        assert by_metric[m] in used


def test_bridge_missing_da_raises():
    ledger = [d for d in full_ledger() if d.metric != "depreciation_amortization"]
    with pytest.raises(MissingFieldError):
        build_peer_financials(ledger, "NKE")


def test_bridge_requires_same_period():
    ledger = full_ledger()
    with pytest.raises(MissingFieldError):
        build_peer_financials(ledger, "NKE", period="FY-2025")


def test_bridge_optional_nci_defaults_to_zero():
    peer, _ = build_peer_financials(full_ledger(), "NKE")
    assert peer.non_controlling_interests == D(0)


# --- cờ NCI ------------------------------------------------------------------

def peer_with(nci, market_cap=100_000, debt=0, cash=0):
    return PeerFinancials(
        ticker="X", market_cap=D(market_cap), total_debt=D(debt), cash=D(cash),
        ebitda=D(1000), ebitda_period="LTM", non_controlling_interests=D(nci),
    )


def test_enterprise_value_includes_nci():
    assert enterprise_value(peer_with(nci=5_000)) == D(105_000)


def test_no_flag_when_nci_zero():
    assert flag_peer(peer_with(nci=0)) == {}


def test_no_flag_below_threshold():
    # 1,000 / 101,000 ~ 1% < 5%
    assert flag_peer(peer_with(nci=1_000)) == {}


def test_flag_above_threshold():
    # 10,000 / 110,000 ~ 9% > 5%
    flags = flag_peer(peer_with(nci=10_000))
    assert "nci_material" in flags


def test_threshold_constant_is_five_percent():
    assert NCI_MATERIALITY == D("0.05")


def test_flag_when_ev_not_positive():
    flags = flag_peer(peer_with(nci=100, market_cap=100, debt=0, cash=1_000))
    assert "nci_unassessable" in flags