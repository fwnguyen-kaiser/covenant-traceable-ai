/* CSRF-safe API client, numeric USD formatters, and DOM helpers. */

export function getCookie(name) {
  const parts = (document.cookie || "").split(";");
  for (const part of parts) {
    const trimmed = part.trim();
    if (trimmed.startsWith(name + "=")) {
      return decodeURIComponent(trimmed.slice(name.length + 1));
    }
  }
  return "";
}

export function newClientMessageId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return "msg-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

export async function apiFetch(url, options) {
  const opts = options || {};
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (opts.method && opts.method !== "GET") {
    headers["X-CSRFToken"] = getCookie("csrftoken");
  }
  const resp = await fetch(url, Object.assign({}, opts, { headers, credentials: "same-origin" }));
  const text = await resp.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (_e) {
    payload = { error: text };
  }
  if (!resp.ok) {
    const err = new Error(payload.error || ("HTTP " + resp.status));
    err.status = resp.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

export function showError(id, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  if (!msg) {
    el.style.display = "none";
    el.textContent = "";
    return;
  }
  el.style.display = "block";
  el.textContent = msg;
}

export function numOrNull(val) {
  if (val === null || val === undefined || val === "") return null;
  const n = Number(val);
  return Number.isFinite(n) ? n : null;
}

export function formatShortUsd(val) {
  const n = numOrNull(val);
  if (n === null) return "N/A";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e9) return sign + "$" + (abs / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return sign + "$" + (abs / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return sign + "$" + (abs / 1e3).toFixed(0) + "K";
  return sign + "$" + abs.toFixed(0);
}

export function isImeComposing(event) {
  return Boolean(event && (event.isComposing || event.keyCode === 229));
}
