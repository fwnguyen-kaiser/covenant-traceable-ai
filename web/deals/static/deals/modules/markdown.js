/* Safe DOM-only Markdown renderer (zero raw innerHTML injection). */

function appendInlineFormatted(parent, text) {
  const tokenRe = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g;
  let lastIdx = 0;
  let match;
  while ((match = tokenRe.exec(text)) !== null) {
    if (match.index > lastIdx) {
      parent.appendChild(document.createTextNode(text.slice(lastIdx, match.index)));
    }
    const tok = match[0];
    if (tok.startsWith("**") && tok.endsWith("**")) {
      const el = document.createElement("strong");
      el.textContent = tok.slice(2, -2);
      parent.appendChild(el);
    } else if (tok.startsWith("*") && tok.endsWith("*")) {
      const el = document.createElement("em");
      el.textContent = tok.slice(1, -1);
      parent.appendChild(el);
    } else if (tok.startsWith("`") && tok.endsWith("`")) {
      const el = document.createElement("code");
      el.className = "mono";
      el.textContent = tok.slice(1, -1);
      parent.appendChild(el);
    }
    lastIdx = match.index + tok.length;
  }
  if (lastIdx < text.length) {
    parent.appendChild(document.createTextNode(text.slice(lastIdx)));
  }
}

function parseTableRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim());
}

function isTableSeparator(line) {
  const cells = parseTableRow(line);
  return cells.length > 0 && cells.every((c) => /^:?-{2,}:?$/.test(c));
}

export function renderSafeMarkdown(container, markdownText) {
  if (!container) return;
  container.replaceChildren();
  const lines = String(markdownText || "").replace(/\r\n/g, "\n").split("\n");
  let listEl = null;
  let i = 0;

  const flushList = () => {
    if (listEl) {
      container.appendChild(listEl);
      listEl = null;
    }
  };

  while (i < lines.length) {
    const raw = lines[i];
    const trimmed = raw.trim();

    if (!trimmed) {
      flushList();
      i += 1;
      continue;
    }

    if (trimmed.startsWith("### ") || trimmed.startsWith("#### ") || trimmed.startsWith("## ")) {
      flushList();
      const level = trimmed.startsWith("#### ") ? "h4" : "h3";
      const h = document.createElement(level);
      appendInlineFormatted(h, trimmed.replace(/^#{2,4}\s+/, ""));
      container.appendChild(h);
      i += 1;
      continue;
    }

    if (trimmed.startsWith("> ")) {
      flushList();
      const bq = document.createElement("blockquote");
      appendInlineFormatted(bq, trimmed.slice(2));
      container.appendChild(bq);
      i += 1;
      continue;
    }

    if (
      trimmed.startsWith("|") &&
      i + 1 < lines.length &&
      (isTableSeparator(lines[i + 1]) ||
        (lines[i + 1].trim().startsWith("|") && parseTableRow(trimmed).length >= 3))
    ) {
      flushList();
      const headers = parseTableRow(trimmed);
      i += isTableSeparator(lines[i + 1]) ? 2 : 1;
      const wrap = document.createElement("div");
      wrap.className = "paper-table-wrap";
      const table = document.createElement("table");
      const thead = document.createElement("thead");
      const htr = document.createElement("tr");
      headers.forEach((cell) => {
        const th = document.createElement("th");
        appendInlineFormatted(th, cell);
        htr.appendChild(th);
      });
      thead.appendChild(htr);
      table.appendChild(thead);

      const tbody = document.createElement("tbody");
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        const rowText = lines[i].trim();
        if (isTableSeparator(rowText)) {
          i += 1;
          continue;
        }
        const rowCells = parseTableRow(rowText);
        const tr = document.createElement("tr");
        rowCells.forEach((cell) => {
          const td = document.createElement("td");
          appendInlineFormatted(td, cell);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
        i += 1;
      }
      table.appendChild(tbody);
      wrap.appendChild(table);
      container.appendChild(wrap);
      continue;
    }

    if (/^(?:-|\+|\*)\s+/.test(trimmed)) {
      if (!listEl) {
        listEl = document.createElement("ul");
      }
      const li = document.createElement("li");
      appendInlineFormatted(li, trimmed.replace(/^(?:-|\+|\*)\s+/, ""));
      listEl.appendChild(li);
      i += 1;
      continue;
    }

    flushList();
    const p = document.createElement("p");
    p.style.margin = "6px 0";
    appendInlineFormatted(p, trimmed);
    container.appendChild(p);
    i += 1;
  }
  flushList();
}
