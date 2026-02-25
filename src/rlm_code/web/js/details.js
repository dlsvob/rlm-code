/**
 * details.js — Right sidebar showing details for a selected symbol,
 * file, or directory.
 *
 * Fetches full details from the API when a selection is made, then
 * renders the appropriate view (symbol / file) into #details-content.
 *
 * Public interface:
 *   Details.showSymbol(symbolId, callbacks)
 *   Details.showFile(filePath, callbacks)
 *   Details.hide()
 */

const Details = (() => {

  const _panel   = () => document.getElementById("sidebar-right");
  const _content = () => document.getElementById("details-content");

  /**
   * Render the symbol detail view: name, kind badge, signature,
   * summary, metrics grid, caller list, callee list.
   *
   * @param {Object} data — response from /api/symbol/:id
   * @param {Object} cb   — { onSymbolClick(id) } for callers/callees
   */
  function _renderSymbol(data, cb) {
    const c = _content();
    c.innerHTML = "";

    /* ── Name + kind badge ────────────────────────────────────── */
    const h2 = document.createElement("h2");
    h2.textContent = data.qualifiedName;
    c.appendChild(h2);

    const kind = document.createElement("span");
    kind.className = `detail-kind ${data.kind}`;
    kind.textContent = data.kind;
    c.appendChild(kind);

    /* ── File location ────────────────────────────────────────── */
    const loc = document.createElement("div");
    loc.className = "detail-section";
    loc.innerHTML = `<h3>Location</h3><code>${data.filePath}:${data.startLine}-${data.endLine}</code>`;
    c.appendChild(loc);

    /* ── Signature ────────────────────────────────────────────── */
    if (data.signature) {
      const sec = document.createElement("div");
      sec.className = "detail-section";
      sec.innerHTML = `<h3>Signature</h3><div class="detail-signature">${_esc(data.signature)}</div>`;
      c.appendChild(sec);
    }

    /* ── Summary ──────────────────────────────────────────────── */
    if (data.summary) {
      const sec = document.createElement("div");
      sec.className = "detail-section";
      sec.innerHTML = `<h3>Summary</h3><div class="detail-summary">${_esc(data.summary)}</div>`;
      c.appendChild(sec);
    }

    /* ── Metrics ──────────────────────────────────────────────── */
    const metSec = document.createElement("div");
    metSec.className = "detail-section";
    metSec.innerHTML = `<h3>Metrics</h3>
      <div class="detail-metrics">
        ${_metric("PageRank", data.pagerank.toFixed(4))}
        ${_metric("Betweenness", data.betweenness.toFixed(4))}
        ${_metric("In-degree", data.inDegree)}
        ${_metric("Out-degree", data.outDegree)}
      </div>`;
    c.appendChild(metSec);

    /* ── Callers ──────────────────────────────────────────────── */
    if (data.callers && data.callers.length > 0) {
      const sec = document.createElement("div");
      sec.className = "detail-section";
      sec.innerHTML = `<h3>Callers (${data.callers.length})</h3>`;
      sec.appendChild(_refList(data.callers, cb));
      c.appendChild(sec);
    }

    /* ── Callees ──────────────────────────────────────────────── */
    if (data.callees && data.callees.length > 0) {
      const sec = document.createElement("div");
      sec.className = "detail-section";
      sec.innerHTML = `<h3>Callees (${data.callees.length})</h3>`;
      sec.appendChild(_refList(data.callees, cb));
      c.appendChild(sec);
    }
  }

  /**
   * Render file detail view: path, language, line count, summary,
   * and a table of symbols defined in the file.
   *
   * @param {Object} data — response from /api/file/:path
   * @param {Object} cb   — { onSymbolClick(id) }
   */
  function _renderFile(data, cb) {
    const c = _content();
    c.innerHTML = "";

    const h2 = document.createElement("h2");
    h2.textContent = data.path;
    c.appendChild(h2);

    const meta = document.createElement("div");
    meta.style.cssText = "color: var(--text-dim); margin-bottom: 8px;";
    meta.textContent = `${data.language}  \u2022  ${data.lineCount} lines`;
    c.appendChild(meta);

    if (data.summary) {
      const sec = document.createElement("div");
      sec.className = "detail-section";
      sec.innerHTML = `<h3>Summary</h3><div class="detail-summary">${_esc(data.summary)}</div>`;
      c.appendChild(sec);
    }

    if (data.symbols && data.symbols.length > 0) {
      const sec = document.createElement("div");
      sec.className = "detail-section";
      sec.innerHTML = `<h3>Symbols (${data.symbols.length})</h3>`;

      const table = document.createElement("table");
      table.className = "detail-symbols-table";
      table.innerHTML = `<thead><tr><th>Name</th><th>Kind</th><th>Line</th></tr></thead>`;
      const tbody = document.createElement("tbody");

      for (const sym of data.symbols) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${_esc(sym.name)}</td><td>${sym.kind}</td><td>${sym.startLine}</td>`;
        tr.addEventListener("click", () => {
          if (cb && cb.onSymbolClick) cb.onSymbolClick(sym.id);
        });
        tbody.appendChild(tr);
      }

      table.appendChild(tbody);
      sec.appendChild(table);
      c.appendChild(sec);
    }
  }

  /* ── Helpers ─────────────────────────────────────────────────────── */

  /** Build one metric tile's HTML. */
  function _metric(label, value) {
    return `<div class="detail-metric">
      <div class="label">${label}</div>
      <div class="value">${value}</div>
    </div>`;
  }

  /** Build a clickable caller/callee list. */
  function _refList(refs, cb) {
    const ul = document.createElement("ul");
    ul.className = "detail-list";
    for (const ref of refs) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="ref-name">${_esc(ref.name)}</span>` +
        (ref.filePath ? `<span class="ref-file">${ref.filePath}</span>` : "");
      li.addEventListener("click", () => {
        if (cb && cb.onSymbolClick) cb.onSymbolClick(ref.id);
      });
      ul.appendChild(li);
    }
    return ul;
  }

  /** HTML-escape a string to prevent XSS. */
  function _esc(s) {
    if (!s) return "";
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
  }


  /* ── Public API ─────────────────────────────────────────────────── */

  return {
    /**
     * Fetch and display full details for a symbol.
     * @param {string} symbolId — compound symbol ID
     * @param {Object} callbacks — { onSymbolClick(id) }
     */
    async showSymbol(symbolId, callbacks) {
      _panel().classList.remove("hidden");
      _content().innerHTML = "<em>Loading...</em>";

      try {
        const data = await API.symbol(symbolId);
        _renderSymbol(data, callbacks || {});
      } catch (err) {
        _content().innerHTML = `<p style="color:var(--red)">Error: ${_esc(err.message)}</p>`;
      }
    },

    /**
     * Fetch and display full details for a file.
     * @param {string} filePath — relative file path
     * @param {Object} callbacks — { onSymbolClick(id) }
     */
    async showFile(filePath, callbacks) {
      _panel().classList.remove("hidden");
      _content().innerHTML = "<em>Loading...</em>";

      try {
        const data = await API.file(filePath);
        _renderFile(data, callbacks || {});
      } catch (err) {
        _content().innerHTML = `<p style="color:var(--red)">Error: ${_esc(err.message)}</p>`;
      }
    },

    /** Hide the details panel. */
    hide() {
      _panel().classList.add("hidden");
    },
  };
})();
