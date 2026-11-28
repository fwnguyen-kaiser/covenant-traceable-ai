window.CovenantApp = (function () {
  function getCookie(name) {
    const parts = (document.cookie || "").split(";");
    for (const part of parts) {
      const trimmed = part.trim();
      if (trimmed.startsWith(name + "=")) {
        return decodeURIComponent(trimmed.slice(name.length + 1));
      }
    }
    return "";
  }

  function newClientMessageId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "msg-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  async function apiFetch(url, options) {
    const opts = options || {};
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (opts.method && opts.method !== "GET") {
      headers["X-CSRFToken"] = getCookie("csrftoken");
    }
    const resp = await fetch(url, Object.assign({}, opts, { headers, credentials: "same-origin" }));
    const text = await resp.text();
    let payload = {};
    try { payload = text ? JSON.parse(text) : {}; } catch (_e) { payload = { error: text }; }
    if (!resp.ok) {
      const err = new Error(payload.error || ("HTTP " + resp.status));
      err.status = resp.status;
      err.payload = payload;
      throw err;
    }
    return payload;
  }

  function showError(id, msg) {
    const el = document.getElementById(id);
    if (!el) return;
    if (!msg) { el.style.display = "none"; el.textContent = ""; return; }
    el.style.display = "block";
    el.textContent = msg;
  }

  function initCaseList() {
    const form = document.getElementById("create-case-form");
    if (!form) return;
    form.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      showError("create-case-error", "");
      const body = { company_name: document.getElementById("new_company_name").value.trim() };
      const ind = document.getElementById("new_industry").value;
      const eb = document.getElementById("new_ebitda").value.trim();
      if (ind && eb) {
        const answers = {
          company_name: body.company_name,
          industry: ind,
          geography: "US",
          ebitda_period: document.getElementById("new_ebitda_period").value,
          ebitda: eb,
          business_description: document.getElementById("new_desc").value.trim() || body.company_name,
        };
        const rev = document.getElementById("new_revenue").value.trim();
        const debt = document.getElementById("new_debt").value.trim();
        const cash = document.getElementById("new_cash").value.trim();
        if (rev) answers.revenue = rev;
        if (debt !== "") answers.total_debt = debt;
        if (cash !== "") answers.cash = cash;
        body.intake_answers = answers;
      }
      try {
        const created = await apiFetch("/api/cases", { method: "POST", body: JSON.stringify(body) });
        window.location.href = "/cases/" + created.case_id + "/";
      } catch (err) {
        showError("create-case-error", err.message);
      }
    });
  }

  function initCaseDetail(caseId, initialRunId) {
    let activeRunId = initialRunId || "";
    const profForm = document.getElementById("profile-version-form");
    if (profForm) {
      profForm.addEventListener("submit", async function (ev) {
        ev.preventDefault();
        showError("profile-error", "");
        const baseVerRaw = document.getElementById("prof_base_version").value;
        const answers = {
          company_name: document.getElementById("prof_company_name").value.trim(),
          industry: document.getElementById("prof_industry").value,
          geography: document.getElementById("prof_geography").value.trim() || "US",
          ebitda_period: document.getElementById("prof_period").value,
          ebitda: document.getElementById("prof_ebitda").value.trim(),
          business_description: document.getElementById("prof_desc").value.trim(),
          revenue: document.getElementById("prof_revenue").value.trim() || null,
          total_debt: document.getElementById("prof_debt").value.trim() === "" ? null : document.getElementById("prof_debt").value.trim(),
          cash: document.getElementById("prof_cash").value.trim() === "" ? null : document.getElementById("prof_cash").value.trim(),
        };
        const payload = { intake_answers: answers };
        if (baseVerRaw) payload.base_version = parseInt(baseVerRaw, 10);
        try {
          await apiFetch("/api/cases/" + caseId + "/profiles", { method: "POST", body: JSON.stringify(payload) });
          window.location.reload();
        } catch (err) {
          showError("profile-error", err.message);
        }
      });
    }

    const btnArch = document.getElementById("btn-archive-case");
    if (btnArch) btnArch.addEventListener("click", async () => {
      try { await apiFetch("/api/cases/" + caseId + "/archive", { method: "POST", body: "{}" }); window.location.reload(); }
      catch (err) { showError("case-banner", err.message); }
    });
    const btnRest = document.getElementById("btn-restore-case");
    if (btnRest) btnRest.addEventListener("click", async () => {
      try { await apiFetch("/api/cases/" + caseId + "/restore", { method: "POST", body: "{}" }); window.location.reload(); }
      catch (err) { showError("case-banner", err.message); }
    });
    const btnDel = document.getElementById("btn-delete-case");
    if (btnDel) btnDel.addEventListener("click", async () => {
      try { await apiFetch("/api/cases/" + caseId, { method: "DELETE" }); window.location.href = "/"; }
      catch (err) { showError("case-banner", err.message); }
    });

    const btnStart = document.getElementById("btn-start-run");
    const runSelector = document.getElementById("run-selector");
    if (btnStart) {
      btnStart.addEventListener("click", async function () {
        showError("chat-error", "");
        try {
          const data = await apiFetch("/api/cases/" + caseId + "/runs", { method: "POST", body: "{}" });
          activeRunId = data.run_id;
          window.location.href = "/cases/" + caseId + "/?run_id=" + encodeURIComponent(activeRunId);
        } catch (err) {
          showError("chat-error", err.message);
        }
      });
    }
    if (runSelector) {
      runSelector.addEventListener("change", function () {
        if (this.value) {
          activeRunId = this.value;
          loadRun(activeRunId);
        }
      });
    }

    async function loadRun(runId) {
      if (!runId) return;
      showError("chat-error", "");
      try {
        const data = await apiFetch("/api/runs/" + runId);
        renderChat(data);
        if (data.has_packet) {
          await loadReport(runId);
        } else {
          document.getElementById("report-card").style.display = "none";
        }
      } catch (err) {
        showError("chat-error", err.message);
      }
    }

    function renderChat(data) {
      const log = document.getElementById("chat-log");
      log.innerHTML = "";
      for (const m of data.messages || []) {
        const div = document.createElement("div");
        div.className = "msg msg-" + m.sender_type;
        div.textContent = m.body;
        log.appendChild(div);
      }
      const box = document.getElementById("chat-prompt-box");
      box.innerHTML = "";
      const p = data.pending_action;
      if (!p) { box.style.display = "none"; return; }
      box.style.display = "block";
      const form = document.createElement("form");
      const title = document.createElement("div");
      title.innerHTML = "<strong>Pending step:</strong> <code>" + p.action + "</code>";
      form.appendChild(title);

      if (p.pending_reply) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.textContent = "Resume previously sent reply";
        retry.onclick = async () => {
          retry.disabled = true;
          try {
            const updated = await apiFetch("/api/runs/" + data.run_id + "/messages", {
              method: "POST", body: JSON.stringify(p.pending_reply),
            });
            renderChat(updated);
            if (updated.has_packet) await loadReport(data.run_id);
          } catch (err) {
            showError("chat-error", err.message);
            await loadRun(data.run_id);
          } finally {
            retry.disabled = false;
          }
        };
        form.appendChild(retry);
        box.appendChild(form);
        return;
      }

      const payload = p.payload || {};
      if (p.action === "confirm_industry") {
        const sel = document.createElement("select");
        sel.id = "hitl-industry";
        for (const ind of (payload.allowed || [])) {
          const opt = document.createElement("option");
          opt.value = ind;
          opt.textContent = ind;
          if (ind === payload.proposed) opt.selected = true;
          sel.appendChild(opt);
        }
        form.appendChild(sel);
      } else if (p.action === "peer_review") {
        const lbl = document.createElement("label");
        lbl.textContent = "Approved peer tickers (comma-separated, min " + (payload.min_required || 3) + ")";
        form.appendChild(lbl);
        const inp = document.createElement("input");
        inp.id = "hitl-peers";
        inp.value = (payload.selected_peers || []).join(", ");
        form.appendChild(inp);

        const outsideBox = document.createElement("div");
        outsideBox.id = "hitl-outside-reasons";
        form.appendChild(outsideBox);
        const poolSet = new Set((payload.candidate_tickers || []).map(x => String(x).toUpperCase()));
        const syncOutsideInputs = () => {
          outsideBox.innerHTML = "";
          const chosen = inp.value.split(",").map(s => s.trim().toUpperCase()).filter(Boolean);
          for (const t of chosen) {
            if (!poolSet.has(t)) {
              const rLbl = document.createElement("label");
              rLbl.textContent = "Rationale for out-of-pool peer " + t;
              const rInp = document.createElement("input");
              rInp.dataset.outsideTicker = t;
              rInp.required = true;
              outsideBox.appendChild(rLbl);
              outsideBox.appendChild(rInp);
            }
          }
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
          form.appendChild(inp);
        }
      }

      const btn = document.createElement("button");
      btn.type = "submit";
      btn.textContent = "Send Response";
      btn.style.marginTop = "10px";
      form.appendChild(btn);

      form.addEventListener("submit", async function (ev) {
        ev.preventDefault();
        showError("chat-error", "");
        btn.disabled = true;
        let answer;
        if (p.action === "confirm_industry") {
          answer = document.getElementById("hitl-industry").value;
        } else if (p.action === "peer_review") {
          const tickers = document.getElementById("hitl-peers").value.split(",").map(s => s.trim().toUpperCase()).filter(Boolean);
          const ratInputs = form.querySelectorAll("[data-outside-ticker]");
          if (ratInputs.length > 0) {
            const rationale = {};
            ratInputs.forEach(r => { rationale[r.dataset.outsideTicker] = r.value.trim(); });
            answer = { approved_peers: tickers, rationale };
          } else {
            answer = tickers;
          }
        } else {
          const fields = form.querySelectorAll("[data-clarify-field]");
          if (fields.length > 0) {
            const outObj = {};
            fields.forEach(el => { outObj[el.dataset.clarifyField] = el.value.trim(); });
            answer = outObj;
          } else {
            answer = document.getElementById("hitl-clarify-text").value.trim();
          }
        }
        try {
          const updated = await apiFetch("/api/runs/" + data.run_id + "/messages", {
            method: "POST",
            body: JSON.stringify({
              client_message_id: newClientMessageId(),
              prompt_id: p.prompt_id,
              answer,
            }),
          });
          renderChat(updated);
          if (updated.has_packet) await loadReport(data.run_id);
        } catch (err) {
          showError("chat-error", err.message);
          await loadRun(data.run_id);
        } finally {
          btn.disabled = false;
        }
      });
      box.appendChild(form);
    }

    async function loadReport(runId) {
      const pkt = await apiFetch("/api/runs/" + runId + "/report");
      const card = document.getElementById("report-card");
      card.style.display = "block";
      document.getElementById("report-meta").textContent =
        "Status: " + pkt.review_status.toUpperCase() +
        " | Audit: " + String(pkt.audit_verdict || "").toUpperCase() +
        " | Method: " + (pkt.method_version || "unrecorded") +
        " | SHA-256: " + pkt.digest;
      document.getElementById("report-md").textContent = pkt.explanation || "";
      const notices = document.getElementById("report-notices");
      const items = [...(pkt.review_notices || [])];
      for (const [ticker, failure] of Object.entries(pkt.screening_failures || {})) {
        items.push(ticker + " (screening): " + failure.message);
      }
      for (const [ticker, reason] of Object.entries(pkt.failed_peers || {})) {
        items.push(ticker + " (valuation): " + reason);
      }
      notices.querySelector("ul").replaceChildren(...items.map(message => {
        const li = document.createElement("li");
        li.textContent = message;
        return li;
      }));
      notices.hidden = items.length === 0;
      document.getElementById("report-exports").innerHTML =
        '<a class="btn btn-secondary" href="/api/runs/' + runId + '/exports/xlsx">Download Excel (.xlsx)</a> ' +
        '<a class="btn btn-secondary" href="/api/runs/' + runId + '/exports/md">Markdown</a> ' +
        '<a class="btn btn-secondary" href="/api/runs/' + runId + '/exports/json">JSON</a>';
      const act = document.getElementById("report-actions");
      act.innerHTML = "";
      if (pkt.review_status === "draft" || pkt.review_status === "changes_requested") {
        const subBtn = document.createElement("button");
        subBtn.textContent = "Submit Draft for Review";
        subBtn.onclick = async () => {
          await apiFetch("/api/runs/" + runId + "/submit", { method: "POST", body: "{}" });
          await loadReport(runId);
        };
        act.appendChild(subBtn);
      }
    }

    if (activeRunId) loadRun(activeRunId);
  }

  async function reviewAction(runId, digest, approved) {
    showError("review-error", "");
    const comment = (document.getElementById("rev-comment-" + runId) || {}).value || "";
    const endpoint = approved ? "/review" : "/request-changes";
    try {
      await apiFetch("/api/runs/" + runId + endpoint, {
        method: "POST",
        body: JSON.stringify({ digest, comment }),
      });
      window.location.reload();
    } catch (err) {
      showError("review-error", err.message);
    }
  }

  async function releaseReport(runId, digest, verdict) {
    showError("release-error", "");
    const ackEl = document.getElementById("ack-warn-" + runId);
    const ack = verdict === "warn" ? Boolean(ackEl && ackEl.checked) : true;
    try {
      await apiFetch("/api/runs/" + runId + "/release", {
        method: "POST",
        body: JSON.stringify({ digest, acknowledge_warnings: ack }),
      });
      window.location.reload();
    } catch (err) {
      showError("release-error", err.message);
    }
  }

  function initAdminConsole() {
    const form = document.getElementById("admin-create-user");
    if (!form) return;
    form.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      showError("admin-error", "");
      try {
        await apiFetch("/api/admin/users", {
          method: "POST",
          body: JSON.stringify({
            email: document.getElementById("adm_email").value.trim(),
            password: document.getElementById("adm_password").value,
            role: document.getElementById("adm_role").value,
          }),
        });
        window.location.reload();
      } catch (err) {
        showError("admin-error", err.message);
      }
    });
  }

  async function toggleUserActive(userId, nextActive) {
    showError("admin-error", "");
    try {
      await apiFetch("/api/admin/users/" + userId, {
        method: "POST",
        body: JSON.stringify({ is_active: nextActive }),
      });
      window.location.reload();
    } catch (err) {
      showError("admin-error", err.message);
    }
  }

  async function toggleReleasePerm(userId, grant) {
    showError("admin-error", "");
    try {
      await apiFetch("/api/admin/users/" + userId + "/release-permission", {
        method: "POST",
        body: JSON.stringify({ can_release_report: grant }),
      });
      window.location.reload();
    } catch (err) {
      showError("admin-error", err.message);
    }
  }

  async function assignMember(caseId) {
    showError("admin-error", "");
    const userId = document.getElementById("assign-user-" + caseId).value;
    const role = document.getElementById("assign-role-" + caseId).value;
    try {
      await apiFetch("/api/admin/cases/" + caseId + "/members", {
        method: "POST",
        body: JSON.stringify({ user_id: userId, role, set_owner_if_unowned: role === "analyst" }),
      });
      window.location.reload();
    } catch (err) {
      showError("admin-error", err.message);
    }
  }

  return {
    initCaseList,
    initCaseDetail,
    reviewAction,
    releaseReport,
    initAdminConsole,
    toggleUserActive,
    toggleReleasePerm,
    assignMember,
  };
})();
