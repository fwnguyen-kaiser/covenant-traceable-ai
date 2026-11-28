/* Pure responsive SVG visualizations for Paper Pitchbook: Football Field + Peer Dispersion. */

import { formatShortUsd, numOrNull } from "./api.js";

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs, text) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    el.setAttribute(k, String(v));
  }
  if (text !== undefined) el.textContent = text;
  return el;
}

export function renderValuationFootballField(container, valuation) {
  if (!container) return;
  container.replaceChildren();
  if (!valuation) {
    container.hidden = true;
    return;
  }

  const rows = [];
  const evLow = numOrNull(valuation.ev_low);
  const evMid = numOrNull(valuation.ev_mid);
  const evHigh = numOrNull(valuation.ev_high);
  if (evLow !== null && evMid !== null && evHigh !== null && evHigh > 0) {
    rows.push({
      label: "EV / EBITDA",
      low: evLow,
      mid: evMid,
      high: evHigh,
      fill: "#1d4ed8",
    });
  }

  const eqLow = numOrNull(valuation.equity_low);
  const eqMid = numOrNull(valuation.equity_mid);
  const eqHigh = numOrNull(valuation.equity_high);
  if (eqLow !== null && eqMid !== null && eqHigh !== null && eqHigh > 0) {
    rows.push({
      label: "Equity Value",
      low: eqLow,
      mid: eqMid,
      high: eqHigh,
      fill: "#059669",
    });
  }

  const sales = valuation.implied_ev_by_sales;
  if (sales) {
    const sLow = numOrNull(sales.ev_low);
    const sMid = numOrNull(sales.ev_mid);
    const sHigh = numOrNull(sales.ev_high);
    if (sLow !== null && sMid !== null && sHigh !== null && sHigh > 0) {
      rows.push({
        label: "EV / Sales",
        low: sLow,
        mid: sMid,
        high: sHigh,
        fill: "#0f766e",
      });
    }
  }

  if (rows.length === 0) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  const maxVal = Math.max(...rows.map((r) => r.high)) * 1.15;
  const xStart = 86;
  const plotWidth = 215;
  const scaleX = (v) => xStart + Math.max(0, Math.min(1, v / maxVal)) * plotWidth;
  const height = 28 + rows.length * 32 + 22;

  const header = document.createElement("div");
  header.className = "card-header";
  header.style.marginBottom = "6px";
  const hTitle = document.createElement("strong");
  hTitle.style.fontSize = "12px";
  hTitle.textContent = "Valuation Football Field (P25–P75 & ◆ Median-Based)";
  const hScale = document.createElement("span");
  hScale.className = "tabular";
  hScale.style.fontSize = "11px";
  hScale.style.color = "var(--paper-muted)";
  hScale.textContent = "Scale: $0 – " + formatShortUsd(maxVal);
  header.append(hTitle, hScale);
  container.appendChild(header);

  const svg = svgEl("svg", {
    viewBox: `0 0 540 ${height}`,
    role: "img",
    "aria-label": "Valuation Football Field horizontal range chart",
    style: "width:100%;height:auto;display:block;",
  });

  [0.25, 0.5, 0.75, 1.0].forEach((frac) => {
    const gx = xStart + frac * plotWidth;
    svg.appendChild(
      svgEl("line", {
        x1: gx,
        y1: 8,
        x2: gx,
        y2: height - 20,
        stroke: "#e2e8f0",
        "stroke-dasharray": "2,2",
      })
    );
    svg.appendChild(
      svgEl(
        "text",
        {
          x: gx,
          y: height - 6,
          "font-size": 9.5,
          "text-anchor": "middle",
          fill: "#64748b",
        },
        formatShortUsd(maxVal * frac)
      )
    );
  });

  rows.forEach((r, idx) => {
    const y = 16 + idx * 32;
    const x1 = scaleX(r.low);
    const x2 = scaleX(r.high);
    const xm = scaleX(r.mid);
    const w = Math.max(6, x2 - x1);

    svg.appendChild(
      svgEl(
        "text",
        { x: 2, y: y + 12, "font-size": 10.5, "font-weight": 600, fill: "#0f172a" },
        r.label
      )
    );
    svg.appendChild(
      svgEl("rect", { x: x1, y, width: w, height: 15, rx: 3, fill: r.fill, opacity: 0.88 })
    );
    svg.appendChild(
      svgEl("polygon", {
        points: `${xm},${y - 2} ${xm + 5},${y + 7.5} ${xm},${y + 17} ${xm - 5},${y + 7.5}`,
        fill: "#0f172a",
      })
    );
    svg.appendChild(
      svgEl(
        "text",
        { x: x1 + w + 8, y: y + 11.5, "font-size": 9.5, fill: "#334155" },
        `${formatShortUsd(r.low)} – ${formatShortUsd(r.high)} (Median ${formatShortUsd(r.mid)})`
      )
    );
  });

  container.appendChild(svg);
}

export function renderPeerSpreadChart(container, valuation) {
  if (!container) return;
  container.replaceChildren();
  const multiples = (valuation && valuation.multiples_used) || {};
  const entries = Object.entries(multiples)
    .map(([ticker, val]) => ({ ticker, val: numOrNull(val) }))
    .filter((e) => e.val !== null && e.val > 0);

  if (entries.length === 0) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  const med = numOrNull(valuation?.peer_stats?.ev_ebitda?.median);
  const statOutliers = (valuation && valuation.statistical_outlier_flags) || null;
  const legacyFlags = (valuation && valuation.outlier_flags) || {};
  const isTukeyOutlier = (ticker) => {
    if (statOutliers !== null) return Boolean(statOutliers[ticker]);
    return String(legacyFlags[ticker] || "").includes("IQR fence");
  };
  const sensMed = numOrNull(valuation?.sensitivity?.excluding_statistical_outliers?.ev_ebitda_median);
  const maxMult = Math.max(...entries.map((e) => e.val), med || 1) * 1.15;
  const xStart = 64;
  const barArea = 310;
  const height = 22 + entries.length * 24 + 16;

  const header = document.createElement("div");
  header.className = "card-header";
  header.style.marginBottom = "6px";
  const title = document.createElement("strong");
  title.style.fontSize = "12px";
  title.textContent = "Comparable Cohort Multiples (EV / EBITDA)";
  const medTxt = document.createElement("span");
  medTxt.className = "tabular";
  medTxt.style.fontSize = "11px";
  medTxt.style.color = "var(--paper-muted)";
  medTxt.textContent =
    (med !== null ? `Median: ${med.toFixed(2)}x` : "") +
    (sensMed !== null ? ` · Excl. Outliers: ${sensMed.toFixed(2)}x` : "");
  header.append(title, medTxt);
  container.appendChild(header);

  const svg = svgEl("svg", {
    viewBox: `0 0 540 ${height}`,
    role: "img",
    "aria-label": "Peer EV/EBITDA multiples bar chart",
    style: "width:100%;height:auto;display:block;",
  });

  if (med !== null) {
    const mx = xStart + (med / maxMult) * barArea;
    svg.appendChild(
      svgEl("line", {
        x1: mx,
        y1: 6,
        x2: mx,
        y2: height - 10,
        stroke: "#0f172a",
        "stroke-dasharray": "3,3",
        "stroke-width": 1.25,
      })
    );
  }

  entries.forEach((e, idx) => {
    const y = 14 + idx * 24;
    const bw = Math.max(4, (e.val / maxMult) * barArea);
    const isOutlier = isTukeyOutlier(e.ticker);
    svg.appendChild(
      svgEl(
        "text",
        { x: 4, y: y + 11, "font-size": 10.5, "font-weight": 700, fill: "#0f172a" },
        e.ticker
      )
    );
    svg.appendChild(
      svgEl("rect", {
        x: xStart,
        y,
        width: bw,
        height: 14,
        rx: 2,
        fill: isOutlier ? "#d97706" : "#2563eb",
        opacity: 0.85,
      })
    );
    svg.appendChild(
      svgEl(
        "text",
        {
          x: xStart + bw + 8,
          y: y + 11,
          "font-size": 10,
          fill: "#334155",
          style: "paint-order:stroke;stroke:#ffffff;stroke-width:3px;stroke-linejoin:round;",
        },
        `${e.val.toFixed(2)}x${isOutlier ? " (Outlier)" : ""}`
      )
    );
  });

  container.appendChild(svg);
}
