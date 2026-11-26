"""Registry of standard financial metrics used across Covenant AI.

Design follows the two-tier peer-screening framework from:
  R&P Ch.1, "Comparable Companies Analysis", Exhibit 1.3
  Rosenbaum & Pearl, "Investment Banking: Valuation, LBOs, and M&A"

Tier 1 -- Business profile (qualitative): sector, products & services,
  customers & end markets, distribution channels, geography.
  -> Stored in business_profile.py, NOT in this registry.

Tier 2 -- Financial profile (quantitative): the five SCREENING_CATEGORIES
  below are used to score and rank peer candidates.
  -> Supplementary categories (Liquidity, Efficiency, Risk) are computed
     for deeper analysis but are NOT part of default peer screening.

Metric kinds:
  RAW         -- taken directly from filings or market data feeds.
  DERIVED     -- computed from other registry metrics; all names in `inputs`
                 must exist in the registry (enforced by tests).
  BUILDING_BLOCK -- raw or derived intermediate; used as input to other
                 metrics only, not surfaced in screening output.

Metric fields:
  inputs          -- dependency names (DERIVED only).
  lookback_periods -- extra historical periods needed (e.g. YoY growth = 1).
  source_hint     -- canonical data source for the metric.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional


class MetricKind(StrEnum):
    """Whether a metric is read directly from a source or computed."""
    RAW = "raw"
    DERIVED = "derived"


class MetricCategory(StrEnum):
    """
    Five SCREENING categories mirror the Financial Profile in Exhibit 1.3
    (R&P Ch.1, p.21-22). The rest are supplementary analysis tools.
    """
    # --- Financial profile (peer screening, R&P Exhibit 1.3) ---
    SIZE = "size"                        # market cap, EV, revenue, EBITDA, EBIT, net income
    PROFITABILITY = "profitability"      # margin metrics (gross, EBITDA, EBIT, net)
    GROWTH = "growth"                    # historical + forward growth rates
    RETURN_ON_INVESTMENT = "return_on_investment"  # ROIC, ROE, ROA
    CREDIT = "credit"                    # leverage and coverage ratios
    # --- Supplementary (computed for deeper analysis, not used in peer screening) ---
    LIQUIDITY = "liquidity"
    EFFICIENCY = "efficiency"
    RISK = "risk"
    # --- Intermediate inputs (never surfaced directly in output) ---
    BUILDING_BLOCK = "building_block"


# The five Financial Profile categories used by default for peer screening.
# Book: R&P Ch.1, Comparable Companies Analysis, Exhibit 1.3, p.21-22.
SCREENING_CATEGORIES: tuple[MetricCategory, ...] = (
    MetricCategory.SIZE,
    MetricCategory.PROFITABILITY,
    MetricCategory.GROWTH,
    MetricCategory.RETURN_ON_INVESTMENT,
    MetricCategory.CREDIT,
)


@dataclass(frozen=True)
class MetricDefinition:
    """
    Immutable descriptor for one standard financial metric.

    Fields
    ------
    name            : snake_case key; must match FinancialDatapoint.metric.
    category        : MetricCategory (determines screening vs supplementary).
    kind            : RAW (from filings) or DERIVED (computed from inputs).
    unit            : "USD", "percent", "x", or "days".
    description     : one-line formula or meaning.
    inputs          : ordered tuple of metric names this one depends on.
    source_hint     : canonical data source (e.g. "SEC 10-K", "market data").
    lookback_periods: extra historical periods required for the calculation
                      (e.g. YoY growth needs 1; 3-year CAGR needs 3).
    """

    name: str
    category: MetricCategory
    kind: MetricKind
    unit: str
    description: str
    inputs: tuple[str, ...] = ()
    source_hint: Optional[str] = None
    lookback_periods: int = 0


class MetricsRegistry:
    """
    In-memory singleton registry of all standard metric definitions.

    Metrics are registered at module import time by calling _reg() below.
    Downstream code should read from this registry rather than hard-coding
    metric names, so that category and dependency changes propagate
    automatically.

    Lookup methods
    --------------
    get(name)              -> single MetricDefinition or None.
    list_by_category(cat)  -> all metrics in one category.
    screening_metrics()    -> metrics in SCREENING_CATEGORIES only
                              (the five Financial Profile dimensions from
                              R&P Ch.1 Exhibit 1.3).
    all()                  -> full list, insertion order.
    names() / raw_names() / derived_names() -> sorted name lists.
    """

    _metrics: dict[str, MetricDefinition] = {}

    @classmethod
    def register(cls, definition: MetricDefinition) -> None:
        """Add or overwrite a metric definition in the registry."""
        cls._metrics[definition.name] = definition

    @classmethod
    def get(cls, name: str) -> Optional[MetricDefinition]:
        """Return the definition for `name`, or None if not registered."""
        return cls._metrics.get(name)

    @classmethod
    def list_by_category(cls, category: MetricCategory) -> list[MetricDefinition]:
        """All metrics belonging to `category`."""
        return [m for m in cls._metrics.values() if m.category == category]

    @classmethod
    def screening_metrics(cls) -> list[MetricDefinition]:
        """
        Metrics in the five Financial Profile categories used for peer screening.
        Book: R&P Ch.1, Comparable Companies Analysis, Exhibit 1.3, p.21-22.
        """
        return [m for m in cls._metrics.values() if m.category in SCREENING_CATEGORIES]

    @classmethod
    def all(cls) -> list[MetricDefinition]:
        """All registered metrics in insertion order."""
        return list(cls._metrics.values())

    @classmethod
    def names(cls) -> list[str]:
        """Sorted list of all metric names."""
        return sorted(cls._metrics.keys())

    @classmethod
    def raw_names(cls) -> list[str]:
        """Sorted list of RAW metric names."""
        return sorted(m.name for m in cls._metrics.values() if m.kind == MetricKind.RAW)

    @classmethod
    def derived_names(cls) -> list[str]:
        """Sorted list of DERIVED metric names."""
        return sorted(m.name for m in cls._metrics.values() if m.kind == MetricKind.DERIVED)


def _reg(
    name: str,
    category: MetricCategory,
    kind: MetricKind,
    unit: str,
    desc: str,
    inputs: tuple[str, ...] = (),
    hint: Optional[str] = None,
    lookback: int = 0,
) -> None:
    MetricsRegistry.register(
        MetricDefinition(name, category, kind, unit, desc, inputs, hint, lookback)
    )


RAW = MetricKind.RAW
DERIVED = MetricKind.DERIVED

# ---------------------------------------------------------------------------
# SIZE
# Book: R&P Ch.1, Comparable Companies Analysis, Exhibit 1.3, p.21.
#   Size = market valuation (equity value, enterprise value) AND key financial
#   statistics (sales, gross profit, EBITDA, EBIT, net income). Similar-size
#   companies in a sector tend to trade at similar multiples. Peers are often
#   split into sub-tiers (e.g. above/below $5B EV) when the sample is large
#   enough to support sub-groups.
# Our choice: gross_profit is categorized under SIZE per R&P Ch.1 Exhibit 1.3
#   and also feeds gross_margin. See docs/rosenbaum-pearl-ch1-profile-framework.md.
# ---------------------------------------------------------------------------
_reg("revenue", MetricCategory.SIZE, RAW, "USD", "Total revenue (top line)", (), "SEC 10-K / income statement")
_reg("gross_profit", MetricCategory.SIZE, RAW, "USD", "Gross profit = revenue - COGS", (), "SEC 10-K / income statement")

# BUILDING_BLOCK raw line items
# These are intermediate numbers read directly from filings. They are not
# screened against directly but feed the DERIVED metrics registered below.
_reg("cogs", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Cost of goods sold", (), "SEC 10-K income statement")
_reg("income_continuing_ops", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Income from continuing operations, consolidated (before NCI allocation)", (), "SEC 10-K income statement")
_reg("interest_expense_net", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Interest expense, net. Positive = net cost, negative = net income (e.g. Nike FY2026: -50)", (), "SEC 10-K income statement")
_reg("income_tax_expense", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Income tax expense", (), "SEC 10-K income statement")
_reg("depreciation_amortization", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Depreciation and amortization (add-back in cash flow statement)", (), "SEC 10-K cash flow statement")

# EBIT and EBITDA: SIZE per Exhibit 1.3; also used as inputs in many DERIVED metrics.
_reg(
    "ebit", MetricCategory.SIZE, DERIVED, "USD",
    "EBIT bottom-up = income_continuing_ops + interest_expense_net + income_tax_expense",
    ("income_continuing_ops", "interest_expense_net", "income_tax_expense"), "calculated",
)
_reg(
    "ebitda", MetricCategory.SIZE, DERIVED, "USD",
    "EBITDA = ebit + depreciation_amortization (unadjusted for non-recurring items)",
    ("ebit", "depreciation_amortization"), "calculated",
)
_reg("net_income", MetricCategory.SIZE, RAW, "USD", "Net income / profit after tax", (), "SEC 10-K")
_reg("eps_diluted", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Diluted earnings per share", (), "SEC 10-K income statement")
_reg("effective_tax_rate", MetricCategory.BUILDING_BLOCK, RAW, "percent", "Effective tax rate", (), "SEC 10-K")
_reg("interest_expense", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Interest expense (gross, before netting interest income)", (), "SEC 10-K income statement")

# Market valuation inputs
_reg("market_cap", MetricCategory.SIZE, RAW, "USD", "Market capitalization", (), "market data")

# Balance sheet building blocks
_reg("total_assets", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Total assets from balance sheet", (), "SEC 10-K balance sheet")
_reg("total_debt", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Total debt", (), "SEC 10-K balance sheet")
_reg("cash", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Cash and equivalents", (), "SEC 10-K balance sheet")
_reg("total_equity", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Total shareholders equity", (), "SEC 10-K balance sheet")
_reg("preferred_equity", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Preferred equity", (), "SEC 10-K balance sheet")
_reg("non_controlling_interests", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Non-controlling interests (minority interest)", (), "SEC 10-K balance sheet")
_reg("inventory", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Inventory", (), "SEC 10-K balance sheet")
_reg("accounts_receivable", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Accounts receivable", (), "SEC 10-K balance sheet")
_reg("current_assets", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Current assets", (), "SEC 10-K balance sheet")
_reg("current_liabilities", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Current liabilities", (), "SEC 10-K balance sheet")

# Cash flow building blocks
_reg("operating_cash_flow", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Cash flow from operations", (), "SEC 10-K cash flow statement")
_reg("capex", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Capital expenditures", (), "SEC 10-K cash flow statement")

# Derived SIZE metrics
_reg(
    "enterprise_value", MetricCategory.SIZE, DERIVED, "USD",
    "EV = market_cap + total_debt + preferred_equity + non_controlling_interests - cash",
    ("market_cap", "total_debt", "cash", "preferred_equity", "non_controlling_interests"),
    "calculated",
)

# Derived BUILDING_BLOCK intermediates used by RETURN_ON_INVESTMENT
_reg(
    "invested_capital", MetricCategory.BUILDING_BLOCK, DERIVED, "USD",
    "Invested capital = total_debt + total_equity - cash",
    ("total_debt", "total_equity", "cash"), "calculated",
)
_reg(
    "nopat", MetricCategory.BUILDING_BLOCK, DERIVED, "USD",
    # Book: R&P Ch.1, Exhibit 1.3, p.22, footnote 9: NOPAT = tax-effected EBIT (also called EBIAT).
    "NOPAT = ebit * (1 - effective_tax_rate)",
    ("ebit", "effective_tax_rate"), "calculated",
)
_reg(
    "fcf", MetricCategory.BUILDING_BLOCK, DERIVED, "USD",
    "Free cash flow = operating_cash_flow - capex",
    ("operating_cash_flow", "capex"), "calculated",
)

# ---------------------------------------------------------------------------
# GROWTH PROFILE
# Book: R&P Ch.1, Comparable Companies Analysis, Exhibit 1.3, p.22.
#   Historical and estimated future growth of sales, EBITDA, and EPS.
#   Higher growth earns higher multiples. The book stresses distinguishing
#   organic growth from acquisition-driven growth (organic is preferred).
#   For mature companies EPS growth is more meaningful; for early-stage
#   companies with little/no earnings, sales or EBITDA growth is used instead.
# Our choice: organic vs acquisition-driven growth is not flagged in the data
#   model yet. Noted as a limitation in ValuationResult.limitations per
#   docs/rosenbaum-pearl-ch1-profile-framework.md section 5, item 5.
# Note: lookback_periods tells the data layer how many extra historical
#   periods to fetch (1 for YoY, 3 for 3-year CAGR).
# ---------------------------------------------------------------------------
_reg("revenue_growth_yoy", MetricCategory.GROWTH, DERIVED, "percent", "Revenue growth YoY = revenue_t / revenue_t-1 - 1", ("revenue",), "calculated", 1)
_reg("revenue_cagr_3y", MetricCategory.GROWTH, DERIVED, "percent", "Revenue CAGR over 3 years = (revenue_t / revenue_t-3)^(1/3) - 1", ("revenue",), "calculated", 3)
_reg("ebitda_growth", MetricCategory.GROWTH, DERIVED, "percent", "EBITDA growth YoY", ("ebitda",), "calculated", 1)
# Book: EPS growth is more meaningful for mature companies (R&P Ch.1, Exhibit 1.3, p.22).
_reg("eps_growth", MetricCategory.GROWTH, DERIVED, "percent", "Diluted EPS growth YoY", ("eps_diluted",), "calculated", 1)
_reg("forward_growth", MetricCategory.GROWTH, RAW, "percent", "Forward growth (analyst consensus if available)", (), "consensus")

# ---------------------------------------------------------------------------
# PROFITABILITY
# Book: R&P Ch.1, Comparable Companies Analysis, Exhibit 1.3, p.22.
#   Ability to convert sales into profit. Margin = profit measure / sales.
#   Profit measures listed: gross profit, EBITDA, EBIT, net income.
#   Within a sector, higher margins generally support higher valuations.
#   Some sectors measure profitability per unit rather than as a margin
#   (R&P Ch.1, footnote 8).
# Our choice: per-unit metrics are not in scope for the initial release.
# ---------------------------------------------------------------------------
_reg("gross_margin", MetricCategory.PROFITABILITY, DERIVED, "percent", "Gross margin = gross_profit / revenue", ("gross_profit", "revenue"), "calculated")
_reg("ebitda_margin", MetricCategory.PROFITABILITY, DERIVED, "percent", "EBITDA margin = ebitda / revenue", ("ebitda", "revenue"), "calculated")
_reg("ebit_margin", MetricCategory.PROFITABILITY, DERIVED, "percent", "EBIT margin = ebit / revenue", ("ebit", "revenue"), "calculated")
_reg("net_margin", MetricCategory.PROFITABILITY, DERIVED, "percent", "Net margin = net_income / revenue", ("net_income", "revenue"), "calculated")

# ---------------------------------------------------------------------------
# RETURN ON INVESTMENT
# Book: R&P Ch.1, Comparable Companies Analysis, Exhibit 1.3, p.22.
#   Ability to generate returns for capital providers. Most common metrics:
#   ROIC (NOPAT / invested capital), ROE (net income / equity),
#   ROA (net income / total assets). Dividend yield is also listed by the
#   book as a return metric (R&P Ch.1, Exhibit 1.3, p.22).
# Our choice: dividend yield is not implemented because it requires current
#   share price and dividend data not yet in the data model. Noted as a
#   limitation per docs/rosenbaum-pearl-ch1-profile-framework.md section 5,
#   item 3.
# ---------------------------------------------------------------------------
_reg("roe", MetricCategory.RETURN_ON_INVESTMENT, DERIVED, "percent", "Return on equity = net_income / total_equity", ("net_income", "total_equity"), "calculated")
_reg("roa", MetricCategory.RETURN_ON_INVESTMENT, DERIVED, "percent", "Return on assets = net_income / total_assets", ("net_income", "total_assets"), "calculated")
_reg("roic", MetricCategory.RETURN_ON_INVESTMENT, DERIVED, "percent", "Return on invested capital = nopat / invested_capital", ("nopat", "invested_capital"), "calculated")

# ---------------------------------------------------------------------------
# CREDIT PROFILE
# Book: R&P Ch.1, Comparable Companies Analysis, Exhibit 1.3, p.22.
#   Creditworthiness measured by leverage (overall debt level) and coverage
#   (ability to service interest). Main agencies: Moody's, S&P, Fitch.
# Our choice: credit ratings (Moody's / S&P / Fitch) are qualitative
#   attributes that belong on the company profile, not in the numeric
#   registry. Noted in docs/rosenbaum-pearl-ch1-profile-framework.md,
#   section 5, item 4.
# Our choice: interest_coverage uses EBIT / interest_expense (gross).
#   The book says "coverage" without specifying EBIT or EBITDA as numerator;
#   if an EBITDA-based coverage ratio is added later, it should be labelled
#   as a team extension, not a book requirement (section 5, item 7).
# ---------------------------------------------------------------------------
_reg("debt_to_equity", MetricCategory.CREDIT, DERIVED, "x", "Debt-to-equity = total_debt / total_equity", ("total_debt", "total_equity"), "calculated")
_reg("net_debt_to_ebitda", MetricCategory.CREDIT, DERIVED, "x", "Net debt / EBITDA = (total_debt - cash) / ebitda", ("total_debt", "cash", "ebitda"), "calculated")
_reg("debt_to_capital", MetricCategory.CREDIT, DERIVED, "percent", "Debt-to-capital = total_debt / (total_debt + total_equity)", ("total_debt", "total_equity"), "calculated")
_reg("interest_coverage", MetricCategory.CREDIT, DERIVED, "x", "Interest coverage = ebit / interest_expense", ("ebit", "interest_expense"), "calculated")

# ---------------------------------------------------------------------------
# LIQUIDITY (supplementary -- not part of default peer screening)
# Used for deeper credit and operational analysis after the peer set is chosen.
# ---------------------------------------------------------------------------
_reg("current_ratio", MetricCategory.LIQUIDITY, DERIVED, "x", "Current ratio = current_assets / current_liabilities", ("current_assets", "current_liabilities"), "calculated")
_reg("quick_ratio", MetricCategory.LIQUIDITY, DERIVED, "x", "Quick ratio = (current_assets - inventory) / current_liabilities", ("current_assets", "inventory", "current_liabilities"), "calculated")

# ---------------------------------------------------------------------------
# EFFICIENCY & CASH FLOW (supplementary -- not part of default peer screening)
# Operational efficiency and cash generation quality metrics.
# ---------------------------------------------------------------------------
_reg("asset_turnover", MetricCategory.EFFICIENCY, DERIVED, "x", "Asset turnover = revenue / total_assets", ("revenue", "total_assets"), "calculated")
_reg("inventory_turnover", MetricCategory.EFFICIENCY, DERIVED, "x", "Inventory turnover = cogs / inventory", ("cogs", "inventory"), "calculated")
_reg("dso", MetricCategory.EFFICIENCY, DERIVED, "days", "Days sales outstanding = accounts_receivable / revenue * 365", ("accounts_receivable", "revenue"), "calculated")
_reg("capex_to_revenue", MetricCategory.EFFICIENCY, DERIVED, "percent", "Capex / revenue", ("capex", "revenue"), "calculated")
_reg("fcf_margin", MetricCategory.EFFICIENCY, DERIVED, "percent", "FCF margin = fcf / revenue", ("fcf", "revenue"), "calculated")
_reg("cash_conversion", MetricCategory.EFFICIENCY, DERIVED, "x", "Cash conversion = fcf / ebitda", ("fcf", "ebitda"), "calculated")

# ---------------------------------------------------------------------------
# RISK (supplementary -- not part of default peer screening)
# ---------------------------------------------------------------------------
_reg("beta", MetricCategory.RISK, RAW, "x", "Beta coefficient vs market", (), "market data")


__all__ = [
    "SCREENING_CATEGORIES",
    "MetricKind",
    "MetricCategory",
    "MetricDefinition",
    "MetricsRegistry",
]