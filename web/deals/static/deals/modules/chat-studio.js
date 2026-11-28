/* Deal Chat Studio: Phase Stepper, Execution Trace, EBITDA Bridge, and Interactive HITL Forms. */

import { formatShortUsd, isImeComposing, newClientMessageId } from "./api.js";

const PHASES = [
  { key: "intake", label: "1. Intake" },
  { key: "industry", label: "2. Industry" },
  { key: "peers", label: "3. Peers" },
  { key: "audit", label: "4. Audit" },
  { key: "completed", label: "5. Packet" },
];

export function renderPhaseStepper(container, phase, status) {
  if (!container) return;
  container.replaceChildren();
  const order = PHASES.map((p) => p.key);
  let activeIdx = order.indexOf(phase);
  if (status === "completed") activeIdx = order.length - 1;
  if (activeIdx < 0) activeIdx = 0;

  PHASES.forEach((step, idx) => {
    const pill = document.createElement("div");
    pill.className = "phase-step";
    if (status === "completed" || idx < activeIdx) {
      pill.classList.add("is-done");
    } else if (idx === activeIdx) {
      pill.classList.add("is-active");
      pill.setAttribute("aria-current", "step");
    }
    pill.textContent = step.label;
    container.appendChild(pill);
  });
}

export function renderTraceAccordion(container, traceSteps) {
  if (!container) return;
  container.replaceChildren();
  const steps = Array.isArray(traceSteps) ? traceSteps : [];
  if (steps.length === 0) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  const details = document.createElement("details");
  details.className = "trace-accordion";
  const summary = document.createElement("summary");
  const left = document.createElement("span");
  left.textContent = `${steps.length} workflow trace step(s) executed`;
  const right = document.createElement("span");
  right.className = "mono badge badge-accent";
  right.textContent = "Server Ledger";
  summary.append(left, right);
  details.appendChild(summary);

  const ul = document.createElement("ul");
  ul.className = "trace-list";
  steps.forEach((step) => {
    const li = document.createElement("li");
    const tag = document.createElement("span");
    tag.className = "trace-node-tag";
    tag.textContent = String(step.node || "node");
    li.appendChild(tag);
    li.appendChild(document.createTextNode(" — " + String(step.detail || step.status || "")));
    ul.appendChild(li);
  });
  details.appendChild(ul);
  container.appendChild(details);
}

export function renderTargetSummaryBridge(container, summary) {
  if (!container) return;
  container.replaceChildren();
  if (!summary) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  const items = [
    { lbl: "Reported EBITDA", val: formatShortUsd(summary.reported_ebitda) },
    { lbl: "Net Adj.", val: formatShortUsd(summary.total_adjustments) },
    { lbl: "Normalized", val: formatShortUsd(summary.normalized_ebitda) },
  ];
  items.forEach((item) => {
    const cell = document.createElement("div");
    cell.className = "ebitda-bridge-cell tabular";
    const lbl = document.createElement("div");
    lbl.className = "lbl";
    lbl.textContent = item.lbl;
    const val = document.createElement("div");
    val.className = "val";
    val.textContent = item.val;
    cell.append(lbl, val);
    container.appendChild(cell);
  });
}

export function renderChatStudio({
  data,
  onSubmitAnswer,
  onResumePendingReply,
}) {
  const stepper = document.getElementById("studio-phase-stepper");
  const traceBox = document.getElementById("studio-trace-box");
  const bridgeBox = document.getElementById("studio-ebitda-bridge");
  renderPhaseStepper(stepper, data.phase || "intake", data.status);
  renderTraceAccordion(traceBox, data.trace_steps);
  renderTargetSummaryBridge(bridgeBox, data.target_summary);

  const log = document.getElementById("chat-log");
  log.replaceChildren();
  for (const m of data.messages || []) {
    const div = document.createElement("div");
    div.className = "msg msg-" + m.sender_type;
    div.textContent = m.body;
    log.appendChild(div);
  }
  log.scrollTop = log.scrollHeight;

  const box = document.getElementById("chat-prompt-box");
  box.replaceChildren();
  const p = data.pending_action;
  if (!p) {
    box.style.display = "none";
    return;
  }
  box.style.display = "block";

  const form = document.createElement("form");
  form.className = "hitl-panel";

  const titleRow = document.createElement("div");
  titleRow.className = "card-header";
  titleRow.style.marginBottom = "8px";
  const titleStrong = document.createElement("strong");
  titleStrong.style.fontSize = "13px";
  titleStrong.textContent = "Pending Decision: ";
  const code = document.createElement("code");
  code.className = "mono badge badge-accent";
  code.textContent = p.action;
  titleStrong.appendChild(code);
  titleRow.appendChild(titleStrong);
  form.appendChild(titleRow);

  if (p.pending_reply) {
    const note = document.createElement("p");
    note.className = "muted-note";
    note.textContent = "An interrupted submission is waiting to resume safely using its original idempotent message ID.";
    form.appendChild(note);
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "Resume previously sent reply";
    retry.addEventListener("click", () => onResumePendingReply(retry, p.pending_reply));
    form.appendChild(retry);
    box.appendChild(form);
    return;
  }

  const payload = p.payload || {};
  if (p.action === "confirm_industry") {
    const hint = document.createElement("p");
    hint.className = "muted-note";
    hint.textContent = "Select the target industry taxonomy for public SEC comparable peer screening.";
    form.appendChild(hint);

    const lbl = document.createElement("label");
    lbl.setAttribute("for", "hitl-industry");
    lbl.textContent = "Confirmed Industry Taxonomy";
    form.appendChild(lbl);

    const sel = document.createElement("select");
    sel.id = "hitl-industry";
    sel.name = "confirmed_industry";
    for (const ind of payload.allowed || []) {
      const opt = document.createElement("option");
      opt.value = ind;
      opt.textContent = ind;
      if (ind === payload.proposed) opt.selected = true;
      sel.appendChild(opt);
    }
    form.appendChild(sel);

    const pills = document.createElement("div");
    pills.className = "hitl-pill-grid";
    for (const ind of payload.allowed || []) {
      const pill = document.createElement("button");
      pill.type = "button";
      pill.className = "hitl-pill" + (ind === sel.value ? " is-selected" : "");
      pill.textContent = ind.replace(/_/g, " ");
      pill.addEventListener("click", () => {
        sel.value = ind;
        pills.querySelectorAll(".hitl-pill").forEach((b) => b.classList.remove("is-selected"));
        pill.classList.add("is-selected");
      });
      pills.appendChild(pill);
    }
    sel.addEventListener("change", () => {
      pills.querySelectorAll(".hitl-pill").forEach((b, idx) => {
        const indVal = (payload.allowed || [])[idx];
        b.classList.toggle("is-selected", indVal === sel.value);
      });
    });
    form.appendChild(pills);
  } else if (p.action === "peer_review") {
    const scoresByTicker = new Map(
      (payload.peer_scores || []).map((s) => [String(s.ticker || "").toUpperCase(), s])
    );

    const cardsWrap = document.createElement("div");
    cardsWrap.className = "peer-card-list";

    const lbl = document.createElement("label");
    lbl.setAttribute("for", "hitl-peers");
    lbl.textContent = "Approved peer tickers (comma-separated, min " + (payload.min_required || 3) + ")";
    const inp = document.createElement("input");
    inp.id = "hitl-peers";
    inp.name = "approved_peers";
    inp.className = "mono";
    inp.value = (payload.selected_peers || []).join(", ");

    const syncPeerCards = () => {
      const chosen = new Set(
        inp.value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean)
      );
      cardsWrap.querySelectorAll(".peer-card-btn").forEach((cardBtn) => {
        cardBtn.classList.toggle("is-selected", chosen.has(cardBtn.dataset.ticker));
      });
    };

    const shownTickers = Array.from(
      new Set([
        ...(payload.selected_peers || []).map((t) => String(t).toUpperCase()),
        ...(payload.peer_scores || []).map((s) => String(s.ticker || "").toUpperCase()),
      ])
    ).slice(0, 8);

    shownTickers.forEach((ticker) => {
      const meta = scoresByTicker.get(ticker) || {};
      const cBtn = document.createElement("button");
      cBtn.type = "button";
      cBtn.className = "peer-card-btn";
      cBtn.dataset.ticker = ticker;

      const left = document.createElement("span");
      const tStrong = document.createElement("strong");
      tStrong.className = "mono";
      tStrong.textContent = ticker;
      left.appendChild(tStrong);
      if (meta.ev_tier) {
        left.appendChild(document.createTextNode(" · " + String(meta.ev_tier).replace(/_/g, " ")));
      }

      const right = document.createElement("span");
      right.className = "tabular";
      right.style.fontSize = "11.5px";
      right.style.color = "var(--studio-subtle)";
      const scoreNum = meta.total_score ? Number(meta.total_score).toFixed(2) : "—";
      right.textContent = meta.rank_order ? `Rank #${meta.rank_order} · Score ${scoreNum}` : "Candidate";

      cBtn.append(left, right);
      cBtn.addEventListener("click", () => {
        const list = inp.value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
        const idx = list.indexOf(ticker);
        if (idx >= 0) {
          list.splice(idx, 1);
        } else {
          list.push(ticker);
        }
        inp.value = list.join(", ");
        inp.dispatchEvent(new Event("input", { bubbles: true }));
      });
      cardsWrap.appendChild(cBtn);
    });

    form.appendChild(cardsWrap);
    form.appendChild(lbl);
    form.appendChild(inp);

    const outsideBox = document.createElement("div");
    outsideBox.id = "hitl-outside-reasons";
    form.appendChild(outsideBox);
    const poolSet = new Set((payload.candidate_tickers || []).map((x) => String(x).toUpperCase()));
    const syncOutsideInputs = () => {
      outsideBox.replaceChildren();
      const chosen = inp.value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
      for (const t of chosen) {
        if (!poolSet.has(t)) {
          const rLbl = document.createElement("label");
          rLbl.textContent = "Rationale for out-of-pool peer " + t;
          const rInp = document.createElement("input");
          rInp.dataset.outsideTicker = t;
          rInp.required = true;
          outsideBox.append(rLbl, rInp);
        }
      }
      syncPeerCards();
    };
    inp.addEventListener("input", syncOutsideInputs);
    syncOutsideInputs();
  } else if (p.action === "intake_clarify") {
    const missing = payload.missing_fields || [];
    if (missing.length > 0) {
      for (const field of missing) {
        const lbl = document.createElement("label");
        lbl.textContent = field.replace(/_/g, " ");
        form.appendChild(lbl);
        if (field === "ebitda_period") {
          const sel = document.createElement("select");
          sel.dataset.clarifyField = "ebitda_period";
          sel.required = true;
          const prompt = new Option("Choose a reporting period", "", true, true);
          prompt.disabled = true;
          sel.add(prompt);
          for (const optVal of ["FY", "LTM"]) {
            const opt = document.createElement("option");
            opt.value = optVal;
            opt.textContent = optVal;
            sel.appendChild(opt);
          }
          form.appendChild(sel);
        } else {
          const inp = document.createElement("input");
          inp.dataset.clarifyField = field;
          inp.required = true;
          form.appendChild(inp);
        }
      }
    } else {
      const inp = document.createElement("input");
      inp.id = "hitl-clarify-text";
      inp.required = true;
      inp.placeholder = payload.structured ? 'Correct fields, e.g. {"revenue":"12000000"}' : "Enter clarification";
      inp.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          if (isImeComposing(event)) return;
          form.requestSubmit();
        }
      });
      form.appendChild(inp);
    }
  }

  const btn = document.createElement("button");
  btn.type = "submit";
  btn.textContent = "Send Response";
  btn.style.marginTop = "12px";
  form.appendChild(btn);

  form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    let answer;
    if (p.action === "confirm_industry") {
      answer = document.getElementById("hitl-industry").value;
    } else if (p.action === "peer_review") {
      const tickers = document.getElementById("hitl-peers").value
        .split(",")
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean);
      const ratInputs = form.querySelectorAll("[data-outside-ticker]");
      if (ratInputs.length > 0) {
        const rationale = {};
        ratInputs.forEach((r) => {
          rationale[r.dataset.outsideTicker] = r.value.trim();
        });
        answer = { approved_peers: tickers, rationale };
      } else {
        answer = tickers;
      }
    } else {
      const fields = form.querySelectorAll("[data-clarify-field]");
      if (fields.length > 0) {
        const outObj = {};
        fields.forEach((el) => {
          outObj[el.dataset.clarifyField] = el.value.trim();
        });
        answer = outObj;
      } else {
        answer = document.getElementById("hitl-clarify-text").value.trim();
      }
    }
    await onSubmitAnswer(btn, {
      client_message_id: newClientMessageId(),
      prompt_id: p.prompt_id,
      answer,
    });
  });

  box.appendChild(form);
}
