"""Typed XBRL fact normalization and SEC period/debt extraction reader (P2)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from covenant_ai.domain.evidence import EvidencedValue, FailureReasonCode, FailureStage, ProviderFailure
from covenant_ai.providers.edgar_errors import MalformedSourceError
from covenant_ai.providers.edgar_tags import (
    ANNUAL_FORMS,
    QUARTERLY_FORMS,
    TAG_DA_COMBINED,
    TAG_DEBT_COMBINED_LT,
    TAG_DEBT_COMMERCIAL_PAPER,
    TAG_DEBT_CURRENT_MATURITIES,
    TAG_DEBT_CURRENT_MATURITIES_V3,
    TAG_DEBT_CURRENT_TOTAL,
    TAG_DEBT_LOC_CURRENT,
    TAG_DEBT_LT_AND_CAP_LEASE,
    TAG_DEBT_LT_CAP_INCL_CURRENT,
    TAG_DEBT_NONCURRENT,
    TAG_DEBT_OTHER_LT,
    TAG_DEBT_SHORT_TERM_ONLY,
    TAG_DEBT_SHORT_TERM_ONLY_V3,
    TAG_DEBT_SPEC_CONVERTIBLE,
    TAG_DEBT_SPEC_LOC,
    TAG_DEBT_SPEC_OTHER_LT,
    TAG_DEBT_SPEC_SECURED_OR_SENIOR,
    TAG_FINANCE_LEASE_COMBINED,
    TAG_FINANCE_LEASE_CURRENT,
    TAG_FINANCE_LEASE_NONCURRENT,
    TAG_OPERATING_LEASE_COMBINED,
    TAG_OPERATING_LEASE_CURRENT,
    TAG_OPERATING_LEASE_NONCURRENT,
    YTD_DAY_RANGES,
)


@dataclass(frozen=True)
class TypedAnnualFact:
    """Normalized annual 10-K/10-K/A duration fact parsed at the XBRL boundary."""

    tag: str
    value: Decimal
    accn: str
    filed: str
    form: str
    period_end: str

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "val": self.value,
            "accn": self.accn,
            "filed": self.filed,
            "form": self.form,
            "period_end": self.period_end,
        }


@dataclass(frozen=True)
class TypedInstantFact:
    """Normalized instant balance-sheet fact parsed at the XBRL boundary."""

    tag: str
    value: Decimal
    accn: str
    period_end: str
    filed: str = ""
    form: str = ""

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "val": self.value,
            "accn": self.accn,
            "period_end": self.period_end,
        }


@dataclass(frozen=True)
class TypedShareFact:
    """Normalized DEI EntityCommonStockSharesOutstanding cover fact."""

    value: Decimal
    end_date: date
    accn: str
    form: str
    filed: str = ""
    frame: str = ""


@dataclass(frozen=True)
class ShareClassComponent:
    """Per-class share count with provenance from filing cover or supplemental iXBRL note."""

    class_label: str
    shares: Decimal
    cover_date: date
    accn: str
    ticker: str | None = None
    source_ref: str = ""


@dataclass(frozen=True)
class TypedLtmSeries:
    """Same-tag FY + Curr_YTD - Prior_YTD calendarized flow breakdown."""

    tag: str
    value: Decimal
    fy_val: Decimal
    curr_val: Decimal
    prior_val: Decimal
    prior_end: str
    fy_accn: str
    accn: str
    prior_accn: str

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "val": self.value,
            "fy_val": self.fy_val,
            "curr_val": self.curr_val,
            "prior_val": self.prior_val,
            "prior_end": self.prior_end,
            "fy_accn": self.fy_accn,
            "accn": self.accn,
            "prior_accn": self.prior_accn,
        }


def _parse_decimal(raw: Any, *, tag: str) -> Decimal:
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise MalformedSourceError(
            ProviderFailure(
                ticker="",
                stage=FailureStage.XBRL_PARSE,
                reason_code=FailureReasonCode.MALFORMED_SOURCE,
                message=f"Invalid numeric XBRL fact in {tag}: {raw!r}",
                source=tag,
                retryable=False,
            )
        ) from exc
    if not d.is_finite():
        raise MalformedSourceError(
            ProviderFailure(
                ticker="",
                stage=FailureStage.XBRL_PARSE,
                reason_code=FailureReasonCode.MALFORMED_SOURCE,
                message=f"Non-finite XBRL value in {tag}: {raw!r}",
                source=tag,
                retryable=False,
            )
        )
    return d


def _parse_iso_date(raw: Any, *, tag: str) -> date:
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise MalformedSourceError(
            ProviderFailure(
                ticker="",
                stage=FailureStage.XBRL_PARSE,
                reason_code=FailureReasonCode.MALFORMED_SOURCE,
                message=f"Invalid ISO date in XBRL fact {tag}: {raw!r}",
                source=tag,
                retryable=False,
            )
        ) from exc


class XbrlFactReader:
    """Converts raw SEC companyfacts JSON into typed, unit/duration-verified facts (P2)."""

    def find_annual_facts(
        self,
        gaap: dict[str, Any],
        tags: tuple[str, ...],
    ) -> dict[str, TypedAnnualFact]:
        """Return period_end -> TypedAnnualFact for annual 10-K / 10-K/A duration items."""
        out: dict[str, TypedAnnualFact] = {}
        for tag in tags:
            best_by_end: dict[str, TypedAnnualFact] = {}
            for entry in gaap.get(tag, {}).get("units", {}).get("USD", []):
                form = entry.get("form")
                if form not in ANNUAL_FORMS or entry.get("fp") != "FY" or "start" not in entry or not entry.get("end"):
                    continue
                if entry.get("val") is None:
                    continue
                end_dt_obj = _parse_iso_date(entry["end"], tag=tag)
                start_dt_obj = _parse_iso_date(entry["start"], tag=tag)
                days = (end_dt_obj - start_dt_obj).days
                if not 340 <= days <= 380:
                    continue
                end_dt = str(entry["end"])
                cand = TypedAnnualFact(
                    tag=tag,
                    value=_parse_decimal(entry["val"], tag=tag),
                    accn=str(entry.get("accn") or ""),
                    filed=str(entry.get("filed") or ""),
                    form=str(form),
                    period_end=end_dt,
                )
                prev = best_by_end.get(end_dt)
                cand_rank = (cand.filed, cand.form == "10-K/A", cand.accn)
                prev_rank = (prev.filed, prev.form == "10-K/A", prev.accn) if prev else None
                if prev_rank is None or cand_rank > prev_rank:
                    best_by_end[end_dt] = cand
            for end_dt, cand in best_by_end.items():
                out.setdefault(end_dt, cand)
        return out

    def find_da_annual_facts(
        self,
        gaap: dict[str, Any],
        *,
        allow_partial: bool = False,
    ) -> tuple[dict[str, TypedAnnualFact], dict[str, str]]:
        """Resolve annual D&A using combined tag or same-accession Depreciation + Amortization."""
        da_facts = self.find_annual_facts(gaap, TAG_DA_COMBINED)
        da_basis = {k: "combined" for k in da_facts}

        dep_facts = self.find_annual_facts(gaap, ("Depreciation",))
        amo_facts = self.find_annual_facts(gaap, ("AmortizationOfIntangibleAssets",))
        for end_dt, dep in dep_facts.items():
            if end_dt in da_facts:
                continue
            amo = amo_facts.get(end_dt)
            if amo and dep.accn == amo.accn:
                da_facts[end_dt] = TypedAnnualFact(
                    tag="Depreciation+AmortizationOfIntangibleAssets",
                    value=dep.value + amo.value,
                    accn=dep.accn,
                    filed=dep.filed or amo.filed,
                    form=dep.form,
                    period_end=end_dt,
                )
                da_basis[end_dt] = "composite"
            elif allow_partial:
                da_facts[end_dt] = TypedAnnualFact(
                    tag="Depreciation(partial)",
                    value=dep.value,
                    accn=dep.accn,
                    filed=dep.filed,
                    form=dep.form,
                    period_end=end_dt,
                )
                da_basis[end_dt] = "depreciation_only"
        return da_facts, da_basis

    def find_instant_fact(
        self,
        gaap: dict[str, Any],
        tags: tuple[str, ...],
        period_end: str,
        *,
        accn: str | None = None,
        allowed_forms: tuple[str, ...] = ANNUAL_FORMS,
    ) -> TypedInstantFact | None:
        """Find balance sheet instant fact at period_end preferring latest amendment then accession."""
        for tag in tags:
            matches = [
                entry
                for entry in gaap.get(tag, {}).get("units", {}).get("USD", [])
                if entry.get("end") == period_end and entry.get("form") in allowed_forms
            ]
            if matches:
                matches.sort(
                    key=lambda e: (
                        e.get("filed", ""),
                        str(e.get("form", "")).endswith("/A"),
                        bool(accn and e.get("accn") == accn),
                        e.get("accn", ""),
                    ),
                    reverse=True,
                )
                entry = matches[0]
                if entry.get("val") is None:
                    continue
                return TypedInstantFact(
                    tag=tag,
                    value=_parse_decimal(entry["val"], tag=tag),
                    accn=str(entry.get("accn") or ""),
                    period_end=period_end,
                    filed=str(entry.get("filed") or ""),
                    form=str(entry.get("form") or ""),
                )
        return None

    def extract_debt_with_evidence(
        self,
        gaap: dict[str, Any],
        period_end: str,
        accn: str,
        *,
        allowed_forms: tuple[str, ...] = ANNUAL_FORMS,
        include_operating_lease: bool = True,
        extended_tiers: bool = False,
    ) -> tuple[Decimal, list[str], EvidencedValue]:
        """Resolve 5-tier mutually exclusive debt + leases returning structured EvidencedValue."""
        total = Decimal(0)
        core_total = Decimal(0)
        finance_total = Decimal(0)
        operating_total = Decimal(0)
        tags_used: list[str] = []
        matched_core_tags: list[str] = []
        matched_finance_tags: list[str] = []
        matched_operating_tags: list[str] = []
        includes_finance_lease = False
        comb_convertible_matched = False
        comb_loc_matched = False

        def pick(tags: tuple[str, ...]) -> TypedInstantFact | None:
            return self.find_instant_fact(
                gaap, tags, period_end, accn=accn, allowed_forms=allowed_forms
            )

        def add_core(item: TypedInstantFact | None) -> None:
            nonlocal total, core_total
            if item is not None:
                matched_core_tags.append(item.tag)
                if item.value != 0:
                    total += item.value
                    core_total += item.value
                    tags_used.append(item.tag)

        maturities_tags = TAG_DEBT_CURRENT_MATURITIES_V3 if extended_tiers else TAG_DEBT_CURRENT_MATURITIES
        st_tags = TAG_DEBT_SHORT_TERM_ONLY_V3 if extended_tiers else TAG_DEBT_SHORT_TERM_ONLY

        def add_current_debt(allow_debt_current: bool, allow_lt_maturities: bool) -> None:
            if allow_debt_current:
                dc = pick(TAG_DEBT_CURRENT_TOTAL)
                if dc is not None:
                    add_core(dc)
                    return
            if allow_lt_maturities:
                if extended_tiers:
                    seen_cur: set[str] = set()
                    for grp in (
                        ("LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"),
                        ("SeniorNotesCurrent",),
                        ("ConvertibleDebtCurrent",),
                        ("OtherLongTermDebtCurrent",),
                    ):
                        if comb_convertible_matched and grp == ("ConvertibleDebtCurrent",):
                            continue
                        item = pick(grp)
                        if item is not None:
                            # LongTermDebtCurrent often rolls up specific current maturities; stop if primary group matched
                            if grp[0] == "LongTermDebtCurrent" and item.value != 0:
                                add_core(item)
                                break
                            if item.tag not in seen_cur:
                                seen_cur.add(item.tag)
                                add_core(item)
                else:
                    add_core(pick(maturities_tags))
            stb = pick(st_tags)
            if stb is not None:
                add_core(stb)
            else:
                cp = pick(TAG_DEBT_COMMERCIAL_PAPER)
                if cp is not None:
                    add_core(cp)
                elif extended_tiers and allow_lt_maturities and not comb_loc_matched:
                    add_core(pick(TAG_DEBT_LOC_CURRENT))

        ltd_nc = pick(TAG_DEBT_NONCURRENT)
        ltd_cap_incl = pick(TAG_DEBT_LT_CAP_INCL_CURRENT)
        ltd_comb = pick(TAG_DEBT_COMBINED_LT)
        ltd_cap = pick(TAG_DEBT_LT_AND_CAP_LEASE)
        other_lt = pick(TAG_DEBT_OTHER_LT)

        if ltd_nc is not None:
            add_core(ltd_nc)
            add_current_debt(allow_debt_current=True, allow_lt_maturities=True)
        elif ltd_cap_incl is not None:
            add_core(ltd_cap_incl)
            includes_finance_lease = True
            add_current_debt(allow_debt_current=False, allow_lt_maturities=False)
        elif ltd_comb is not None:
            add_core(ltd_comb)
            add_current_debt(allow_debt_current=False, allow_lt_maturities=False)
            if ltd_cap is not None and ltd_cap.value == ltd_comb.value:
                includes_finance_lease = True
        elif ltd_cap is not None:
            add_core(ltd_cap)
            includes_finance_lease = True
            add_current_debt(allow_debt_current=True, allow_lt_maturities=True)
        elif extended_tiers:
            # Tier 5 (v3): mutually exclusive instrument-level LT categories only when Tiers 1-4 are absent
            conv_tot = pick(("ConvertibleDebt",))
            conv_nc = pick(("ConvertibleDebtNoncurrent", "ConvertibleLongTermNotesPayable"))
            conv_cur = pick(("ConvertibleDebtCurrent",))
            if conv_tot is not None and conv_nc is not None and conv_tot.accn == conv_nc.accn:
                if conv_cur is not None and conv_cur.accn == conv_nc.accn:
                    if conv_tot.value != conv_nc.value + conv_cur.value:
                        return Decimal(0), [], EvidencedValue.conflicting(
                            (
                                f"Conflicting ConvertibleDebt ({conv_tot.value}) vs "
                                f"{conv_nc.tag}+{conv_cur.tag} ({conv_nc.value + conv_cur.value}) at {period_end}"
                            ),
                            tags=("ConvertibleDebt", conv_nc.tag, conv_cur.tag),
                        )
                elif conv_tot.value != conv_nc.value:
                    lt_cur = pick(("LongTermDebtCurrent", "DebtCurrent"))
                    unexplained = (
                        conv_tot.value < conv_nc.value
                        or lt_cur is None
                        or (conv_tot.value - conv_nc.value) > lt_cur.value
                    )
                    if unexplained:
                        return Decimal(0), [], EvidencedValue.conflicting(
                            (
                                f"Conflicting ConvertibleDebt ({conv_tot.value}) vs "
                                f"{conv_nc.tag} ({conv_nc.value}) without matching current split at {period_end}"
                            ),
                            tags=("ConvertibleDebt", conv_nc.tag),
                        )
            spec_sec = pick(TAG_DEBT_SPEC_SECURED_OR_SENIOR)
            spec_conv = pick(TAG_DEBT_SPEC_CONVERTIBLE)
            spec_loc = pick(TAG_DEBT_SPEC_LOC)
            spec_other = pick(TAG_DEBT_SPEC_OTHER_LT)
            add_core(spec_sec)
            if spec_conv is not None:
                if spec_conv.tag == "ConvertibleDebt":
                    comb_convertible_matched = True
                add_core(spec_conv)
            if spec_loc is not None and spec_loc.tag == "LineOfCredit":
                comb_loc_matched = True
            add_core(spec_loc)
            add_core(spec_other)
            add_current_debt(allow_debt_current=True, allow_lt_maturities=True)
        elif other_lt is not None:
            add_core(other_lt)
            add_current_debt(allow_debt_current=True, allow_lt_maturities=True)
        else:
            add_current_debt(allow_debt_current=True, allow_lt_maturities=True)

        ol_curr = pick(TAG_OPERATING_LEASE_CURRENT)
        ol_noncurr = pick(TAG_OPERATING_LEASE_NONCURRENT)
        ol_items = [ol_curr, ol_noncurr] if (ol_curr is not None or ol_noncurr is not None) else [pick(TAG_OPERATING_LEASE_COMBINED)]
        for item in ol_items:
            if item is not None:
                matched_operating_tags.append(item.tag)
                if item.value != 0:
                    operating_total += item.value
                    if include_operating_lease:
                        total += item.value
                        tags_used.append(item.tag)

        if not includes_finance_lease:
            fl_curr = pick(TAG_FINANCE_LEASE_CURRENT)
            fl_noncurr = pick(TAG_FINANCE_LEASE_NONCURRENT)
            fl_items = [fl_curr, fl_noncurr] if (fl_curr is not None or fl_noncurr is not None) else [pick(TAG_FINANCE_LEASE_COMBINED)]
            for item in fl_items:
                if item is not None:
                    matched_finance_tags.append(item.tag)
                    if item.value != 0:
                        finance_total += item.value
                        total += item.value
                        tags_used.append(item.tag)

        components = {
            "core_debt": str(core_total),
            "finance_lease": str(finance_total),
            "operating_lease": str(operating_total),
            "operating_lease_excluded": str(operating_total if not include_operating_lease else Decimal(0)),
            "operating_lease_included": str(include_operating_lease).lower(),
            "core_tags": ",".join(matched_core_tags),
        }
        all_matched = tuple(dict.fromkeys([*matched_core_tags, *matched_finance_tags, *matched_operating_tags]))
        if matched_core_tags:
            if extended_tiers and core_total == 0:
                # A single zero tranche (e.g. only noncurrent borrowings) does not establish zero
                # total debt; require either an all-maturities combined zero or noncurrent + current zero.
                def covered(tags: tuple[str, ...], *, zero: bool = False) -> bool:
                    fact = pick(tags)
                    return fact is not None and fact.accn == accn and (not zero or fact.value == 0)

                combined_zero = any(
                    covered(tags, zero=True)
                    for tags in (TAG_DEBT_COMBINED_LT, TAG_DEBT_LT_CAP_INCL_CURRENT)
                )
                split_zero = (
                    any(
                        covered(tags, zero=True)
                        for tags in (TAG_DEBT_NONCURRENT, TAG_DEBT_LT_AND_CAP_LEASE)
                    )
                    and covered(TAG_DEBT_CURRENT_TOTAL, zero=True)
                )
                fl_combined = covered(TAG_FINANCE_LEASE_COMBINED)
                fl_split = covered(TAG_FINANCE_LEASE_CURRENT) and covered(TAG_FINANCE_LEASE_NONCURRENT)
                finance_covered = (
                    includes_finance_lease
                    or not matched_finance_tags
                    or fl_combined
                    or fl_split
                )
                if not ((combined_zero or split_zero) and finance_covered):
                    ev = EvidencedValue.unresolved(
                        "zero component does not establish current, long-term and finance lease coverage",
                        tags=all_matched,
                        components=components,
                    )
                    return total, tags_used, ev
            if total == 0:
                ev = EvidencedValue.evidenced_zero(
                    tags=all_matched,
                    reason="explicit zero balance-sheet debt amounts",
                    components=components,
                )
            else:
                ev = EvidencedValue.evidenced(
                    total,
                    tags=tuple(tags_used) or all_matched,
                    components=components,
                )
        elif matched_finance_tags or (include_operating_lease and matched_operating_tags):
            ev = EvidencedValue.unresolved(
                "only lease liabilities reported on balance sheet without core debt fact",
                reason_code=FailureReasonCode.UNRESOLVED_SCOPE,
                tags=tuple(matched_finance_tags + matched_operating_tags),
                components=components,
            )
        elif matched_operating_tags and not include_operating_lease:
            ev = EvidencedValue.unresolved(
                "only excluded operating lease liabilities reported on balance sheet without core debt or finance lease fact",
                reason_code=FailureReasonCode.UNRESOLVED_SCOPE,
                tags=tuple(matched_operating_tags),
                components=components,
            )
        else:
            ev = EvidencedValue.missing(
                "no core debt or lease balance-sheet facts reported",
                reason_code=FailureReasonCode.FACT_MISSING,
                components=components,
            )
        if extended_tiers and ev.is_usable and total == 0:
            tags_used = list(dict.fromkeys([*matched_core_tags, *matched_finance_tags, *TAG_DEBT_CURRENT_TOTAL]))
        return total, tags_used, ev

    def find_aggregate_common_shares(
        self,
        gaap: dict[str, Any],
        *,
        accn: str,
        allowed_forms: tuple[str, ...],
    ) -> TypedInstantFact | None:
        """Read us-gaap:CommonStockSharesOutstanding aggregate control figure when reported."""
        entries = [
            e
            for e in gaap.get("CommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
            if e.get("form") in allowed_forms and e.get("accn") == accn and e.get("end") and e.get("val") is not None
        ]
        if not entries:
            return None
        entries.sort(key=lambda e: (e.get("filed", ""), e.get("end", "")), reverse=True)
        e = entries[0]
        return TypedInstantFact(
            tag="CommonStockSharesOutstanding",
            value=_parse_decimal(e["val"], tag="CommonStockSharesOutstanding"),
            accn=str(e.get("accn") or ""),
            period_end=str(e["end"]),
            filed=str(e.get("filed") or ""),
            form=str(e.get("form") or ""),
        )

    def calc_ltm_same_tag(
        self,
        gaap: dict[str, Any],
        tags: tuple[str, ...],
        fy_end: str,
        q_end: str,
        q_fp: str,
        q_accn: str,
    ) -> TypedLtmSeries | None:
        """Compute LTM = FY + Curr_YTD - Prior_YTD enforcing same-tag & YTD duration parity (FIN-05)."""
        fy_end_dt = date.fromisoformat(fy_end)
        q_end_dt = date.fromisoformat(q_end)
        day_lo, day_hi = YTD_DAY_RANGES[q_fp]

        for tag in tags:
            usd_entries = gaap.get(tag, {}).get("units", {}).get("USD", [])
            fy_matches = [
                e for e in usd_entries
                if e.get("form") in ANNUAL_FORMS
                and e.get("fp") == "FY"
                and "start" in e
                and e.get("end") == fy_end
                and e.get("val") is not None
                and 340 <= (_parse_iso_date(e["end"], tag=tag) - _parse_iso_date(e["start"], tag=tag)).days <= 380
            ]
            if not fy_matches:
                continue
            fy_item = max(
                fy_matches,
                key=lambda e: (e.get("filed", ""), str(e.get("form", "")).endswith("/A"), e.get("accn", "")),
            )

            curr_matches = []
            for e in usd_entries:
                if e.get("form") not in QUARTERLY_FORMS or e.get("fp") != q_fp or e.get("end") != q_end or "start" not in e:
                    continue
                if e.get("val") is None:
                    continue
                s_dt = _parse_iso_date(e["start"], tag=tag)
                days = (q_end_dt - s_dt).days
                if not (day_lo <= days <= day_hi):
                    continue
                if -7 <= (s_dt - fy_end_dt).days <= 10:
                    curr_matches.append((days, e))
            if not curr_matches:
                continue
            curr_matches.sort(
                key=lambda pair: (
                    pair[1].get("filed", ""),
                    str(pair[1].get("form", "")).endswith("/A"),
                    bool(pair[1].get("accn") == q_accn),
                    pair[1].get("accn", ""),
                ),
                reverse=True,
            )
            curr_days, curr_item = curr_matches[0]

            prior_matches = []
            for e in usd_entries:
                if e.get("form") not in QUARTERLY_FORMS or e.get("fp") != q_fp or "start" not in e or not e.get("end"):
                    continue
                if e.get("val") is None:
                    continue
                p_end_dt = _parse_iso_date(e["end"], tag=tag)
                gap_days = (q_end_dt - p_end_dt).days
                if not (345 <= gap_days <= 385):
                    continue
                p_days = (p_end_dt - _parse_iso_date(e["start"], tag=tag)).days
                if not (day_lo <= p_days <= day_hi) or abs(p_days - curr_days) > 12:
                    continue
                prior_matches.append(e)
            if not prior_matches:
                continue
            prior_matches.sort(
                key=lambda e: (
                    bool(e.get("accn") == curr_item.get("accn")),
                    e.get("filed", ""),
                    str(e.get("form", "")).endswith("/A"),
                    e.get("accn", ""),
                ),
                reverse=True,
            )
            prior_item = prior_matches[0]

            fy_val = _parse_decimal(fy_item["val"], tag=tag)
            curr_val = _parse_decimal(curr_item["val"], tag=tag)
            prior_val = _parse_decimal(prior_item["val"], tag=tag)
            return TypedLtmSeries(
                tag=tag,
                value=fy_val + curr_val - prior_val,
                fy_val=fy_val,
                curr_val=curr_val,
                prior_val=prior_val,
                prior_end=str(prior_item["end"]),
                fy_accn=str(fy_item.get("accn") or ""),
                accn=str(curr_item.get("accn") or ""),
                prior_accn=str(prior_item.get("accn") or ""),
            )
        return None

    def find_cover_shares(
        self,
        dei: dict[str, Any],
        *,
        allowed_forms: tuple[str, ...],
        accn: str,
    ) -> list[TypedShareFact]:
        """Parse typed EntityCommonStockSharesOutstanding facts for an exact filing accession."""
        raw_entries = (
            dei.get("EntityCommonStockSharesOutstanding", {})
            .get("units", {})
            .get("shares", [])
        )
        out: list[TypedShareFact] = []
        for entry in raw_entries:
            if entry.get("form") not in allowed_forms or entry.get("accn") != accn or not entry.get("end"):
                continue
            if entry.get("val") is None:
                continue
            out.append(
                TypedShareFact(
                    value=_parse_decimal(entry["val"], tag="EntityCommonStockSharesOutstanding"),
                    end_date=_parse_iso_date(entry["end"], tag="EntityCommonStockSharesOutstanding"),
                    accn=str(entry.get("accn") or ""),
                    form=str(entry.get("form") or ""),
                    filed=str(entry.get("filed") or ""),
                    frame=str(entry.get("frame") or ""),
                )
            )
        return out
