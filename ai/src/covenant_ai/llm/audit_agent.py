"""Two-tier self-audit gate verifying placeholder integrity, numbers, parity, risks, and semantics."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING

from covenant_ai.domain.models import AuditVerdict
from covenant_ai.llm.client import LLMClient, require_local_client
from covenant_ai.llm.fact_pack import FACT_PATTERN, LockedFactPack
from covenant_ai.llm.prompts import AUDIT_SYSTEM_PROMPT, build_audit_user_prompt
from covenant_ai.llm.schemas import AuditFinding, SemanticAuditResponse

if TYPE_CHECKING:
    from covenant_ai.graph.state import ValuationState

# Domain notations allowed without triggering raw-number alarms.
ALLOWED_TECHNICAL_NUMERIC_TOKENS = re.compile(
    r"\b(?:P25|P75|Tier-[12]|10-K(?:/A)?|10-Q(?:/A)?|8-K|ASC\s*842|Item\s*1|EV/EBITDA|EV/Sales|3PL|B2B|B2C|Q[1-4]|Fortune\s*500)\b",
    re.IGNORECASE,
)
ALLOWED_LIST_INDEX_PREFIX = re.compile(r"^###\s*0\.", re.MULTILINE)
UNCLOSED_BRACE_PATTERN = re.compile(r"\{\{|\}\}")
STRAY_NUMBER_PATTERN = re.compile(
    r"(?:\$\s*[\d,.]+(?:\s*[kKmMbB])?|\b\d+(?:\.\d+)?\s*x\b|\b\d+(?:\.\d+)?\s*%|\b\d[\d,.]*(?:\.\d+)?\b)"
)

FORBIDDEN_CLAIM_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b20%\s*(?:private|liquidity|marketability|size)\s*discount", "Improperly implies an unapplied 20% private-company discount."),
    (r"\b(?:applied|including)\s+a?\s*(?:liquidity|marketability)\s+discount", "Improperly claims a private-company liquidity discount was applied."),
    (r"\b(?:strong\s+buy|buy\s+recommendation|sell\s+recommendation|recommend\s+(?:buying|selling))\b", "Improperly issues a buy/sell investment recommendation."),
)


def _check_stray_numbers(raw_template: str) -> list[AuditFinding]:
    """Ensure the LLM wrote zero raw numbers outside {{fact:...}} placeholders."""
    cleaned = FACT_PATTERN.sub(" ", raw_template)
    if UNCLOSED_BRACE_PATTERN.search(cleaned):
        return [
            AuditFinding(
                rule="malformed_placeholder",
                severity="fail",
                message="Narrative contains unresolved or malformed '{{' or '}}' placeholder syntax.",
            )
        ]
    cleaned = ALLOWED_LIST_INDEX_PREFIX.sub("### ", cleaned)
    cleaned = ALLOWED_TECHNICAL_NUMERIC_TOKENS.sub(" ", cleaned)
    matches = STRAY_NUMBER_PATTERN.findall(cleaned)
    if not matches:
        return []
    sample = ", ".join(dict.fromkeys(m.strip() for m in matches[:6]))
    return [
        AuditFinding(
            rule="stray_raw_number",
            severity="fail",
            message=(
                f"Narrative contains ungrounded raw numeric/currency/multiple token(s) "
                f"outside {{{{fact:...}}}} placeholders: {sample}."
            ),
        )
    ]


def _check_directional_consistency(
    raw_template: str,
    rendered_md: str,
    fact_pack: LockedFactPack,
) -> list[AuditFinding]:
    """Catch obvious directional inversions on profitability vs peer median."""
    findings: list[AuditFinding] = []
    direction = fact_pack.facts.get("margin_spread_direction")
    lower = rendered_md.lower()
    if direction == "below":
        if re.search(
            r"(?:margin|profitability)[^.]{0,60}\b(?:above|exceeds|outperforms|higher than|superior to)\b[^.]{0,40}\b(?:peer|median)",
            lower,
        ):
            findings.append(
                AuditFinding(
                    rule="directional_contradiction",
                    severity="fail",
                    message=(
                        f"Narrative states target EBITDA margin exceeds peer median, contradicting "
                        f"Fact Pack margin_spread_direction='below' ({fact_pack.facts.get('margin_spread_pct')})."
                    ),
                )
            )
    elif direction == "above":
        if re.search(
            r"(?:margin|profitability)[^.]{0,60}\b(?:below|trails|underperforms|lower than|lags)\b[^.]{0,40}\b(?:peer|median)",
            lower,
        ):
            findings.append(
                AuditFinding(
                    rule="directional_contradiction",
                    severity="fail",
                    message=(
                        f"Narrative states target EBITDA margin trails peer median, contradicting "
                        f"Fact Pack margin_spread_direction='above' ({fact_pack.facts.get('margin_spread_pct')})."
                    ),
                )
            )
    return findings


def _check_period_parity(raw_template: str, rendered_md: str, fact_pack: LockedFactPack) -> list[AuditFinding]:
    basis = fact_pack.facts.get("target_period_basis")
    lower = rendered_md.lower()
    findings: list[AuditFinding] = []
    if basis == "LTM":
        if re.search(r"\b(?:latest\s+full\s+fiscal\s+year|fy\s*\d{4})\b", lower) and "ltm" not in lower:
            findings.append(
                AuditFinding(
                    rule="period_parity",
                    severity="fail",
                    message="Target basis is LTM, but narrative cites FY terminology without LTM disclosure.",
                )
            )
    elif basis == "FY":
        if re.search(r"\btrailing\s+twelve\s+months\b|\bltm\b", lower):
            findings.append(
                AuditFinding(
                    rule="period_parity",
                    severity="fail",
                    message="Target basis is FY, but narrative refers to LTM.",
                )
            )
    return findings


def _check_mandatory_disclosures(
    raw_template: str,
    rendered_md: str,
    fact_pack: LockedFactPack,
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    lower = "\n".join(line for line in rendered_md.lower().splitlines() if not line.lstrip().startswith("#"))
    risks = set(fact_pack.required_risk_topics)

    if "customer_concentration" in risks:
        if "concentration" not in lower and "{{fact:target_customer_concentration_pct}}" not in raw_template:
            findings.append(
                AuditFinding(
                    rule="missing_risk_customer_concentration",
                    severity="fail",
                    message="High customer concentration (>= 25%) is present but omitted from Executive Commentary.",
                )
            )

    if "high_leverage_distressed" in risks:
        if not any(k in lower for k in ("leverage", "net debt", "impaired", "distressed", "debt")):
            findings.append(
                AuditFinding(
                    rule="missing_risk_leverage",
                    severity="fail",
                    message="High leverage / equity impairment warning is present but omitted from Executive Commentary.",
                )
            )

    if "lmm_size_spread" in risks:
        if not any(k in lower for k in ("size", "scale", "smaller", "lmm", "liquidity")):
            findings.append(
                AuditFinding(
                    rule="missing_risk_lmm_size",
                    severity="fail",
                    message="LMM public-vs-private scale spread caveat is present but omitted from Executive Commentary.",
                )
            )

    for code, terms in {
        "equity_method_exposure": ("equity method", "equity-method", "affiliate earnings", "affiliate investment"),
        "nci_material": ("nci", "non-controlling", "noncontrolling"),
        "small_peer_count": ("small cohort", "small peer", "limited peer", "few peers", "thin cohort"),
        "excluded_or_failed_peers": ("excluded", "failed", "omitted peer", "disqualified"),
    }.items():
        if code in risks and not any(term in lower for term in terms):
            findings.append(AuditFinding(rule=f"missing_{code}", severity="fail", message=f"Disclose {code} in commentary."))
    if fact_pack.inconsistencies:
        if not any(k in lower for k in ("inconsisten", "unverified", "mismatch", "discrepanc")):
            findings.append(
                AuditFinding(
                    rule="missing_inconsistency_disclosure",
                    severity="fail",
                    message="Process/data inconsistencies exist and must be explicitly acknowledged in the narrative.",
                )
            )
    return findings


def _check_forbidden_claims(rendered_md: str) -> list[AuditFinding]:
    lower = rendered_md.lower()
    findings: list[AuditFinding] = []
    for pat, msg in FORBIDDEN_CLAIM_PATTERNS:
        if re.search(pat, lower):
            findings.append(AuditFinding(rule="forbidden_claim", severity="fail", message=msg))
    return findings


def run_deterministic_audit(
    raw_template: str,
    rendered_md: str,
    fact_pack: LockedFactPack,
    unknown_keys: list[str],
) -> list[AuditFinding]:
    """Run fast deterministic verification gates before optional semantic audit."""
    findings: list[AuditFinding] = []
    if unknown_keys:
        findings.append(
            AuditFinding(
                rule="unknown_fact_placeholder",
                severity="fail",
                message=f"Narrative references unknown fact key(s): {', '.join(unknown_keys)}.",
            )
        )
    findings.extend(_check_stray_numbers(raw_template))
    if "implied_ev_mid" in fact_pack.facts and not re.search(r"\{\{fact:implied_ev_(?:mid|low|high)\}\}", raw_template):
        findings.append(AuditFinding(rule="missing_enterprise_value", severity="fail",
                                     message="Valuation commentary must cite a frozen enterprise value."))
    if re.search(r"\bequity\s+(?:value|midpoint|range|valuation)\b"
                 r"(?:\s+(?:is|of|at|from|equals|midpoint|estimated|between))*\s*[:=]?\s*\{\{fact:implied_ev_",
                 raw_template, re.I):
        findings.append(AuditFinding(rule="equity_ev_mismatch", severity="fail",
                                     message="An enterprise value is presented as an equity value."))
    findings.extend(_check_directional_consistency(raw_template, rendered_md, fact_pack))
    findings.extend(_check_period_parity(raw_template, rendered_md, fact_pack))
    findings.extend(_check_mandatory_disclosures(raw_template, rendered_md, fact_pack))
    findings.extend(_check_forbidden_claims(rendered_md))
    return findings


def audit_narrative(
    client: LLMClient | None,
    *,
    raw_template: str,
    rendered_md: str,
    fact_pack: LockedFactPack,
    unknown_keys: list[str],
    state: ValuationState,
    trace_id: str | None = None,
) -> tuple[AuditVerdict, list[AuditFinding]]:
    """Verify drafted IB commentary using Tier-1 deterministic gates and Tier-2 fast semantic audit."""
    findings = run_deterministic_audit(raw_template, rendered_md, fact_pack, unknown_keys)

    if any(f.severity == "fail" for f in findings):
        return AuditVerdict.FAIL, findings

    if client is not None and rendered_md.strip():
        prompt = build_audit_user_prompt(fact_pack, rendered_md)
        try:
            require_local_client(client)
            sem = client.generate_structured(
                prompt=prompt,
                system_prompt=AUDIT_SYSTEM_PROMPT,
                schema=SemanticAuditResponse,
                task_name="semantic_audit",
                trace_id=trace_id,
            )
        except Exception as exc:
            findings.append(
                AuditFinding(
                    rule="semantic_audit_unavailable",
                    severity="warn",
                    message=(
                        f"Tier-2 semantic audit LLM call failed ({type(exc).__name__}: {exc}); "
                        "report verified via Tier-1 deterministic gates only."
                    ),
                )
            )
            return AuditVerdict.WARN, findings
        if sem.verdict == AuditVerdict.FAIL and not sem.issues:
            findings.append(AuditFinding(rule="semantic_fail", severity="fail",
                                         message="Semantic reviewer rejected the draft without issue details."))
        if sem.verdict == AuditVerdict.WARN and not sem.issues:
            findings.append(AuditFinding(rule="semantic_warn", severity="warn",
                                         message="Semantic reviewer returned WARN without issue details."))
        for issue in sem.issues:
            findings.append(
                AuditFinding(
                    rule=f"semantic:{issue.rule}",
                    severity=issue.severity,
                    message=issue.message,
                )
            )
        if sem.verdict == AuditVerdict.FAIL or any(f.severity == "fail" for f in findings):
            return AuditVerdict.FAIL, findings

    if fact_pack.inconsistencies:
        findings.append(
            AuditFinding(
                rule="recorded_inconsistencies",
                severity="warn",
                message=f"Surfaced {len(fact_pack.inconsistencies)} recorded data/intake inconsistency note(s) in final report.",
            )
        )

    verdict = AuditVerdict.WARN if any(f.severity == "warn" for f in findings) else AuditVerdict.PASS
    return verdict, findings
