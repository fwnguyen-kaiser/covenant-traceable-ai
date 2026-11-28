/* Persistent dark/light theme manager synced via <meta name="color-scheme"> and storage events. */

const STORAGE_KEY = "covenant-color-scheme";

function getMetaTag() {
  return document.querySelector('meta[name="color-scheme"]');
}

export function currentTheme() {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch (_e) {
    // Storage unavailable; inspect root attribute cleanly.
  }
  const attr = document.documentElement.getAttribute("data-studio-theme");
  return attr === "light" ? "light" : "dark";
}

export function applyTheme(mode) {
  const resolved = mode === "light" ? "light" : "dark";
  const meta = getMetaTag();
  if (meta) meta.setAttribute("content", resolved);
  document.documentElement.setAttribute("data-studio-theme", resolved);
  const btn = document.getElementById("theme-toggle-btn");
  if (btn) {
    btn.textContent = resolved === "dark" ? "Light Mode" : "Dark Mode";
    btn.setAttribute("aria-pressed", String(resolved === "light"));
    btn.setAttribute(
      "aria-label",
      resolved === "dark" ? "Switch workspace to light mode" : "Switch workspace to dark mode"
    );
  }
}

export function initTheme() {
  applyTheme(currentTheme());
  const btn = document.getElementById("theme-toggle-btn");
  if (btn) {
    btn.addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      try {
        window.localStorage.setItem(STORAGE_KEY, next);
      } catch (_e) {
        // Ignore storage write restriction; apply live in DOM cleanly.
      }
      applyTheme(next);
    });
  }
  window.addEventListener("storage", (ev) => {
    if (ev.key === STORAGE_KEY) {
      applyTheme(ev.newValue === "light" ? "light" : "dark");
    }
  });
}
