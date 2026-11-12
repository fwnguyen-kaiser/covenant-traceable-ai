"""Registry of standard financial metrics used across Covenant AI.

Categories follow the peer-screening framework in Rosenbaum & Pearl,
"Investment Banking" (Comparable Companies Analysis chapter).

Financial profile (SCREENING_CATEGORIES, dùng để chọn peer):
  Size, Profitability, Growth profile, Return on investment, Credit profile
Supplementary (phân tích sau, KHÔNG dùng lọc peer mặc định):
  Liquidity, Efficiency, Risk
BUILDING_BLOCK: số liệu thô / trung gian chỉ dùng làm input để tính metric khác.

Business profile (sector, products, end markets, channels, geography) là thuộc
tính định tính, nằm ở business_profile.py, không nằm trong registry này.

Each metric carries:
  - kind: RAW (lấy thẳng từ báo cáo/thị trường) | DERIVED (tính từ các metric khác)
  - inputs: metric name cần để tính (chỉ cho DERIVED). Mọi tên trong inputs
    PHẢI tồn tại trong registry (có test kiểm tra).
  - lookback_periods: số kỳ trước kỳ hiện tại mà phép tính cần
    (vd revenue_growth_yoy cần 1, revenue_cagr_3y cần 3).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional


class MetricKind(StrEnum):
    RAW = "raw"
    DERIVED = "derived"


class MetricCategory(StrEnum):
    # --- Financial profile (screening) ---
    SIZE = "size"
    PROFITABILITY = "profitability"
    GROWTH = "growth"
    RETURN_ON_INVESTMENT = "return_on_investment"
    CREDIT = "credit"
    # --- Supplementary ---
    LIQUIDITY = "liquidity"
    EFFICIENCY = "efficiency"
    RISK = "risk"
    # --- Inputs ---
    BUILDING_BLOCK = "building_block"


# Các nhóm dùng để chọn peer theo sách (Financial profile).
SCREENING_CATEGORIES: tuple[MetricCategory, ...] = (
    MetricCategory.SIZE,
    MetricCategory.PROFITABILITY,
    MetricCategory.GROWTH,
    MetricCategory.RETURN_ON_INVESTMENT,
    MetricCategory.CREDIT,
)


@dataclass(frozen=True)
class MetricDefinition:
    """One standard metric definition."""

    name: str  # snake_case key dùng trong FinancialDatapoint.metric, vd: "revenue"
    category: MetricCategory
    kind: MetricKind  # RAW hoặc DERIVED
    unit: str  # "USD", "percent", "x", "days"
    description: str
    inputs: tuple[str, ...] = ()  # metric names cần để tính (chỉ cho DERIVED)
    source_hint: Optional[str] = None  # "SEC 10-K", "consensus", "market data"
    lookback_periods: int = 0  # số kỳ trước cần thêm (growth metrics)


class MetricsRegistry:
    """In-memory registry of all standard metrics."""

    _metrics: dict[str, MetricDefinition] = {}

    @classmethod
    def register(cls, definition: MetricDefinition) -> None:
        cls._metrics[definition.name] = definition

    @classmethod
    def get(cls, name: str) -> Optional[MetricDefinition]:
        return cls._metrics.get(name)

    @classmethod
    def list_by_category(cls, category: MetricCategory) -> list[MetricDefinition]:
        return [m for m in cls._metrics.values() if m.category == category]

    @classmethod
    def screening_metrics(cls) -> list[MetricDefinition]:
        """Metric thuộc Financial profile theo sách, dùng để chọn peer."""
        return [m for m in cls._metrics.values() if m.category in SCREENING_CATEGORIES]

    @classmethod
    def all(cls) -> list[MetricDefinition]:
        return list(cls._metrics.values())

    @classmethod
    def names(cls) -> list[str]:
        return sorted(cls._metrics.keys())

    @classmethod
    def raw_names(cls) -> list[str]:
        return sorted(m.name for m in cls._metrics.values() if m.kind == MetricKind.RAW)

    @classmethod
    def derived_names(cls) -> list[str]:
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
# Raw line items (statements, market data); category ghi theo từng dòng
# ---------------------------------------------------------------------------
_reg("revenue", MetricCategory.SIZE, RAW, "USD", "Total revenue (top line)", (), "SEC 10-K / income statement")
_reg("gross_profit", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Gross profit = revenue - COGS", (), "SEC 10-K income statement")
_reg("cogs", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Cost of goods sold", (), "SEC 10-K income statement")
_reg("income_continuing_ops", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Income from continuing operations, consolidated (trước khi chia cho NCI)", (), "SEC 10-K income statement")
_reg("interest_expense_net", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Interest expense, net. Dương = chi phí ròng, ÂM = thu nhập ròng (vd Nike FY2026: -50)", (), "SEC 10-K income statement")
_reg("income_tax_expense", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Income tax expense", (), "SEC 10-K income statement")
_reg("depreciation_amortization", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Depreciation and amortization (add-back ở cash flow statement)", (), "SEC 10-K cash flow statement")
_reg(
    "ebit", MetricCategory.SIZE, DERIVED, "USD",
    "EBIT bottom-up = income_continuing_ops + interest_expense_net + income_tax_expense",
    ("income_continuing_ops", "interest_expense_net", "income_tax_expense"), "calculated",
)
_reg(
    "ebitda", MetricCategory.SIZE, DERIVED, "USD",
    "EBITDA = ebit + depreciation_amortization (chưa điều chỉnh khoản bất thường)",
    ("ebit", "depreciation_amortization"), "calculated",
)
_reg("net_income", MetricCategory.SIZE, RAW, "USD", "Net income / profit after tax", (), "SEC 10-K")
_reg("eps_diluted", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Diluted earnings per share", (), "SEC 10-K income statement")
_reg("effective_tax_rate", MetricCategory.BUILDING_BLOCK, RAW, "percent", "Effective tax rate", (), "SEC 10-K")
_reg("interest_expense", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Interest expense", (), "SEC 10-K income statement")
_reg("market_cap", MetricCategory.SIZE, RAW, "USD", "Market capitalization", (), "market data")
_reg("total_assets", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Total assets from balance sheet", (), "SEC 10-K balance sheet")
_reg("total_debt", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Total debt", (), "SEC 10-K balance sheet")
_reg("cash", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Cash and equivalents", (), "SEC 10-K balance sheet")
_reg("total_equity", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Total shareholders' equity", (), "SEC 10-K balance sheet")
_reg("preferred_equity", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Preferred equity", (), "SEC 10-K balance sheet")
_reg("non_controlling_interests", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Non-controlling interests (minority interest)", (), "SEC 10-K balance sheet")
_reg("inventory", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Inventory", (), "SEC 10-K balance sheet")
_reg("accounts_receivable", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Accounts receivable", (), "SEC 10-K balance sheet")
_reg("current_assets", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Current assets", (), "SEC 10-K balance sheet")
_reg("current_liabilities", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Current liabilities", (), "SEC 10-K balance sheet")
_reg("operating_cash_flow", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Cash flow from operations", (), "SEC 10-K cash flow statement")
_reg("capex", MetricCategory.BUILDING_BLOCK, RAW, "USD", "Capital expenditures", (), "SEC 10-K cash flow statement")

# Derived (size + building blocks)
_reg(
    "enterprise_value", MetricCategory.SIZE, DERIVED, "USD",
    "EV = market_cap + total_debt + preferred_equity + non_controlling_interests - cash",
    ("market_cap", "total_debt", "cash", "preferred_equity", "non_controlling_interests"),
    "calculated",
)
_reg(
    "invested_capital", MetricCategory.BUILDING_BLOCK, DERIVED, "USD",
    "Invested capital = total_debt + total_equity - cash",
    ("total_debt", "total_equity", "cash"), "calculated",
)
_reg(
    "nopat", MetricCategory.BUILDING_BLOCK, DERIVED, "USD",
    "NOPAT = ebit * (1 - effective_tax_rate)",
    ("ebit", "effective_tax_rate"), "calculated",
)
_reg(
    "fcf", MetricCategory.BUILDING_BLOCK, DERIVED, "USD",
    "Free cash flow = operating_cash_flow - capex",
    ("operating_cash_flow", "capex"), "calculated",
)

# ---------------------------------------------------------------------------
# GROWTH PROFILE (cần dữ liệu nhiều kỳ -> lookback_periods)
# ---------------------------------------------------------------------------
_reg("revenue_growth_yoy", MetricCategory.GROWTH, DERIVED, "percent", "Revenue growth YoY = revenue_t / revenue_t-1 - 1", ("revenue",), "calculated", 1)
_reg("revenue_cagr_3y", MetricCategory.GROWTH, DERIVED, "percent", "Revenue CAGR over 3 years = (revenue_t / revenue_t-3)^(1/3) - 1", ("revenue",), "calculated", 3)
_reg("ebitda_growth", MetricCategory.GROWTH, DERIVED, "percent", "EBITDA growth YoY", ("ebitda",), "calculated", 1)
_reg("eps_growth", MetricCategory.GROWTH, DERIVED, "percent", "Diluted EPS growth YoY", ("eps_diluted",), "calculated", 1)
_reg("forward_growth", MetricCategory.GROWTH, RAW, "percent", "Forward growth (analyst consensus if available)", (), "consensus")

# ---------------------------------------------------------------------------
# PROFITABILITY
# ---------------------------------------------------------------------------
_reg("gross_margin", MetricCategory.PROFITABILITY, DERIVED, "percent", "Gross margin = gross_profit / revenue", ("gross_profit", "revenue"), "calculated")
_reg("ebitda_margin", MetricCategory.PROFITABILITY, DERIVED, "percent", "EBITDA margin = ebitda / revenue", ("ebitda", "revenue"), "calculated")
_reg("ebit_margin", MetricCategory.PROFITABILITY, DERIVED, "percent", "EBIT margin = ebit / revenue", ("ebit", "revenue"), "calculated")
_reg("net_margin", MetricCategory.PROFITABILITY, DERIVED, "percent", "Net margin = net_income / revenue", ("net_income", "revenue"), "calculated")
_reg("roe", MetricCategory.RETURN_ON_INVESTMENT, DERIVED, "percent", "Return on equity = net_income / total_equity", ("net_income", "total_equity"), "calculated")
_reg("roa", MetricCategory.RETURN_ON_INVESTMENT, DERIVED, "percent", "Return on assets = net_income / total_assets", ("net_income", "total_assets"), "calculated")
_reg("roic", MetricCategory.RETURN_ON_INVESTMENT, DERIVED, "percent", "Return on invested capital = nopat / invested_capital", ("nopat", "invested_capital"), "calculated")

# ---------------------------------------------------------------------------
# CREDIT PROFILE
# ---------------------------------------------------------------------------
_reg("debt_to_equity", MetricCategory.CREDIT, DERIVED, "x", "Debt-to-equity = total_debt / total_equity", ("total_debt", "total_equity"), "calculated")
_reg("net_debt_to_ebitda", MetricCategory.CREDIT, DERIVED, "x", "Net debt / EBITDA = (total_debt - cash) / ebitda", ("total_debt", "cash", "ebitda"), "calculated")
_reg("debt_to_capital", MetricCategory.CREDIT, DERIVED, "percent", "Debt-to-capital = total_debt / (total_debt + total_equity)", ("total_debt", "total_equity"), "calculated")
_reg("interest_coverage", MetricCategory.CREDIT, DERIVED, "x", "Interest coverage = ebit / interest_expense", ("ebit", "interest_expense"), "calculated")

# ---------------------------------------------------------------------------
# LIQUIDITY
# ---------------------------------------------------------------------------
_reg("current_ratio", MetricCategory.LIQUIDITY, DERIVED, "x", "Current ratio = current_assets / current_liabilities", ("current_assets", "current_liabilities"), "calculated")
_reg("quick_ratio", MetricCategory.LIQUIDITY, DERIVED, "x", "Quick ratio = (current_assets - inventory) / current_liabilities", ("current_assets", "inventory", "current_liabilities"), "calculated")

# ---------------------------------------------------------------------------
# EFFICIENCY & CASH FLOW
# ---------------------------------------------------------------------------
_reg("asset_turnover", MetricCategory.EFFICIENCY, DERIVED, "x", "Asset turnover = revenue / total_assets", ("revenue", "total_assets"), "calculated")
_reg("inventory_turnover", MetricCategory.EFFICIENCY, DERIVED, "x", "Inventory turnover = cogs / inventory", ("cogs", "inventory"), "calculated")
_reg("dso", MetricCategory.EFFICIENCY, DERIVED, "days", "Days sales outstanding = accounts_receivable / revenue * 365", ("accounts_receivable", "revenue"), "calculated")
_reg("capex_to_revenue", MetricCategory.EFFICIENCY, DERIVED, "percent", "Capex / revenue", ("capex", "revenue"), "calculated")
_reg("fcf_margin", MetricCategory.EFFICIENCY, DERIVED, "percent", "FCF margin = fcf / revenue", ("fcf", "revenue"), "calculated")
_reg("cash_conversion", MetricCategory.EFFICIENCY, DERIVED, "x", "Cash conversion = fcf / ebitda", ("fcf", "ebitda"), "calculated")

# ---------------------------------------------------------------------------
# RISK
# ---------------------------------------------------------------------------
_reg("beta", MetricCategory.RISK, RAW, "x", "Beta coefficient vs market", (), "market data")


__all__ = [
    "SCREENING_CATEGORIES",
    "MetricKind",
    "MetricCategory",
    "MetricDefinition",
    "MetricsRegistry",
]