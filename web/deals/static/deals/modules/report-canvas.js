/* Paper Pitchbook Report Canvas: KPI strip, SVG charts, notices, and safe Markdown sheet. */

import { apiFetch, formatShortUsd, numOrNull } from "./api.js";
import { renderPeerSpreadChart, renderValuationFootballField } from "./charts.js";
import { renderSafeMarkdown } from "./markdown.js";

function makeKpiCard(labelText, valueText, subText) {
  const card = document.createElement("div");
  card.className = "paper-kpi-card tabular";
  const lbl = document.createElement("div");
  lbl.className = "lbl";
  lbl.textContent = labelText;
  const val = document.createElement("div");
  val.className = "val";
  val.textContent = valueText;
  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = subText;
  card.append(lbl, val, sub);
  return card;
}

export async function renderReportCanvas(runId, onReload) {
  const pkt = await apiFetch("/api/runs/" + runId + "/report");
  const card = document.getElementById("report-card");
  const emptyCard = document.getElementById("report-empty-state");
  if (emptyCard) emptyCard.hidden = true;
  card.style.display = "block";

  const meta = document.getElementById("report-meta");
  meta.textContent =
    "Status: " + String(pkt.review_status || "draft").toUpperCase() +
    " | Audit: " + String(pkt.audit_verdict || "").toUpperCase() +
    " | Method: " + (pkt.method_version || "unrecorded") +
    " | SHA-256: " + pkt.digest;

  const kpiGrid = document.getElementById("report-kpi-grid");
  if (kpiGrid) {
    kpiGrid.replaceChildren();
    const v = pkt.valuation || {};
    const medMult = numOrNull(v?.peer_stats?.ev_ebitda?.median);
    const p25 = numOrNull(v?.peer_stats?.ev_ebitda?.p25);
    const p75 = numOrNull(v?.peer_stats?.ev_ebitda?.p75);
    const evArith = numOrNull(v.ev_arithmetic_midpoint);
    const eqMid = v.equity_mid !== null && v.equity_mid !== undefined ? formatShortUsd(v.equity_mid) : "Unavailable";
    const eqArith = numOrNull(v.equity_arithmetic_midpoint);
    const eqSub =
      v.equity_mid !== null && v.equity_mid !== undefined
        ? formatShortUsd(v.equity_low) + " – " + formatShortUsd(v.equity_high) +
          (eqArith !== null ? ` · Arith Mid ${formatShortUsd(eqArith)}` : "")
        : "Missing target debt or cash balance";

    const sensMed = numOrNull(v?.sensitivity?.excluding_statistical_outliers?.ev_ebitda_median);
    const multSub =
      p25 !== null && p75 !== null
        ? `P25 ${p25.toFixed(2)}x · P75 ${p75.toFixed(2)}x` +
          (sensMed !== null ? ` · Excl. Out ${sensMed.toFixed(2)}x` : "")
        : "Comparable cohort";

    kpiGrid.append(
      makeKpiCard(
        "Median-Based EV (EV/EBITDA)",
        formatShortUsd(v.ev_mid),
        formatShortUsd(v.ev_low) + " – " + formatShortUsd(v.ev_high) +
          (evArith !== null ? ` · Arith Mid ${formatShortUsd(evArith)}` : "")
      ),
      makeKpiCard(
        "Median EV / EBITDA",
        medMult !== null ? medMult.toFixed(2) + "x" : "N/A",
        multSub
      ),
      makeKpiCard("Median-Based Equity Value", eqMid, eqSub)
    );
  }

  renderValuationFootballField(document.getElementById("report-chart-football"), pkt.valuation);
  renderPeerSpreadChart(document.getElementById("report-chart-peers"), pkt.valuation);
  renderSafeMarkdown(document.getElementById("report-md"), pkt.explanation || "");

  const notices = document.getElementById("report-notices");
  const items = [...(pkt.review_notices || [])];
  for (const [ticker, failure] of Object.entries(pkt.screening_failures || {})) {
    items.push(ticker + " (screening): " + failure.message);
  }
  for (const [ticker, reason] of Object.entries(pkt.failed_peers || {})) {
    items.push(ticker + " (valuation): " + reason);
  }
  notices.querySelector("ul").replaceChildren(
    ...items.map((message) => {
      const li = document.createElement("li");
      li.textContent = message;
      return li;
    })
  );
  notices.hidden = items.length === 0;

  const exportsEl = document.getElementById("report-exports");
  exportsEl.replaceChildren();
  [
    { href: `/api/runs/${runId}/exports/xlsx`, label: "Download Excel (.xlsx)" },
    { href: `/api/runs/${runId}/exports/md`, label: "Markdown" },
    { href: `/api/runs/${runId}/exports/json`, label: "JSON" },
  ].forEach((linkSpec) => {
    const a = document.createElement("a");
    a.className = "btn btn-sm paper-btn-outline";
    a.href = linkSpec.href;
    a.textContent = linkSpec.label;
    exportsEl.appendChild(a);
  });

  const act = document.getElementById("report-actions");
  act.replaceChildren();
  if (pkt.review_status === "draft" || pkt.review_status === "changes_requested") {
    const subBtn = document.createElement("button");
    subBtn.type = "button";
    subBtn.className = "btn paper-btn-dark";
    subBtn.textContent = "Submit Draft for Review";
    subBtn.addEventListener("click", async () => {
      subBtn.disabled = true;
      try {
        await apiFetch("/api/runs/" + runId + "/submit", { method: "POST", body: "{}" });
        await onReload(runId);
      } finally {
        subBtn.disabled = false;
      }
    });
    act.appendChild(subBtn);
  }
}
