"""US-GAAP / DEI XBRL tag registries and validated peer catalog loader (P2)."""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from covenant_ai.domain.evidence import FailureReasonCode, FailureStage, ProviderFailure
from covenant_ai.providers.edgar_errors import CatalogError

ANNUAL_FORMS: tuple[str, ...] = ("10-K", "10-K/A")
QUARTERLY_FORMS: tuple[str, ...] = ("10-Q", "10-Q/A")
MAJOR_US_EXCHANGES: frozenset[str] = frozenset({"NYSE", "Nasdaq", "NYSE American", "CBOE"})

# Candidate tags in priority order
TAG_INCOME_CONTINUING: tuple[str, ...] = (
    "IncomeLossFromContinuingOperations",
    "IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest",
    "ProfitLoss",
    "NetIncomeLoss",
)

# Gross interest expense tags (positive = expense, matching bridge.py convention).
TAG_INTEREST_EXPENSE_GROSS: tuple[str, ...] = (
    "InterestExpense",
    "InterestExpenseNonoperating",
    "InterestExpenseDebt",
    "InterestExpenseLongTermDebt",
    "InterestAndDebtExpense",
    "InterestExpenseOther",
    "InterestExpenseOperating",
)

# US-GAAP net interest tags have credit balance (positive = net interest income,
# negative = net interest expense) and must be negated to match bridge.py.
TAG_INTEREST_INCOME_EXPENSE_NET: tuple[str, ...] = (
    "InterestIncomeExpenseNet",
    "InterestIncomeExpenseNonoperatingNet",
)
TAG_INTEREST_EXPENSE: tuple[str, ...] = TAG_INTEREST_EXPENSE_GROSS + TAG_INTEREST_INCOME_EXPENSE_NET

# Pure interest income tags paired strictly on same filing accession & period in v3.
TAG_INTEREST_INCOME_PURE: tuple[str, ...] = (
    "InvestmentIncomeInterest",
    "InterestIncomeDepositsWithFinancialInstitutions",
    "InterestIncomeOperating",
    "InterestIncomeOther",
    "InterestIncomeInterestEarningAsset",
    "InterestAndDividendIncomeOperating",
)
TAG_INTEREST_PARTIAL_DEBT_TRANCHES: tuple[str, ...] = (
    "InterestExpenseLongTermDebt",
    "InterestExpenseShortTermBorrowings",
    "InterestExpenseSubordinatedDebt",
)

TAG_EQUITY_METHOD_INCOME: tuple[str, ...] = (
    "IncomeLossFromEquityMethodInvestments",
    "IncomeLossFromEquityMethodInvestmentsNetOfDividendsOrDistributions",
)
TAG_EQUITY_METHOD_BS: tuple[str, ...] = ("EquityMethodInvestments",)

TAG_TAX_EXPENSE: tuple[str, ...] = ("IncomeTaxExpenseBenefit",)

TAG_DA_COMBINED: tuple[str, ...] = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAmortizationAndOther",
)

TAG_CASH: tuple[str, ...] = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "CashCashEquivalentsAndShortTermInvestments",
    "Cash",
)

TAG_SHORT_TERM_INVESTMENTS: tuple[str, ...] = (
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesCurrent",
)

# Debt & lease tag tiers (mutually exclusive resolution avoids double counting).
TAG_DEBT_NONCURRENT: tuple[str, ...] = ("LongTermDebtNoncurrent",)
TAG_DEBT_LT_CAP_INCL_CURRENT: tuple[str, ...] = (
    "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
)
TAG_DEBT_COMBINED_LT: tuple[str, ...] = ("LongTermDebt",)
TAG_DEBT_LT_AND_CAP_LEASE: tuple[str, ...] = ("LongTermDebtAndCapitalLeaseObligations",)
TAG_DEBT_OTHER_LT: tuple[str, ...] = ("SeniorNotes", "DebtInstrumentCarryingAmount", "NotesPayable")
TAG_DEBT_CURRENT_TOTAL: tuple[str, ...] = ("DebtCurrent",)
TAG_DEBT_CURRENT_MATURITIES: tuple[str, ...] = (
    "LongTermDebtCurrent",
    "LongTermDebtAndCapitalLeaseObligationsCurrent",
)
TAG_DEBT_CURRENT_MATURITIES_V3: tuple[str, ...] = (
    "LongTermDebtCurrent",
    "LongTermDebtAndCapitalLeaseObligationsCurrent",
    "SeniorNotesCurrent",
    "ConvertibleDebtCurrent",
    "OtherLongTermDebtCurrent",
)
TAG_DEBT_SHORT_TERM_ONLY: tuple[str, ...] = ("ShortTermBorrowings", "NotesPayableCurrent")
TAG_DEBT_SHORT_TERM_ONLY_V3: tuple[str, ...] = (
    "ShortTermBorrowings",
    "NotesPayableCurrent",
    "OtherShortTermBorrowings",
)
TAG_DEBT_COMMERCIAL_PAPER: tuple[str, ...] = ("CommercialPaper",)

# Tier-5 mutually exclusive specific debt instruments resolved ONLY when
# consolidated Tiers 1-4 (LongTermDebtNoncurrent / LongTermDebt / CapLease) are absent.
TAG_DEBT_SPEC_SECURED_OR_SENIOR: tuple[str, ...] = (
    "SecuredLongTermDebt",
    "SecuredDebt",
    "SeniorNotes",
    "SeniorLongTermNotes",
    "LongTermNotesPayable",
    "LongTermLoansPayable",
    "DebtInstrumentCarryingAmount",
    "NotesPayable",
)
TAG_DEBT_SPEC_CONVERTIBLE: tuple[str, ...] = (
    "ConvertibleDebtNoncurrent",
    "ConvertibleLongTermNotesPayable",
    "ConvertibleDebt",
)
TAG_DEBT_SPEC_LOC: tuple[str, ...] = ("LongTermLineOfCredit", "LineOfCredit")
TAG_DEBT_LOC_CURRENT: tuple[str, ...] = ("LinesOfCreditCurrent", "LoansPayableCurrent")
TAG_DEBT_SPEC_OTHER_LT: tuple[str, ...] = ("OtherLongTermDebtNoncurrent",)
TAG_OPERATING_LEASE_CURRENT: tuple[str, ...] = ("OperatingLeaseLiabilityCurrent",)
TAG_OPERATING_LEASE_NONCURRENT: tuple[str, ...] = ("OperatingLeaseLiabilityNoncurrent",)
TAG_OPERATING_LEASE_COMBINED: tuple[str, ...] = ("OperatingLeaseLiability",)
TAG_FINANCE_LEASE_CURRENT: tuple[str, ...] = ("FinanceLeaseLiabilityCurrent",)
TAG_FINANCE_LEASE_NONCURRENT: tuple[str, ...] = ("FinanceLeaseLiabilityNoncurrent",)
TAG_FINANCE_LEASE_COMBINED: tuple[str, ...] = ("FinanceLeaseLiability",)

TAG_REVENUE: tuple[str, ...] = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
TAG_COGS: tuple[str, ...] = (
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",
    "CostOfServices",
)
TAG_GROSS_PROFIT: tuple[str, ...] = ("GrossProfit",)
TAG_ASSETS: tuple[str, ...] = ("Assets",)
TAG_EQUITY: tuple[str, ...] = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
TAG_CAPEX: tuple[str, ...] = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "CapitalExpendituresIncurredButNotYetPaid",
)

YTD_DAY_RANGES: dict[str, tuple[int, int]] = {
    "Q1": (75, 108),
    "Q2": (165, 200),
    "Q3": (255, 290),
}


def default_catalog_path() -> Path:
    """Return bundled peer_verification.json path via package resources (CWD-independent)."""
    try:
        res = resources.files("covenant_ai").joinpath("peer_verification.json")
        return Path(str(res))
    except Exception:
        return Path(__file__).resolve().parent.parent / "peer_verification.json"


class PeerCatalog:
    """Validated peer catalog loaded and indexed once per catalog revision (P2)."""

    def __init__(
        self,
        catalog_path: Path | str | None = None,
        *,
        catalog_data: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._uses_bundled_resource = catalog_path is None and catalog_data is None
        self.catalog_path = Path(catalog_path) if catalog_path is not None else default_catalog_path()
        self._industries: dict[str, list[dict[str, Any]]] | None = None
        self._verified_tickers: frozenset[str] = frozenset()
        self._loaded_stamp: tuple[int, int] | None = None
        if catalog_data is not None:
            self._set_validated_industries(catalog_data, source_label="injected")

    def _set_validated_industries(
        self,
        industries: Any,
        *,
        source_label: str,
    ) -> None:
        if not isinstance(industries, dict):
            raise CatalogError(
                ProviderFailure(
                    ticker="",
                    stage=FailureStage.CATALOG,
                    reason_code=FailureReasonCode.MALFORMED_SOURCE,
                    message=f"Invalid peer verification catalog schema in {source_label}",
                    source=source_label,
                    retryable=False,
                )
            )
        normalized: dict[str, list[dict[str, Any]]] = {}
        verified: set[str] = set()
        for ind_key, items in industries.items():
            if not isinstance(items, list):
                raise CatalogError(
                    ProviderFailure(
                        ticker="",
                        stage=FailureStage.CATALOG,
                        reason_code=FailureReasonCode.MALFORMED_SOURCE,
                        message=f"Invalid industry entry for {ind_key!r} in {source_label}",
                        source=source_label,
                        retryable=False,
                    )
                )
            slug = str(ind_key).strip().lower()
            normalized[slug] = items
            for item in items:
                if (
                    isinstance(item, dict)
                    and item.get("verified")
                    and item.get("exchange") in MAJOR_US_EXCHANGES
                    and item.get("ticker")
                ):
                    verified.add(str(item["ticker"]).strip().upper())
        self._industries = normalized
        self._verified_tickers = frozenset(verified)

    def reload(self) -> None:
        """Force re-reading and validating the underlying catalog file."""
        self._industries = None
        self._loaded_stamp = None
        self.ensure_loaded()

    def ensure_loaded(self) -> dict[str, list[dict[str, Any]]]:
        """Load and validate catalog once (or reload if file mtime/size changes)."""
        if self._industries is not None and self._loaded_stamp is None:
            return self._industries
        if not self.catalog_path.is_file():
            raise CatalogError(
                ProviderFailure(
                    ticker="",
                    stage=FailureStage.CATALOG,
                    reason_code=FailureReasonCode.CATALOG_UNAVAILABLE,
                    message=f"Peer verification catalog not found: {self.catalog_path}",
                    source=str(self.catalog_path),
                    retryable=False,
                )
            )
        stat = self.catalog_path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._industries is not None and self._loaded_stamp == stamp:
            return self._industries

        try:
            payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise CatalogError(
                ProviderFailure(
                    ticker="",
                    stage=FailureStage.CATALOG,
                    reason_code=FailureReasonCode.MALFORMED_SOURCE,
                    message=f"Malformed peer verification catalog {self.catalog_path.name}: {exc}",
                    source=str(self.catalog_path),
                    retryable=False,
                )
            ) from exc
        if not isinstance(payload, dict) or "industries" not in payload:
            raise CatalogError(
                ProviderFailure(
                    ticker="",
                    stage=FailureStage.CATALOG,
                    reason_code=FailureReasonCode.MALFORMED_SOURCE,
                    message=f"Invalid peer verification catalog schema in {self.catalog_path.name}",
                    source=str(self.catalog_path),
                    retryable=False,
                )
            )
        self._set_validated_industries(payload["industries"], source_label=self.catalog_path.name)
        self._loaded_stamp = stamp
        assert self._industries is not None
        return self._industries

    def is_verified_peer(self, ticker: str) -> bool:
        """Check whether ticker is verified on a major US equity exchange (FIN-08)."""
        self.ensure_loaded()
        return ticker.strip().upper() in self._verified_tickers

    def get_candidate_peers(self, industry: str) -> list[str]:
        """Return verified EV/EBITDA-eligible candidate tickers for an industry."""
        industries = self.ensure_loaded()
        ind_key = industry.strip().lower()
        items = industries.get(ind_key, [])
        seen: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("verified") and item.get("ev_ebitda_eligible", True):
                t = str(item.get("ticker") or "").strip().upper()
                if t and t not in seen:
                    seen.append(t)
        return seen
