"""Shared FinancialDatapoint builder with separate FY and LTM period selectors (P2)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from covenant_ai.domain.bridge import evaluate_peer_evidence
from covenant_ai.domain.evidence import (
    DEFAULT_METHOD_VERSION,
    EvidencedValue,
    FailureReasonCode,
    FailureStage,
    MethodologyVersion,
    PeerExtractionOutcome,
    ProviderFailure,
    normalize_method_version,
)
from covenant_ai.domain.models import FinancialDatapoint, SourceType
from covenant_ai.providers.edgar_errors import MissingEvidenceError, classify_provider_exception
from covenant_ai.providers.edgar_tags import (
    ANNUAL_FORMS,
    QUARTERLY_FORMS,
    TAG_ASSETS,
    TAG_CAPEX,
    TAG_CASH,
    TAG_COGS,
    TAG_DA_COMBINED,
    TAG_DEBT_COMBINED_LT,
    TAG_DEBT_COMMERCIAL_PAPER,
    TAG_DEBT_CURRENT_MATURITIES_V3,
    TAG_DEBT_CURRENT_TOTAL,
    TAG_DEBT_LOC_CURRENT,
    TAG_DEBT_LT_AND_CAP_LEASE,
    TAG_DEBT_LT_CAP_INCL_CURRENT,
    TAG_DEBT_NONCURRENT,
    TAG_DEBT_SHORT_TERM_ONLY_V3,
    TAG_DEBT_SPEC_CONVERTIBLE,
    TAG_DEBT_SPEC_LOC,
    TAG_DEBT_SPEC_OTHER_LT,
    TAG_DEBT_SPEC_SECURED_OR_SENIOR,
    TAG_EQUITY,
    TAG_EQUITY_METHOD_BS,
    TAG_EQUITY_METHOD_INCOME,
    TAG_FINANCE_LEASE_COMBINED,
    TAG_FINANCE_LEASE_CURRENT,
    TAG_FINANCE_LEASE_NONCURRENT,
    TAG_GROSS_PROFIT,
    TAG_INCOME_CONTINUING,
    TAG_INTEREST_EXPENSE_GROSS,
    TAG_INTEREST_INCOME_EXPENSE_NET,
    TAG_INTEREST_INCOME_PURE,
    TAG_INTEREST_PARTIAL_DEBT_TRANCHES,
    TAG_REVENUE,
    TAG_SHORT_TERM_INVESTMENTS,
    TAG_TAX_EXPENSE,
    YTD_DAY_RANGES,
)
from covenant_ai.providers.xbrl_reader import (
    ShareClassComponent,
    TypedAnnualFact,
    TypedInstantFact,
    TypedLtmSeries,
    XbrlFactReader,
)

PriceLookupFn = Callable[[str, date, str], tuple[Decimal, date, str]]
ListedTickersFn = Callable[[int, str], list[str]]
ClockFn = Callable[[], datetime]


def _default_utc_clock() -> datetime:
    return datetime.now(UTC)


class PeerDatapointBuilder:
    """Constructs FinancialDatapoint collections from typed XBRL facts and snapshots (P2)."""

    def __init__(
        self,
        fact_reader: XbrlFactReader,
        price_lookup: PriceLookupFn,
        listed_tickers_lookup: ListedTickersFn | None = None,
        *,
        clock: ClockFn | None = None,
        share_class_supplements: dict[tuple[int, str], list[ShareClassComponent]] | None = None,
    ) -> None:
        self.fact_reader = fact_reader
        self.price_lookup = price_lookup
        self.listed_tickers_lookup = listed_tickers_lookup
        self.clock = clock or _default_utc_clock
        self.share_class_supplements: dict[tuple[int, str], list[ShareClassComponent]] = dict(
            share_class_supplements or {}
        )

    @staticmethod
    def make_datapoint(
        *,
        ticker: str,
        metric: str,
        value: Decimal,
        period: str,
        period_end: date,
        source_type: SourceType,
        source_ref: str,
        retrieved_at: datetime,
        currency: str = "USD",
        filing_date: date | None = None,
    ) -> FinancialDatapoint:
        """Central factory eliminating duplicate FinancialDatapoint field wiring."""
        return FinancialDatapoint(
            metric=metric,
            value=value,
            currency=currency,
            period=period,
            period_end=period_end,
            filing_date=filing_date,
            source_type=source_type,
            source_ref=source_ref,
            retrieved_at=retrieved_at,
            peer_ticker=ticker,
        )

    def _resolve_cash_and_sti(
        self,
        gaap: dict[str, Any],
        *,
        ticker: str,
        period_end: str,
        accn: str,
        allowed_forms: tuple[str, ...],
        missing_label: str,
    ) -> tuple[Decimal, str, TypedInstantFact]:
        cash_item = self.fact_reader.find_instant_fact(
            gaap, TAG_CASH, period_end, accn=accn, allowed_forms=allowed_forms
        )
        if cash_item is None:
            raise KeyError(f"Ticker {ticker} is missing {missing_label} for {period_end}")
        cash_val = cash_item.value
        cash_tag = cash_item.tag
        if cash_tag != "CashCashEquivalentsAndShortTermInvestments":
            sti_item = self.fact_reader.find_instant_fact(
                gaap, TAG_SHORT_TERM_INVESTMENTS, period_end, accn=accn, allowed_forms=allowed_forms
            )
            if sti_item is not None:
                cash_val += sti_item.value
                cash_tag += f"+{sti_item.tag}"
        return cash_val, cash_tag, cash_item

    def _validate_debt_for_method(
        self,
        gaap: dict[str, Any],
        *,
        ticker: str,
        cik_str: str | None,
        period: str,
        period_end: str,
        accn: str,
        allowed_forms: tuple[str, ...],
        eff_method: MethodologyVersion,
        label_prefix: str = "debt",
        source_label: str = "SEC 10-K",
    ) -> tuple[Decimal, list[str], EvidencedValue]:
        is_v3 = eff_method == MethodologyVersion.V3
        total_debt, debt_tags_used, debt_ev = self.fact_reader.extract_debt_with_evidence(
            gaap,
            period_end,
            accn=accn,
            allowed_forms=allowed_forms,
            include_operating_lease=not is_v3,
            extended_tiers=is_v3,
        )
        if is_v3 and not debt_ev.is_usable:
            raise MissingEvidenceError(
                ProviderFailure(
                    ticker=ticker,
                    cik=cik_str,
                    period=period,
                    stage=FailureStage.XBRL_PARSE,
                    reason_code=debt_ev.reason_code or FailureReasonCode.FACT_MISSING,
                    message=f"Ticker {ticker} {label_prefix} {debt_ev.status.value} for {period_end}: {debt_ev.reason}",
                    source=f"{source_label} {period_end}",
                    retryable=False,
                )
            )
        return total_debt, debt_tags_used, debt_ev

    @staticmethod
    def _format_debt_source_ref(
        *,
        prefix: str,
        debt_tags_used: list[str],
        debt_ev: EvidencedValue,
        eff_method: MethodologyVersion,
        note_suffix: str = "",
    ) -> str:
        tags_s = ", ".join(debt_tags_used) or "None"
        if eff_method != MethodologyVersion.V3:
            return f"{prefix} tags: {tags_s}{note_suffix}"
        comp = debt_ev.components
        breakdown = (
            f"core_debt={comp.get('core_debt', '0')}, "
            f"finance_lease={comp.get('finance_lease', '0')}, "
            f"operating_lease_excluded={comp.get('operating_lease_excluded', '0')}"
        )
        return f"{prefix} tags: {tags_s} ({breakdown}){note_suffix}"

    def _find_excluded_debt_tranche(
        self,
        gaap: dict[str, Any],
        *,
        int_tag: str,
        period_end: str,
        accn: str,
        allowed_forms: tuple[str, ...],
    ) -> str | None:
        """Detect balance-sheet debt tranches excluded by partial-tranche interest tags."""
        if int_tag not in TAG_INTEREST_PARTIAL_DEBT_TRANCHES:
            return None

        def nonzero(groups: tuple[tuple[str, ...], ...]) -> list[str]:
            found: list[str] = []
            for grp in groups:
                item = self.fact_reader.find_instant_fact(
                    gaap, grp, period_end, accn=accn, allowed_forms=allowed_forms
                )
                if item is not None and item.value != 0:
                    found.append(f"{item.tag}={item.value}")
            return found

        if int_tag == "InterestExpenseLongTermDebt":
            excl = nonzero(
                (
                    TAG_DEBT_SHORT_TERM_ONLY_V3,
                    TAG_DEBT_LOC_CURRENT,
                    TAG_DEBT_COMMERCIAL_PAPER,
                    TAG_FINANCE_LEASE_CURRENT,
                    TAG_FINANCE_LEASE_NONCURRENT,
                    TAG_FINANCE_LEASE_COMBINED,
                )
            )
            dc = self.fact_reader.find_instant_fact(
                gaap, TAG_DEBT_CURRENT_TOTAL, period_end, accn=accn, allowed_forms=allowed_forms
            )
            if dc is not None and dc.value != 0:
                ltc = self.fact_reader.find_instant_fact(
                    gaap, TAG_DEBT_CURRENT_MATURITIES_V3, period_end, accn=accn, allowed_forms=allowed_forms
                )
                if ltc is None or dc.value > ltc.value:
                    excl.append(f"{dc.tag}={dc.value}")
            return ", ".join(excl) if excl else None

        if int_tag == "InterestExpenseShortTermBorrowings":
            excl = nonzero(
                (
                    TAG_DEBT_NONCURRENT,
                    TAG_DEBT_LT_CAP_INCL_CURRENT,
                    TAG_DEBT_COMBINED_LT,
                    TAG_DEBT_LT_AND_CAP_LEASE,
                    TAG_DEBT_CURRENT_MATURITIES_V3,
                    TAG_DEBT_SPEC_SECURED_OR_SENIOR,
                    TAG_DEBT_SPEC_CONVERTIBLE,
                    TAG_DEBT_SPEC_LOC,
                    TAG_DEBT_SPEC_OTHER_LT,
                    TAG_FINANCE_LEASE_CURRENT,
                    TAG_FINANCE_LEASE_NONCURRENT,
                    TAG_FINANCE_LEASE_COMBINED,
                )
            )
            return ", ".join(excl) if excl else None

        # InterestExpenseSubordinatedDebt excludes all senior/secured/LOC/ST/CP/finance-lease debt.
        excl = nonzero(
            (
                TAG_DEBT_NONCURRENT,
                TAG_DEBT_LT_CAP_INCL_CURRENT,
                TAG_DEBT_COMBINED_LT,
                TAG_DEBT_LT_AND_CAP_LEASE,
                TAG_DEBT_SPEC_SECURED_OR_SENIOR,
                TAG_DEBT_SPEC_CONVERTIBLE,
                TAG_DEBT_SPEC_LOC,
                TAG_DEBT_CURRENT_TOTAL,
                TAG_DEBT_SHORT_TERM_ONLY_V3,
                TAG_DEBT_COMMERCIAL_PAPER,
                TAG_FINANCE_LEASE_COMBINED,
            )
        )
        return ", ".join(excl) if excl else None

    def _find_matching_pure_interest_income_fy(
        self,
        gaap: dict[str, Any],
        *,
        period_end: str,
        gross_accn: str,
    ) -> TypedAnnualFact | None:
        for tag in TAG_INTEREST_INCOME_PURE:
            fact = self.fact_reader.find_annual_facts(gaap, (tag,)).get(period_end)
            if fact is not None and fact.accn == gross_accn:
                return fact
        return None

    def _find_matching_pure_interest_income_ltm(
        self,
        gaap: dict[str, Any],
        *,
        fy_end: str,
        q_end: str,
        q_fp: str,
        q_accn: str,
        gross_ltm: TypedLtmSeries,
    ) -> TypedLtmSeries | None:
        expected_accns = (gross_ltm.fy_accn, gross_ltm.accn, gross_ltm.prior_accn, gross_ltm.prior_end)
        for tag in TAG_INTEREST_INCOME_PURE:
            inc_ltm = self.fact_reader.calc_ltm_same_tag(gaap, (tag,), fy_end, q_end, q_fp, q_accn)
            if inc_ltm is not None and (
                inc_ltm.fy_accn,
                inc_ltm.accn,
                inc_ltm.prior_accn,
                inc_ltm.prior_end,
            ) == expected_accns:
                return inc_ltm
        return None

    def _resolve_fy_interest(
        self,
        gaap: dict[str, Any],
        *,
        ticker: str,
        cik_str: str | None,
        target_period: str,
        latest_end: str,
        bs_accn: str,
        eff_method: MethodologyVersion,
        gross_int_units: dict[str, TypedAnnualFact],
        net_int_units: dict[str, TypedAnnualFact],
    ) -> tuple[Decimal, str]:
        has_gross = latest_end in gross_int_units
        has_net = latest_end in net_int_units
        if eff_method != MethodologyVersion.V3:
            if has_gross:
                int_data = gross_int_units[latest_end]
                return int_data.value, f"SEC 10-K accn:{int_data.accn} tag:{int_data.tag}"
            if has_net:
                int_data = net_int_units[latest_end]
                return -int_data.value, f"SEC 10-K accn:{int_data.accn} tag:{int_data.tag} (sign inverted to expense basis)"
            raise KeyError(
                f"Ticker {ticker} is missing annual interest expense for {latest_end}; "
                "cannot compute a consistent EBITDA from reported facts"
            )

        if has_net:
            int_data = net_int_units[latest_end]
            return -int_data.value, f"SEC 10-K accn:{int_data.accn} tag:{int_data.tag} (sign inverted to expense basis)"
        if has_gross:
            gross_data = gross_int_units[latest_end]
            excluded_tranches = self._find_excluded_debt_tranche(
                gaap,
                int_tag=gross_data.tag,
                period_end=latest_end,
                accn=bs_accn,
                allowed_forms=ANNUAL_FORMS,
            )
            if excluded_tranches:
                raise MissingEvidenceError(
                    ProviderFailure(
                        ticker=ticker,
                        cik=cik_str,
                        period=target_period,
                        stage=FailureStage.XBRL_PARSE,
                        reason_code=FailureReasonCode.UNRESOLVED_SCOPE,
                        message=(
                            f"Ticker {ticker} uses partial-tranche interest tag {gross_data.tag} "
                            f"for {latest_end} while excluded balance-sheet debt tranches exist ({excluded_tranches})"
                        ),
                        source=f"SEC 10-K accn:{gross_data.accn}",
                        retryable=False,
                    )
                )
            pure_inc = self._find_matching_pure_interest_income_fy(
                gaap, period_end=latest_end, gross_accn=gross_data.accn
            )
            if pure_inc is not None:
                net_exp = gross_data.value - pure_inc.value
                ref = (
                    f"SEC 10-K accn:{gross_data.accn} net:{gross_data.tag} ({gross_data.value}) "
                    f"- {pure_inc.tag} ({pure_inc.value})"
                )
                return net_exp, ref
            raise MissingEvidenceError(
                ProviderFailure(
                    ticker=ticker,
                    cik=cik_str,
                    period=target_period,
                    stage=FailureStage.XBRL_PARSE,
                    reason_code=FailureReasonCode.UNRESOLVED_SCOPE,
                    message=(
                        f"Ticker {ticker} reports gross interest expense ({gross_data.tag}) "
                        f"for {latest_end} without net interest or same-accession pure interest income evidence"
                    ),
                    source=f"SEC 10-K accn:{gross_data.accn}",
                    retryable=False,
                )
            )
        raise MissingEvidenceError(
            ProviderFailure(
                ticker=ticker,
                cik=cik_str,
                period=target_period,
                stage=FailureStage.XBRL_PARSE,
                reason_code=FailureReasonCode.FACT_MISSING,
                message=(
                    f"Ticker {ticker} is missing annual interest expense for {latest_end}; "
                    "cannot compute a consistent EBITDA from reported facts"
                ),
                source=f"SEC 10-K {latest_end}",
                retryable=False,
            )
        )

    def _resolve_ltm_interest(
        self,
        gaap: dict[str, Any],
        *,
        ticker: str,
        cik_str: str | None,
        fy_end: str,
        q_end: str,
        q_fp: str,
        q_accn: str,
        mrq_forms: tuple[str, ...],
        eff_method: MethodologyVersion,
    ) -> tuple[Decimal, str]:
        gross_int_ltm = self.fact_reader.calc_ltm_same_tag(
            gaap, TAG_INTEREST_EXPENSE_GROSS, fy_end, q_end, q_fp, q_accn
        )
        net_int_ltm = (
            self.fact_reader.calc_ltm_same_tag(
                gaap, TAG_INTEREST_INCOME_EXPENSE_NET, fy_end, q_end, q_fp, q_accn
            )
            if (eff_method == MethodologyVersion.V3 or gross_int_ltm is None)
            else None
        )
        if eff_method != MethodologyVersion.V3:
            if gross_int_ltm is not None:
                return (
                    gross_int_ltm.value,
                    f"SEC LTM ({fy_end} + {q_fp} {q_end} - {gross_int_ltm.prior_end}) "
                    f"accn:{gross_int_ltm.accn} FY:{gross_int_ltm.fy_accn} "
                    f"prior:{gross_int_ltm.prior_accn} tag:{gross_int_ltm.tag}",
                )
            if net_int_ltm is not None:
                return (
                    -net_int_ltm.value,
                    f"SEC LTM ({fy_end} + {q_fp} {q_end} - {net_int_ltm.prior_end}) "
                    f"accn:{net_int_ltm.accn} FY:{net_int_ltm.fy_accn} "
                    f"prior:{net_int_ltm.prior_accn} tag:{net_int_ltm.tag} (sign inverted to expense basis)",
                )
            raise KeyError(f"Ticker {ticker} is missing matched FY/YTD interest series for LTM {q_end}")

        if net_int_ltm is not None:
            return (
                -net_int_ltm.value,
                f"SEC LTM ({fy_end} + {q_fp} {q_end} - {net_int_ltm.prior_end}) "
                f"accn:{net_int_ltm.accn} FY:{net_int_ltm.fy_accn} "
                f"prior:{net_int_ltm.prior_accn} tag:{net_int_ltm.tag} (sign inverted to expense basis)",
            )
        if gross_int_ltm is not None:
            excluded_tranches = self._find_excluded_debt_tranche(
                gaap,
                int_tag=gross_int_ltm.tag,
                period_end=q_end,
                accn=q_accn,
                allowed_forms=mrq_forms,
            )
            if excluded_tranches:
                raise MissingEvidenceError(
                    ProviderFailure(
                        ticker=ticker,
                        cik=cik_str,
                        period="LTM",
                        stage=FailureStage.XBRL_PARSE,
                        reason_code=FailureReasonCode.UNRESOLVED_SCOPE,
                        message=(
                            f"Ticker {ticker} uses partial-tranche interest tag {gross_int_ltm.tag} "
                            f"for LTM {q_end} while excluded MRQ debt tranches exist ({excluded_tranches})"
                        ),
                        source=f"SEC LTM {q_end}",
                        retryable=False,
                    )
                )
            inc_ltm = self._find_matching_pure_interest_income_ltm(
                gaap, fy_end=fy_end, q_end=q_end, q_fp=q_fp, q_accn=q_accn, gross_ltm=gross_int_ltm
            )
            if inc_ltm is not None:
                net_exp = gross_int_ltm.value - inc_ltm.value
                ref = (
                    f"SEC LTM ({fy_end} + {q_fp} {q_end} - {gross_int_ltm.prior_end}) "
                    f"accn:{gross_int_ltm.accn} FY:{gross_int_ltm.fy_accn} "
                    f"prior:{gross_int_ltm.prior_accn} tags:{gross_int_ltm.tag} - {inc_ltm.tag}"
                )
                return net_exp, ref
            raise MissingEvidenceError(
                ProviderFailure(
                    ticker=ticker,
                    cik=cik_str,
                    period="LTM",
                    stage=FailureStage.XBRL_PARSE,
                    reason_code=FailureReasonCode.UNRESOLVED_SCOPE,
                    message=f"Ticker {ticker} reports gross interest expense ({gross_int_ltm.tag}) for LTM {q_end} without net interest or matching-accession pure interest income evidence",
                    source=f"SEC LTM {q_end}",
                    retryable=False,
                )
            )
        raise MissingEvidenceError(
            ProviderFailure(
                ticker=ticker,
                cik=cik_str,
                period="LTM",
                stage=FailureStage.XBRL_PARSE,
                reason_code=FailureReasonCode.FACT_MISSING,
                message=f"Ticker {ticker} is missing matched FY/YTD interest series for LTM {q_end}",
                source=f"SEC LTM {q_end}",
                retryable=False,
            )
        )

    def _resolve_market_cap_from_cover(
        self,
        *,
        ticker: str,
        cik: int | None,
        cik_str: str | None,
        gaap: dict[str, Any],
        dei: dict[str, Any],
        allowed_forms: tuple[str, ...],
        cover_accn: str,
        form_label: str,
        target_period: str,
        eff_method: MethodologyVersion,
        note_suffix: str = "",
    ) -> tuple[Decimal, date, date, str]:
        supplements = (
            self.share_class_supplements.get((cik, cover_accn))
            if (eff_method == MethodologyVersion.V3 and cik is not None)
            else None
        )
        agg_shares = (
            self.fact_reader.find_aggregate_common_shares(
                gaap, accn=cover_accn, allowed_forms=allowed_forms
            )
            if eff_method == MethodologyVersion.V3
            else None
        )

        if supplements:
            seen_labels: set[str] = set()
            seen_tickers: set[str] = set()
            for comp in supplements:
                if not comp.shares.is_finite() or comp.shares <= 0:
                    raise ValueError(
                        f"Ticker {ticker} supplemental share class {comp.class_label!r} count must be positive"
                    )
                lbl_key = comp.class_label.strip().upper()
                if lbl_key in seen_labels:
                    raise MissingEvidenceError(
                        ProviderFailure(
                            ticker=ticker,
                            cik=cik_str,
                            period=target_period,
                            stage=FailureStage.XBRL_PARSE,
                            reason_code=FailureReasonCode.CONFLICTING_EVIDENCE,
                            message=f"Ticker {ticker} has duplicate supplemental share-class entry {comp.class_label!r} on {form_label} cover {cover_accn}",
                            source=f"SEC {form_label} accn:{cover_accn}",
                            retryable=False,
                        )
                    )
                seen_labels.add(lbl_key)
                if comp.ticker and comp.ticker.strip():
                    t_key = comp.ticker.strip().upper()
                    if t_key in seen_tickers:
                        raise MissingEvidenceError(
                            ProviderFailure(
                                ticker=ticker,
                                cik=cik_str,
                                period=target_period,
                                stage=FailureStage.XBRL_PARSE,
                                reason_code=FailureReasonCode.CONFLICTING_EVIDENCE,
                                message=f"Ticker {ticker} has overlapping supplemental symbol {t_key} on {form_label} cover {cover_accn}",
                                source=f"SEC {form_label} accn:{cover_accn}",
                                retryable=False,
                            )
                        )
                    seen_tickers.add(t_key)

            comp_sum = sum((c.shares for c in supplements), Decimal(0))
            if agg_shares is not None and agg_shares.value > 0:
                same_date = any(c.cover_date.isoformat() == agg_shares.period_end for c in supplements)
                tol = max(Decimal("1"), agg_shares.value * Decimal("0.01"))
                over_double = comp_sum >= agg_shares.value * Decimal("1.5")
                same_date_mismatch = same_date and abs(comp_sum - agg_shares.value) > tol
                if over_double or same_date_mismatch:
                    raise MissingEvidenceError(
                        ProviderFailure(
                            ticker=ticker,
                            cik=cik_str,
                            period=target_period,
                            stage=FailureStage.XBRL_PARSE,
                            reason_code=FailureReasonCode.CONFLICTING_EVIDENCE,
                            message=(
                                f"Ticker {ticker} share-class sum ({comp_sum}) conflicts with "
                                f"aggregate CommonStockSharesOutstanding ({agg_shares.value}) on {form_label} accn:{cover_accn}"
                            ),
                            source=f"SEC {form_label} accn:{cover_accn}",
                            retryable=False,
                        )
                    )

            unlisted = [c for c in supplements if not (c.ticker or "").strip()]
            if unlisted:
                verified_desc = ", ".join(
                    f"{c.class_label} ({c.ticker}: {c.shares:,})"
                    for c in supplements
                    if (c.ticker or "").strip()
                )
                unlisted_desc = ", ".join(f"{c.class_label} ({c.shares:,})" for c in unlisted)
                raise MissingEvidenceError(
                    ProviderFailure(
                        ticker=ticker,
                        cik=cik_str,
                        period=target_period,
                        stage=FailureStage.XBRL_PARSE,
                        reason_code=FailureReasonCode.UNRESOLVED_SCOPE,
                        message=(
                            f"Ticker {ticker} includes unlisted/unpriced share class(es) [{unlisted_desc}] "
                            f"alongside listed class(es) [{verified_desc or 'none'}] on {form_label} cover {cover_accn}"
                        ),
                        source=f"SEC {form_label} accn:{cover_accn}",
                        retryable=False,
                    )
                )

            mcap_val = Decimal(0)
            max_cover = supplements[0].cover_date
            max_price_date: date | None = None
            parts: list[str] = []
            for comp in supplements:
                sym = (comp.ticker or "").strip().upper()
                px, px_dt, px_src = self.price_lookup(sym, comp.cover_date, form_label)
                if not px.is_finite() or px <= 0:
                    raise ValueError(f"Ticker {sym} closing price must be positive")
                mcap_val += comp.shares * px
                if comp.cover_date > max_cover:
                    max_cover = comp.cover_date
                if max_price_date is None or px_dt > max_price_date:
                    max_price_date = px_dt
                parts.append(
                    f"{comp.class_label} [{sym}]: {comp.shares} as of {comp.cover_date.isoformat()} "
                    f"x ${px} on {px_dt.isoformat()} ({px_src})"
                )
            assert max_price_date is not None
            return (
                mcap_val,
                max_cover,
                max_price_date,
                f"{form_label} accession {cover_accn}; multi-class sum ({'; '.join(parts)}){note_suffix}",
            )

        if eff_method == MethodologyVersion.V3 and cik is not None and self.listed_tickers_lookup is not None:
            eq_tickers = self.listed_tickers_lookup(cik, ticker)
            if len(eq_tickers) > 1:
                raise MissingEvidenceError(
                    ProviderFailure(
                        ticker=ticker,
                        cik=cik_str,
                        period=target_period,
                        stage=FailureStage.XBRL_PARSE,
                        reason_code=FailureReasonCode.UNRESOLVED_SCOPE,
                        message=(
                            f"Ticker {ticker} shares CIK {cik_str} across multiple listed symbols {eq_tickers}; "
                            "class share scope is unresolved without per-class verification"
                        ),
                        source="sec_exchange_directory",
                        retryable=False,
                    )
                )

        shares = self.fact_reader.find_cover_shares(
            dei, allowed_forms=allowed_forms, accn=cover_accn
        )
        if not shares:
            agg_note = (
                f"; aggregate CommonStockSharesOutstanding ({agg_shares.value}) cannot substitute unverified cover classes"
                if agg_shares is not None
                else ""
            )
            detail = (
                f"Ticker {ticker} has no shares-outstanding fact in the latest 10-K (accession {cover_accn}){agg_note}"
                if form_label == "10-K"
                else f"Ticker {ticker} has no shares-outstanding fact in latest 10-Q ({cover_accn}){agg_note}"
            )
            raise MissingEvidenceError(
                ProviderFailure(
                    ticker=ticker,
                    cik=cik_str,
                    period=target_period,
                    stage=FailureStage.XBRL_PARSE,
                    reason_code=FailureReasonCode.FACT_MISSING,
                    message=detail,
                    source=f"SEC {form_label} accn:{cover_accn}",
                    retryable=False,
                )
            )
        share_entry = max(shares, key=lambda s: s.end_date)
        if eff_method == MethodologyVersion.V3:
            distinct = {(s.end_date, s.value) for s in shares}
            if len(distinct) > 1:
                raise MissingEvidenceError(
                    ProviderFailure(
                        ticker=ticker,
                        cik=cik_str,
                        period=target_period,
                        stage=FailureStage.XBRL_PARSE,
                        reason_code=FailureReasonCode.CONFLICTING_EVIDENCE,
                        message=f"Ticker {ticker} has multiple conflicting share-count entries on {form_label} cover {cover_accn}",
                        source=f"SEC {form_label} accn:{cover_accn}",
                        retryable=False,
                    )
                )
            if agg_shares is not None and agg_shares.period_end == share_entry.end_date.isoformat():
                tol = max(Decimal("1"), agg_shares.value * Decimal("0.02"))
                if abs(agg_shares.value - share_entry.value) > tol:
                    raise MissingEvidenceError(
                        ProviderFailure(
                            ticker=ticker,
                            cik=cik_str,
                            period=target_period,
                            stage=FailureStage.XBRL_PARSE,
                            reason_code=FailureReasonCode.CONFLICTING_EVIDENCE,
                            message=(
                                f"Ticker {ticker} cover shares ({share_entry.value}) conflict with "
                                f"same-date CommonStockSharesOutstanding ({agg_shares.value}) on {form_label} accn:{cover_accn}"
                            ),
                            source=f"SEC {form_label} accn:{cover_accn}",
                            retryable=False,
                        )
                    )
        closing_price, price_date, price_source = self.price_lookup(
            ticker, share_entry.end_date, form_label
        )
        if (
            not share_entry.value.is_finite()
            or not closing_price.is_finite()
            or share_entry.value <= 0
            or closing_price <= 0
        ):
            raise ValueError(f"Ticker {ticker} shares outstanding and closing price must be positive")
        mcap_val = share_entry.value * closing_price
        mcap_ref = (
            f"{form_label} accession {cover_accn}; shares {share_entry.value} "
            f"as of {share_entry.end_date.isoformat()} x "
            f"closing price ${closing_price} on {price_date.isoformat()}; "
            f"price source: {price_source}{note_suffix}"
        )
        return mcap_val, share_entry.end_date, price_date, mcap_ref

    def build_fy(
        self,
        ticker: str,
        gaap: dict[str, Any],
        dei: dict[str, Any],
        *,
        target_period: str = "FY",
        provenance_note: str = "",
        required_end: str,
        cik: int | None = None,
        method_version: MethodologyVersion | None = None,
    ) -> list[FinancialDatapoint]:
        """Build point-in-time FY FinancialDatapoints anchored on required_end 10-K filing."""
        eff_method = method_version or normalize_method_version(DEFAULT_METHOD_VERSION)
        cik_str = f"{cik:010d}" if cik is not None else None

        inc_units = self.fact_reader.find_annual_facts(gaap, TAG_INCOME_CONTINUING)
        tax_units = self.fact_reader.find_annual_facts(gaap, TAG_TAX_EXPENSE)
        gross_int_units = self.fact_reader.find_annual_facts(gaap, TAG_INTEREST_EXPENSE_GROSS)
        net_int_units = self.fact_reader.find_annual_facts(gaap, TAG_INTEREST_INCOME_EXPENSE_NET)
        da_units, _ = self.fact_reader.find_da_annual_facts(gaap, allow_partial=False)

        if not inc_units or not tax_units or not da_units:
            raise KeyError(f"Ticker {ticker} missing essential annual 10-K GAAP lines")

        common_ends = set(inc_units) & set(tax_units) & set(da_units)
        if not common_ends:
            raise KeyError(f"Ticker {ticker} has no common 10-K period_end across income lines")
        if required_end not in common_ends:
            raise KeyError(f"{ticker} latest 10-K {required_end} lacks complete annual GAAP lines")

        latest_end = required_end
        inc_data = inc_units[latest_end]
        tax_data = tax_units[latest_end]
        da_data = da_units[latest_end]

        int_val, int_ref = self._resolve_fy_interest(
            gaap,
            ticker=ticker,
            cik_str=cik_str,
            target_period=target_period,
            latest_end=latest_end,
            bs_accn=inc_data.accn,
            eff_method=eff_method,
            gross_int_units=gross_int_units,
            net_int_units=net_int_units,
        )

        cash_val, cash_tag, cash_item = self._resolve_cash_and_sti(
            gaap,
            ticker=ticker,
            period_end=latest_end,
            accn=inc_data.accn,
            allowed_forms=ANNUAL_FORMS,
            missing_label="a cash balance-sheet fact",
        )
        total_debt, debt_tags_used, debt_ev = self._validate_debt_for_method(
            gaap,
            ticker=ticker,
            cik_str=cik_str,
            period=target_period,
            period_end=latest_end,
            accn=inc_data.accn,
            allowed_forms=ANNUAL_FORMS,
            eff_method=eff_method,
            label_prefix="debt",
            source_label="SEC 10-K",
        )

        rev_units = self.fact_reader.find_annual_facts(gaap, TAG_REVENUE)
        rev_val = rev_units[latest_end].value if latest_end in rev_units else None
        rev_tag = rev_units[latest_end].tag if latest_end in rev_units else None

        pref_item = self.fact_reader.find_instant_fact(
            gaap, ("PreferredStockValue", "PreferredStockCarryingAmount"), latest_end, accn=inc_data.accn
        )
        pref_val = pref_item.value if pref_item is not None else Decimal(0)
        pref_ref = (
            f"SEC 10-K accn:{pref_item.accn} tag:{pref_item.tag}"
            if pref_item is not None
            else "SEC 10-K (not reported; defaulted to 0)"
        )

        nci_item = self.fact_reader.find_instant_fact(
            gaap, ("MinorityInterest", "NoncontrollingInterestInVariableInterestEntity"), latest_end, accn=inc_data.accn
        )
        nci_val = nci_item.value if nci_item is not None else Decimal(0)
        nci_ref = (
            f"SEC 10-K accn:{nci_item.accn} tag:{nci_item.tag}"
            if nci_item is not None
            else "SEC 10-K (not reported; defaulted to 0)"
        )

        p_date = date.fromisoformat(latest_end)
        note_suffix = f" ({provenance_note})" if provenance_note else ""
        mcap_val, _, price_date, mcap_ref = self._resolve_market_cap_from_cover(
            ticker=ticker,
            cik=cik,
            cik_str=cik_str,
            gaap=gaap,
            dei=dei,
            allowed_forms=ANNUAL_FORMS,
            cover_accn=inc_data.accn,
            form_label="10-K",
            target_period=target_period,
            eff_method=eff_method,
            note_suffix=note_suffix,
        )
        debt_ref = self._format_debt_source_ref(
            prefix="SEC 10-K",
            debt_tags_used=debt_tags_used,
            debt_ev=debt_ev,
            eff_method=eff_method,
            note_suffix=note_suffix,
        )

        now = self.clock()
        inc_filed_dt = date.fromisoformat(inc_data.filed) if getattr(inc_data, "filed", None) else None
        inc_filed_tag = f" filed:{inc_data.filed}" if getattr(inc_data, "filed", None) else ""
        pts = [
            self.make_datapoint(ticker=ticker, metric="market_cap", value=mcap_val, period=target_period, period_end=price_date, source_type=SourceType.MARKET_DATA, source_ref=mcap_ref, retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="total_debt", value=total_debt, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=debt_ref, retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="cash", value=cash_val, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{cash_item.accn} tag:{cash_tag}{note_suffix}", retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="income_continuing_ops", value=inc_data.value, period=target_period, period_end=p_date, filing_date=inc_filed_dt, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{inc_data.accn} tag:{inc_data.tag}{inc_filed_tag}{note_suffix}", retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="interest_expense_net", value=int_val, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"{int_ref}{note_suffix}", retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="income_tax_expense", value=tax_data.value, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{tax_data.accn} tag:{tax_data.tag}{note_suffix}", retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="depreciation_amortization", value=da_data.value, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{da_data.accn} tag:{da_data.tag}{note_suffix}", retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="preferred_equity", value=pref_val, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"{pref_ref}{note_suffix}", retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="non_controlling_interests", value=nci_val, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"{nci_ref}{note_suffix}", retrieved_at=now),
        ]

        if rev_val is not None:
            pts.append(self.make_datapoint(ticker=ticker, metric="revenue", value=rev_val, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K tag:{rev_tag}{note_suffix}", retrieved_at=now))
            prior_ends = sorted(
                end_s for end_s in rev_units
                if 300 <= (p_date - date.fromisoformat(end_s)).days <= 430
            )
            if prior_ends:
                prev_rev = rev_units[prior_ends[-1]].value
                if prev_rev > 0:
                    yoy = (rev_val / prev_rev) - Decimal(1)
                    pts.append(self.make_datapoint(ticker=ticker, metric="revenue_growth_yoy", value=yoy, currency="ratio", period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K YoY {rev_tag}: {prior_ends[-1]} -> {latest_end}{note_suffix}", retrieved_at=now))

        cogs_item = self.fact_reader.find_annual_facts(gaap, TAG_COGS).get(latest_end)
        gp_item = self.fact_reader.find_annual_facts(gaap, TAG_GROSS_PROFIT).get(latest_end)
        if cogs_item is not None:
            pts.append(self.make_datapoint(ticker=ticker, metric="cogs", value=cogs_item.value, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{cogs_item.accn} tag:{cogs_item.tag}{note_suffix}", retrieved_at=now))
        if gp_item is not None:
            pts.append(self.make_datapoint(ticker=ticker, metric="gross_profit", value=gp_item.value, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{gp_item.accn} tag:{gp_item.tag}{note_suffix}", retrieved_at=now))
        elif rev_val is not None and cogs_item is not None and cogs_item.accn == inc_data.accn:
            pts.append(self.make_datapoint(ticker=ticker, metric="gross_profit", value=rev_val - cogs_item.value, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"Derived revenue - {cogs_item.tag} (accn:{cogs_item.accn}){note_suffix}", retrieved_at=now))

        ni_units = self.fact_reader.find_annual_facts(gaap, ("NetIncomeLoss", "ProfitLoss"))
        if latest_end in ni_units:
            ni_item = ni_units[latest_end]
            pts.append(self.make_datapoint(ticker=ticker, metric="net_income", value=ni_item.value, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{ni_item.accn} tag:{ni_item.tag}{note_suffix}", retrieved_at=now))

        if latest_end in gross_int_units:
            gi_item = gross_int_units[latest_end]
            pts.append(self.make_datapoint(ticker=ticker, metric="interest_expense", value=gi_item.value, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{gi_item.accn} tag:{gi_item.tag}{note_suffix}", retrieved_at=now))

        assets_item = self.fact_reader.find_instant_fact(gaap, TAG_ASSETS, latest_end, accn=inc_data.accn)
        if assets_item is not None:
            pts.append(self.make_datapoint(ticker=ticker, metric="total_assets", value=assets_item.value, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{assets_item.accn} tag:{assets_item.tag}{note_suffix}", retrieved_at=now))

        equity_item = self.fact_reader.find_instant_fact(gaap, TAG_EQUITY, latest_end, accn=inc_data.accn)
        if equity_item is not None:
            pts.append(self.make_datapoint(ticker=ticker, metric="total_equity", value=equity_item.value, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{equity_item.accn} tag:{equity_item.tag}{note_suffix}", retrieved_at=now))

        capex_units = self.fact_reader.find_annual_facts(gaap, TAG_CAPEX)
        if latest_end in capex_units:
            cx_item = capex_units[latest_end]
            pts.append(self.make_datapoint(ticker=ticker, metric="capex", value=cx_item.value, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{cx_item.accn} tag:{cx_item.tag}{note_suffix}", retrieved_at=now))

        pretax_v = inc_data.value + tax_data.value
        if pretax_v > 0 and tax_data.value >= 0:
            etr = max(Decimal(0), min(Decimal(1), tax_data.value / pretax_v))
            pts.append(self.make_datapoint(ticker=ticker, metric="effective_tax_rate", value=etr, currency="ratio", period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"Derived GAAP tax / (income_continuing_ops + tax){note_suffix}", retrieved_at=now))

        if eff_method == MethodologyVersion.V3:
            em_inc = self.fact_reader.find_annual_facts(gaap, TAG_EQUITY_METHOD_INCOME).get(latest_end)
            if em_inc is not None:
                pts.append(self.make_datapoint(ticker=ticker, metric="equity_method_income", value=em_inc.value, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{em_inc.accn} tag:{em_inc.tag}{note_suffix}", retrieved_at=now))
            em_bs = self.fact_reader.find_instant_fact(gaap, TAG_EQUITY_METHOD_BS, latest_end, accn=inc_data.accn, allowed_forms=ANNUAL_FORMS)
            if em_bs is not None:
                pts.append(self.make_datapoint(ticker=ticker, metric="equity_method_investments", value=em_bs.value, period=target_period, period_end=p_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-K accn:{em_bs.accn} tag:{em_bs.tag}{note_suffix}", retrieved_at=now))

        return pts

    def build_ltm(
        self,
        ticker: str,
        gaap: dict[str, Any],
        dei: dict[str, Any],
        required_fy: str,
        latest_q: str,
        *,
        cik: int | None = None,
        method_version: MethodologyVersion | None = None,
    ) -> list[FinancialDatapoint]:
        """Compute LTM = FY + Curr_YTD - Prior_YTD paired with MRQ balance sheet & shares."""
        eff_method = method_version or normalize_method_version(DEFAULT_METHOD_VERSION)
        cik_str = f"{cik:010d}" if cik is not None else None

        inc_fy = self.fact_reader.find_annual_facts(gaap, TAG_INCOME_CONTINUING)
        tax_fy = self.fact_reader.find_annual_facts(gaap, TAG_TAX_EXPENSE)
        da_fy, _ = self.fact_reader.find_da_annual_facts(gaap, allow_partial=False)

        common_fy = set(inc_fy) & set(tax_fy) & set(da_fy)
        if not common_fy:
            raise KeyError(f"Ticker {ticker} missing essential annual 10-K GAAP lines for LTM anchor")
        if required_fy not in common_fy:
            raise KeyError(f"{ticker} latest 10-K {required_fy} lacks the LTM anchor")
        fy_end = required_fy
        fy_end_dt = date.fromisoformat(fy_end)

        q_cands: list[tuple[str, str, bool, str, str]] = []
        for tag in TAG_INCOME_CONTINUING:
            for e in gaap.get(tag, {}).get("units", {}).get("USD", []):
                fp = e.get("fp")
                form = e.get("form")
                if form not in QUARTERLY_FORMS or fp not in YTD_DAY_RANGES or "start" not in e or not e.get("end"):
                    continue
                if e["end"] != latest_q or e["end"] <= fy_end:
                    continue
                e_dt = date.fromisoformat(e["end"])
                s_dt = date.fromisoformat(e["start"])
                days = (e_dt - s_dt).days
                lo, hi = YTD_DAY_RANGES[fp]
                if lo <= days <= hi and -7 <= (s_dt - fy_end_dt).days <= 10:
                    q_cands.append((e["end"], e.get("filed", ""), form == "10-Q/A", e.get("accn", ""), fp))

        if latest_q > fy_end and not q_cands:
            raise KeyError(f"{ticker} latest 10-Q {latest_q} lacks verified cumulative YTD income")
        if not q_cands:
            return self.build_fy(
                ticker,
                gaap,
                dei,
                target_period="LTM",
                provenance_note="LTM equals latest 10-K FY; no subsequent 10-Q period filed",
                required_end=required_fy,
                cik=cik,
                method_version=eff_method,
            )

        q_end, q_filed, _, q_accn, q_fp = max(q_cands)
        q_date = date.fromisoformat(q_end)

        inc_ltm = self.fact_reader.calc_ltm_same_tag(gaap, TAG_INCOME_CONTINUING, fy_end, q_end, q_fp, q_accn)
        tax_ltm = self.fact_reader.calc_ltm_same_tag(gaap, TAG_TAX_EXPENSE, fy_end, q_end, q_fp, q_accn)
        da_ltm = self.fact_reader.calc_ltm_same_tag(gaap, TAG_DA_COMBINED, fy_end, q_end, q_fp, q_accn)
        if da_ltm is None:
            dep_ltm = self.fact_reader.calc_ltm_same_tag(gaap, ("Depreciation",), fy_end, q_end, q_fp, q_accn)
            amo_ltm = self.fact_reader.calc_ltm_same_tag(gaap, ("AmortizationOfIntangibleAssets",), fy_end, q_end, q_fp, q_accn)
            if (
                dep_ltm is not None
                and amo_ltm is not None
                and (dep_ltm.fy_accn, dep_ltm.accn, dep_ltm.prior_accn, dep_ltm.prior_end)
                == (amo_ltm.fy_accn, amo_ltm.accn, amo_ltm.prior_accn, amo_ltm.prior_end)
            ):
                from covenant_ai.providers.xbrl_reader import TypedLtmSeries

                da_ltm = TypedLtmSeries(
                    tag="Depreciation+AmortizationOfIntangibleAssets",
                    value=dep_ltm.value + amo_ltm.value,
                    fy_val=dep_ltm.fy_val + amo_ltm.fy_val,
                    curr_val=dep_ltm.curr_val + amo_ltm.curr_val,
                    prior_val=dep_ltm.prior_val + amo_ltm.prior_val,
                    prior_end=dep_ltm.prior_end,
                    fy_accn=dep_ltm.fy_accn,
                    accn=dep_ltm.accn,
                    prior_accn=dep_ltm.prior_accn,
                )

        if inc_ltm is None or tax_ltm is None or da_ltm is None:
            raise KeyError(f"Ticker {ticker} lacks matched cumulative YTD/FY series for LTM as of {q_end}")

        mrq_forms = QUARTERLY_FORMS + ANNUAL_FORMS
        int_val, int_ref = self._resolve_ltm_interest(
            gaap,
            ticker=ticker,
            cik_str=cik_str,
            fy_end=fy_end,
            q_end=q_end,
            q_fp=q_fp,
            q_accn=q_accn,
            mrq_forms=mrq_forms,
            eff_method=eff_method,
        )

        cash_val, cash_tag, cash_item = self._resolve_cash_and_sti(
            gaap,
            ticker=ticker,
            period_end=q_end,
            accn=q_accn,
            allowed_forms=mrq_forms,
            missing_label="MRQ cash balance sheet fact",
        )
        total_debt, debt_tags_used, debt_ev = self._validate_debt_for_method(
            gaap,
            ticker=ticker,
            cik_str=cik_str,
            period="LTM",
            period_end=q_end,
            accn=q_accn,
            allowed_forms=mrq_forms,
            eff_method=eff_method,
            label_prefix="MRQ debt",
            source_label="SEC 10-Q MRQ",
        )
        pref_item = self.fact_reader.find_instant_fact(
            gaap, ("PreferredStockValue", "PreferredStockCarryingAmount"), q_end, accn=q_accn, allowed_forms=mrq_forms
        )
        pref_val = pref_item.value if pref_item is not None else Decimal(0)
        nci_item = self.fact_reader.find_instant_fact(
            gaap, ("MinorityInterest", "NoncontrollingInterestInVariableInterestEntity"), q_end, accn=q_accn, allowed_forms=mrq_forms
        )
        nci_val = nci_item.value if nci_item is not None else Decimal(0)

        mcap_val, _, price_date, mcap_ref = self._resolve_market_cap_from_cover(
            ticker=ticker,
            cik=cik,
            cik_str=cik_str,
            gaap=gaap,
            dei=dei,
            allowed_forms=QUARTERLY_FORMS,
            cover_accn=q_accn,
            form_label="10-Q",
            target_period="LTM",
            eff_method=eff_method,
        )
        debt_ref = self._format_debt_source_ref(
            prefix=f"SEC 10-Q MRQ accn:{q_accn}",
            debt_tags_used=debt_tags_used,
            debt_ev=debt_ev,
            eff_method=eff_method,
        )

        now = self.clock()
        ltm_span = f"{fy_end} + {q_fp} {q_end} - {inc_ltm.prior_end}"
        q_filed_dt = date.fromisoformat(q_filed) if q_filed else None
        q_filed_tag = f" filed:{q_filed}" if q_filed else ""
        pts = [
            self.make_datapoint(ticker=ticker, metric="market_cap", value=mcap_val, period="LTM", period_end=price_date, source_type=SourceType.MARKET_DATA, source_ref=mcap_ref, retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="total_debt", value=total_debt, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=debt_ref, retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="cash", value=cash_val, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-Q MRQ accn:{cash_item.accn} tag:{cash_tag}", retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="income_continuing_ops", value=inc_ltm.value, period="LTM", period_end=q_date, filing_date=q_filed_dt, source_type=SourceType.SEC_FILING, source_ref=f"SEC LTM ({ltm_span}) accn:{inc_ltm.accn} FY:{inc_ltm.fy_accn} prior:{inc_ltm.prior_accn} tag:{inc_ltm.tag}{q_filed_tag}", retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="interest_expense_net", value=int_val, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=int_ref, retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="income_tax_expense", value=tax_ltm.value, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC LTM ({ltm_span}) accn:{tax_ltm.accn} FY:{tax_ltm.fy_accn} prior:{tax_ltm.prior_accn} tag:{tax_ltm.tag}", retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="depreciation_amortization", value=da_ltm.value, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC LTM ({ltm_span}) accn:{da_ltm.accn} FY:{da_ltm.fy_accn} prior:{da_ltm.prior_accn} tag:{da_ltm.tag}", retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="preferred_equity", value=pref_val, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-Q MRQ accn:{pref_item.accn} tag:{pref_item.tag}" if pref_item else "SEC 10-Q MRQ (not reported; 0)", retrieved_at=now),
            self.make_datapoint(ticker=ticker, metric="non_controlling_interests", value=nci_val, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-Q MRQ accn:{nci_item.accn} tag:{nci_item.tag}" if nci_item else "SEC 10-Q MRQ (not reported; 0)", retrieved_at=now),
        ]

        rev_ltm = self.fact_reader.calc_ltm_same_tag(gaap, TAG_REVENUE, fy_end, q_end, q_fp, q_accn)
        if rev_ltm is not None:
            pts.append(self.make_datapoint(ticker=ticker, metric="revenue", value=rev_ltm.value, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC LTM ({ltm_span}) tag:{rev_ltm.tag}", retrieved_at=now))
            if rev_ltm.prior_val > 0:
                pts.append(self.make_datapoint(ticker=ticker, metric="revenue_growth_yoy", value=(rev_ltm.curr_val / rev_ltm.prior_val) - Decimal(1), currency="ratio", period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC {q_fp} YTD YoY ({q_end} vs {rev_ltm.prior_end})", retrieved_at=now))

        gp_ltm = self.fact_reader.calc_ltm_same_tag(gaap, TAG_GROSS_PROFIT, fy_end, q_end, q_fp, q_accn)
        if gp_ltm is not None:
            pts.append(self.make_datapoint(ticker=ticker, metric="gross_profit", value=gp_ltm.value, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC LTM ({ltm_span}) tag:{gp_ltm.tag}", retrieved_at=now))

        capex_ltm = self.fact_reader.calc_ltm_same_tag(gaap, TAG_CAPEX, fy_end, q_end, q_fp, q_accn)
        if capex_ltm is not None and capex_ltm.value >= 0:
            pts.append(self.make_datapoint(ticker=ticker, metric="capex", value=capex_ltm.value, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC LTM ({ltm_span}) tag:{capex_ltm.tag}", retrieved_at=now))

        assets_mrq = self.fact_reader.find_instant_fact(gaap, TAG_ASSETS, q_end, accn=q_accn, allowed_forms=mrq_forms)
        if assets_mrq is not None:
            pts.append(self.make_datapoint(ticker=ticker, metric="total_assets", value=assets_mrq.value, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-Q MRQ accn:{q_accn} tag:{assets_mrq.tag}", retrieved_at=now))

        equity_mrq = self.fact_reader.find_instant_fact(gaap, TAG_EQUITY, q_end, accn=q_accn, allowed_forms=mrq_forms)
        if equity_mrq is not None:
            pts.append(self.make_datapoint(ticker=ticker, metric="total_equity", value=equity_mrq.value, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-Q MRQ accn:{q_accn} tag:{equity_mrq.tag}", retrieved_at=now))

        if eff_method == MethodologyVersion.V3:
            em_inc_ltm = self.fact_reader.calc_ltm_same_tag(gaap, TAG_EQUITY_METHOD_INCOME, fy_end, q_end, q_fp, q_accn)
            if em_inc_ltm is not None:
                pts.append(self.make_datapoint(ticker=ticker, metric="equity_method_income", value=em_inc_ltm.value, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC LTM ({ltm_span}) accn:{em_inc_ltm.accn} FY:{em_inc_ltm.fy_accn} prior:{em_inc_ltm.prior_accn} tag:{em_inc_ltm.tag}", retrieved_at=now))
            em_bs_mrq = self.fact_reader.find_instant_fact(gaap, TAG_EQUITY_METHOD_BS, q_end, accn=q_accn, allowed_forms=mrq_forms)
            if em_bs_mrq is not None:
                pts.append(self.make_datapoint(ticker=ticker, metric="equity_method_investments", value=em_bs_mrq.value, period="LTM", period_end=q_date, source_type=SourceType.SEC_FILING, source_ref=f"SEC 10-Q MRQ accn:{em_bs_mrq.accn} tag:{em_bs_mrq.tag}", retrieved_at=now))

        return pts

    def build_partial_fy_evidence(
        self,
        *,
        gaap: dict[str, Any],
        fy_end: str,
        fy_accn: str,
        method_version: MethodologyVersion | None = None,
    ) -> dict[str, EvidencedValue]:
        """Collect independent and available component evidence when full peer extraction stops."""
        eff_method = method_version or normalize_method_version(DEFAULT_METHOD_VERSION)
        is_v3 = eff_method == MethodologyVersion.V3
        raw_ev: dict[str, EvidencedValue] = {}
        rev_u = self.fact_reader.find_annual_facts(gaap, TAG_REVENUE)
        if fy_end in rev_u:
            raw_ev["revenue"] = EvidencedValue.evidenced(rev_u[fy_end].value, tags=(rev_u[fy_end].tag,))
        gp_u = self.fact_reader.find_annual_facts(gaap, TAG_GROSS_PROFIT)
        if fy_end in gp_u:
            raw_ev["gross_profit"] = EvidencedValue.evidenced(gp_u[fy_end].value, tags=(gp_u[fy_end].tag,))
        _, _, debt_ev = self.fact_reader.extract_debt_with_evidence(
            gaap,
            fy_end,
            accn=fy_accn,
            allowed_forms=ANNUAL_FORMS,
            include_operating_lease=not is_v3,
            extended_tiers=is_v3,
        )
        raw_ev["total_debt"] = debt_ev
        cash_item = self.fact_reader.find_instant_fact(gaap, TAG_CASH, fy_end, accn=fy_accn)
        if cash_item is not None:
            raw_ev["cash"] = EvidencedValue.evidenced(cash_item.value, tags=(cash_item.tag,))
        inc_u = self.fact_reader.find_annual_facts(gaap, TAG_INCOME_CONTINUING)
        if fy_end in inc_u:
            raw_ev["income_continuing_ops"] = EvidencedValue.evidenced(inc_u[fy_end].value, tags=(inc_u[fy_end].tag,))
        tax_u = self.fact_reader.find_annual_facts(gaap, TAG_TAX_EXPENSE)
        if fy_end in tax_u:
            raw_ev["income_tax_expense"] = EvidencedValue.evidenced(tax_u[fy_end].value, tags=(tax_u[fy_end].tag,))
        return evaluate_peer_evidence(raw_ev)

    def build_outcome_from_points(
        self,
        *,
        ticker: str,
        cik_str: str | None,
        period: str,
        method_version: MethodologyVersion,
        datapoints: list[FinancialDatapoint] | None = None,
        failure: ProviderFailure | None = None,
        fallback_evidence: dict[str, EvidencedValue] | None = None,
    ) -> PeerExtractionOutcome:
        """Assemble PeerExtractionOutcome from either full datapoints or failure + partial evidence."""
        if failure is not None:
            return PeerExtractionOutcome(
                ticker=ticker,
                cik=cik_str,
                period=period,
                method_version=method_version.value,
                datapoints=[],
                evidence_by_metric=fallback_evidence or {},
                failure=failure,
            )
        pts = datapoints or []
        raw_ok = {
            dp.metric: EvidencedValue.evidenced(dp.value, tags=(dp.source_ref,))
            for dp in pts
        }
        return PeerExtractionOutcome(
            ticker=ticker,
            cik=cik_str,
            period=period,
            method_version=method_version.value,
            datapoints=pts,
            evidence_by_metric=evaluate_peer_evidence(raw_ok),
            failure=None,
        )
