/* Institutional Studio Entrypoint (Vanilla ES Modules, zero bundler required). */

import { apiFetch, showError } from "./modules/api.js";
import { renderChatStudio } from "./modules/chat-studio.js";
import { renderReportCanvas } from "./modules/report-canvas.js";
import { initTheme } from "./modules/theme.js";

function initCaseList() {
  const form = document.getElementById("create-case-form");
  if (!form) return;
  form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    showError("create-case-error", "");
    const submitBtn = form.querySelector("button[type='submit']");
    if (submitBtn) submitBtn.disabled = true;
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
      if (submitBtn) submitBtn.disabled = false;
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
      const submitBtn = profForm.querySelector("button[type='submit']");
      if (submitBtn) submitBtn.disabled = true;
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
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  const btnArch = document.getElementById("btn-archive-case");
  if (btnArch) {
    btnArch.addEventListener("click", async () => {
      try {
        await apiFetch("/api/cases/" + caseId + "/archive", { method: "POST", body: "{}" });
        window.location.reload();
      } catch (err) {
        showError("case-banner", err.message);
      }
    });
  }
  const btnRest = document.getElementById("btn-restore-case");
  if (btnRest) {
    btnRest.addEventListener("click", async () => {
      try {
        await apiFetch("/api/cases/" + caseId + "/restore", { method: "POST", body: "{}" });
        window.location.reload();
      } catch (err) {
        showError("case-banner", err.message);
      }
    });
  }
  const btnDel = document.getElementById("btn-delete-case");
  if (btnDel) {
    btnDel.addEventListener("click", async () => {
      try {
        await apiFetch("/api/cases/" + caseId, { method: "DELETE" });
        window.location.href = "/";
      } catch (err) {
        showError("case-banner", err.message);
      }
    });
  }

  const btnStart = document.getElementById("btn-start-run");
  const runSelector = document.getElementById("run-selector");
  if (btnStart) {
    btnStart.addEventListener("click", async function () {
      showError("chat-error", "");
      btnStart.disabled = true;
      try {
        const data = await apiFetch("/api/cases/" + caseId + "/runs", { method: "POST", body: "{}" });
        activeRunId = data.run_id;
        window.location.href = "/cases/" + caseId + "/?run_id=" + encodeURIComponent(activeRunId);
      } catch (err) {
        showError("chat-error", err.message);
        btnStart.disabled = false;
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

  async function syncView(data) {
    renderChatStudio({
      data,
      onSubmitAnswer: async (btn, body) => {
        showError("chat-error", "");
        btn.disabled = true;
        try {
          const updated = await apiFetch("/api/runs/" + data.run_id + "/messages", {
            method: "POST",
            body: JSON.stringify(body),
          });
          await syncView(updated);
        } catch (err) {
          showError("chat-error", err.message);
          await loadRun(data.run_id);
        } finally {
          btn.disabled = false;
        }
      },
      onResumePendingReply: async (btn, pendingReply) => {
        showError("chat-error", "");
        btn.disabled = true;
        try {
          const updated = await apiFetch("/api/runs/" + data.run_id + "/messages", {
            method: "POST",
            body: JSON.stringify(pendingReply),
          });
          await syncView(updated);
        } catch (err) {
          showError("chat-error", err.message);
          await loadRun(data.run_id);
        } finally {
          btn.disabled = false;
        }
      },
    });
    const emptyCard = document.getElementById("report-empty-state");
    if (data.has_packet) {
      await renderReportCanvas(data.run_id, loadReport);
    } else {
      document.getElementById("report-card").style.display = "none";
      if (emptyCard) emptyCard.hidden = false;
    }
  }

  async function loadReport(runId) {
    await renderReportCanvas(runId, loadReport);
  }

  async function loadRun(runId) {
    if (!runId) return;
    showError("chat-error", "");
    try {
      const data = await apiFetch("/api/runs/" + runId);
      await syncView(data);
    } catch (err) {
      showError("chat-error", err.message);
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

function initReviewQueue() {
  document.querySelectorAll("[data-review-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      reviewAction(
        btn.dataset.runId,
        btn.dataset.digest,
        btn.dataset.reviewAction === "approve"
      );
    });
  });
  document.querySelectorAll("[data-release-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      releaseReport(btn.dataset.runId, btn.dataset.digest, btn.dataset.verdict);
    });
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

function initAdminConsole() {
  const form = document.getElementById("admin-create-user");
  if (form) {
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
  document.querySelectorAll("[data-admin-toggle-active]").forEach((btn) => {
    btn.addEventListener("click", () => {
      toggleUserActive(btn.dataset.userId, btn.dataset.nextActive === "true");
    });
  });
  document.querySelectorAll("[data-admin-toggle-release]").forEach((btn) => {
    btn.addEventListener("click", () => {
      toggleReleasePerm(btn.dataset.userId, btn.dataset.nextRelease === "true");
    });
  });
  document.querySelectorAll("[data-admin-assign-case]").forEach((btn) => {
    btn.addEventListener("click", () => {
      assignMember(btn.dataset.adminAssignCase);
    });
  });
}

window.CovenantApp = {
  initCaseList,
  initCaseDetail,
  reviewAction,
  releaseReport,
  initAdminConsole,
  toggleUserActive,
  toggleReleasePerm,
  assignMember,
};

initTheme();
const page = document.body.dataset.page;
if (page === "case-list") {
  initCaseList();
} else if (page === "case-detail") {
  initCaseDetail(document.body.dataset.caseId || "", document.body.dataset.runId || "");
} else if (page === "review-queue") {
  initReviewQueue();
} else if (page === "admin-console") {
  initAdminConsole();
}
