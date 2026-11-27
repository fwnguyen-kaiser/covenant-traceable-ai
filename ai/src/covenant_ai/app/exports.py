"""Unified frozen-packet exporters: Markdown, JSON, and XLSX (P6)."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Font


EXCEL_MAX_SIG_DIGITS = 15


def format_packet_banner(packet: dict[str, Any]) -> str:
    """Render the frozen packet release banner including SHA-256 fingerprint."""
    status = str(packet.get("release_status") or "draft").upper()
    method = str(packet.get("method_version") or "cover-gaap-v2")
    digest = str(packet.get("digest") or "")
    return f"**{status}** · {method} · SHA-256 `{digest}`"


def render_markdown_report(packet: dict[str, Any]) -> str:
    """Build exportable Markdown directly from a frozen review packet."""
    banner = format_packet_banner(packet)
    explanation = str(packet.get("explanation") or "")
    return f"{banner}\n\n{explanation}" if explanation else banner


def render_json_report(packet: dict[str, Any]) -> str:
    """Serialize a frozen review packet deterministically to JSON."""
    return json.dumps(packet, ensure_ascii=False, indent=2)


def _assign_safe_text(cell: Any, value: str | None) -> None:
    """Write user or external string as literal text (mitigates Excel formula injection)."""
    if value is None:
        cell.value = None
        return
    text = str(value)
    cell.value = text
    cell.data_type = "s"
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        cell.quotePrefix = True


def _significant_digits_count(raw: str) -> int:
    s = raw.strip().lstrip("+-").replace(".", "").lstrip("0")
    s = s.rstrip("0") if "." in raw else s
    return len(s)


def _assign_decimal_or_missing(cell: Any, value: Any) -> None:
    """Preserve missing != 0 and store exact string when > 15 significant Excel digits."""
    if value is None or (isinstance(value, str) and not value.strip()):
        cell.value = None
        return
    raw = str(value).strip()
    try:
        dec = Decimal(raw)
    except InvalidOperation:
        _assign_safe_text(cell, raw)
        return
    if not dec.is_finite():
        _assign_safe_text(cell, raw)
        return
    if _significant_digits_count(raw) > EXCEL_MAX_SIG_DIGITS:
        cell.value = raw
        cell.data_type = "s"
        return
    cell.value = float(dec) if "." in raw else int(dec)


def render_xlsx_bytes(packet: dict[str, Any]) -> bytes:
    """Export frozen review packet into 3 traceable sheets without calling live providers."""
    wb = openpyxl.Workbook()
    header_font = Font(bold=True)

    # Sheet 1: Results
    ws_res = wb.active
    ws_res.title = "Results"
    ws_res.append(["Field", "Value", "Snapshot / Lineage Reference"])
    for cell in ws_res[1]:
        cell.font = header_font

    val = dict(packet.get("valuation") or {})
    prof = dict(packet.get("company_profile") or {})
    lineage = dict(packet.get("lineage") or {})
    ev_lineage = lineage.get("ev_mid") or {}
    eq_lineage = lineage.get("equity_mid") or {}
    peer_snap_summary = json.dumps(ev_lineage.get("peer_snapshot_ids") or {}, sort_keys=True)

    rows_meta: list[tuple[str, Any, str, bool]] = [
        ("Case ID", packet.get("case_id"), "cases.id", True),
        ("Run ID", packet.get("run_id"), "analysis_runs.id", True),
        ("Thread ID", packet.get("thread_id"), "analysis_runs.thread_id", True),
        ("Method Version", packet.get("method_version"), "frozen_method", True),
        ("Report Digest (SHA-256)", packet.get("digest"), "canonical_review_packet_sha256", True),
        ("Release Status", packet.get("review_status") or packet.get("release_status"), "report_approvals", True),
        ("Audit Verdict", packet.get("audit_verdict"), "self_audit_gate", True),
        ("Company Name", prof.get("company_name"), "company_profiles", True),
        ("Industry", prof.get("industry"), "company_profiles", True),
        ("Period Basis", val.get("ebitda_period") or prof.get("ebitda_period"), "period_parity", True),
        ("Target Normalized EBITDA", prof.get("ebitda"), ",".join(ev_lineage.get("target_snapshot_ids") or []), False),
        ("Target Revenue", prof.get("revenue"), "company_profiles.revenue", False),
        ("Target Total Debt", prof.get("total_debt"), ",".join(eq_lineage.get("inputs") or []), False),
        ("Target Cash", prof.get("cash"), ",".join(eq_lineage.get("inputs") or []), False),
        ("Implied EV Low", val.get("ev_low"), peer_snap_summary, False),
        ("Implied EV Midpoint", val.get("ev_mid"), peer_snap_summary, False),
        ("Implied EV High", val.get("ev_high"), peer_snap_summary, False),
        ("Implied Equity Low", val.get("equity_low"), eq_lineage.get("formula") or "", False),
        ("Implied Equity Midpoint", val.get("equity_mid"), eq_lineage.get("formula") or "", False),
        ("Implied Equity High", val.get("equity_high"), eq_lineage.get("formula") or "", False),
    ]
    for label, raw_val, ref, is_text in rows_meta:
        r_idx = ws_res.max_row + 1
        _assign_safe_text(ws_res.cell(row=r_idx, column=1), label)
        val_cell = ws_res.cell(row=r_idx, column=2)
        if is_text:
            _assign_safe_text(val_cell, str(raw_val) if raw_val is not None else None)
        else:
            _assign_decimal_or_missing(val_cell, raw_val)
        _assign_safe_text(ws_res.cell(row=r_idx, column=3), ref)

    # Sheet 2: Comparable Companies
    ws_comps = wb.create_sheet("Comparable Companies")
    ws_comps.append(["Ticker", "Status", "EV/EBITDA", "EV/Sales", "Snapshot IDs", "Flags / Exclusion Reason"])
    for cell in ws_comps[1]:
        cell.font = header_font

    multiples = dict(val.get("multiples_used") or {})
    sales_multiples = dict(val.get("sales_multiples_used") or {})
    outlier_flags = dict(val.get("outlier_flags") or {})
    peer_flags = dict(val.get("peer_flags") or {})
    failed_peers = dict(packet.get("failed_peers") or {})
    peer_snaps = dict(ev_lineage.get("peer_snapshot_ids") or {})

    for ticker, mult in multiples.items():
        r_idx = ws_comps.max_row + 1
        flags = []
        if outlier_flags.get(ticker):
            flags.append(str(outlier_flags[ticker]))
        for v in (peer_flags.get(ticker) or {}).values():
            flags.append(str(v))
        _assign_safe_text(ws_comps.cell(row=r_idx, column=1), ticker)
        _assign_safe_text(ws_comps.cell(row=r_idx, column=2), "included")
        _assign_decimal_or_missing(ws_comps.cell(row=r_idx, column=3), mult)
        _assign_decimal_or_missing(ws_comps.cell(row=r_idx, column=4), sales_multiples.get(ticker))
        _assign_safe_text(ws_comps.cell(row=r_idx, column=5), ",".join(peer_snaps.get(ticker) or []))
        _assign_safe_text(ws_comps.cell(row=r_idx, column=6), "; ".join(flags) if flags else "")

    for ticker, reason in failed_peers.items():
        r_idx = ws_comps.max_row + 1
        _assign_safe_text(ws_comps.cell(row=r_idx, column=1), ticker)
        _assign_safe_text(ws_comps.cell(row=r_idx, column=2), "excluded")
        ws_comps.cell(row=r_idx, column=3).value = None
        ws_comps.cell(row=r_idx, column=4).value = None
        _assign_safe_text(ws_comps.cell(row=r_idx, column=5), "")
        _assign_safe_text(ws_comps.cell(row=r_idx, column=6), reason)

    # Sheet 3: Sources
    ws_src = wb.create_sheet("Sources")
    ws_src.append([
        "Snapshot ID",
        "Origin ID",
        "Entity / Ticker",
        "Metric",
        "Exact Decimal Value",
        "Currency",
        "Period",
        "Period End",
        "Source Type",
        "Source Ref",
        "Retrieved At",
    ])
    for cell in ws_src[1]:
        cell.font = header_font

    for src in (packet.get("sources") or []):
        r_idx = ws_src.max_row + 1
        _assign_safe_text(ws_src.cell(row=r_idx, column=1), src.get("snapshot_id"))
        _assign_safe_text(ws_src.cell(row=r_idx, column=2), src.get("id"))
        _assign_safe_text(ws_src.cell(row=r_idx, column=3), src.get("peer_ticker") or "TARGET")
        _assign_safe_text(ws_src.cell(row=r_idx, column=4), src.get("metric"))
        # Always preserve exact Decimal strings in Sources sheet as required by P6
        _assign_safe_text(
            ws_src.cell(row=r_idx, column=5),
            str(src["value"]) if src.get("value") is not None else None,
        )
        _assign_safe_text(ws_src.cell(row=r_idx, column=6), src.get("currency"))
        _assign_safe_text(ws_src.cell(row=r_idx, column=7), src.get("period"))
        _assign_safe_text(ws_src.cell(row=r_idx, column=8), src.get("period_end"))
        _assign_safe_text(ws_src.cell(row=r_idx, column=9), src.get("source_type"))
        _assign_safe_text(ws_src.cell(row=r_idx, column=10), src.get("source_ref"))
        _assign_safe_text(ws_src.cell(row=r_idx, column=11), src.get("retrieved_at"))

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_packet_files(
    packet: dict[str, Any],
    *,
    markdown_path: Path | None = None,
    json_path: Path | None = None,
    xlsx_path: Path | None = None,
) -> str:
    """Render Markdown and optionally persist Markdown, JSON, or XLSX snapshot exports."""
    markdown_text = render_markdown_report(packet)
    if markdown_path is not None:
        markdown_path.write_text(markdown_text, encoding="utf-8")
    if json_path is not None:
        json_path.write_text(render_json_report(packet), encoding="utf-8")
    if xlsx_path is not None:
        xlsx_path.write_bytes(render_xlsx_bytes(packet))
    return markdown_text
