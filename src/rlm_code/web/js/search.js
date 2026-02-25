/**
 * search.js — Top-bar search with debounced typeahead results.
 *
 * Types in the search box trigger an ILIKE query via /api/search after
 * a 300ms debounce.  Results appear in a dropdown; clicking one
 * navigates to the symbol in the tree + graph + details panels.
 *
 * Public interface:
 *   Search.init(callbacks)
 */

const Search = (() => {

  let _debounceTimer = null;

  /** CSS class for kind-colored badges in the result dropdown. */
  const KIND_BG = {
    class:    "background: #89b4fa; color: #1e1e2e;",
    function: "background: #a6e3a1; color: #1e1e2e;",
    method:   "background: #fab387; color: #1e1e2e;",
  };

  return {
    /**
     * Wire up the search input and results dropdown.
     *
     * @param {Object} callbacks — { onSelect(symbolId) } called when
     *   the user clicks a search result
     */
    init(callbacks) {
      const input   = document.getElementById("search-input");
      const results = document.getElementById("search-results");

      /** Hide the dropdown. */
      function hide() {
        results.classList.add("hidden");
        results.innerHTML = "";
      }

      /** Render a list of search matches into the dropdown. */
      function show(items) {
        results.innerHTML = "";
        if (items.length === 0) {
          results.innerHTML = '<li style="color:var(--text-dim);padding:8px">No results</li>';
          results.classList.remove("hidden");
          return;
        }
        for (const item of items) {
          const li = document.createElement("li");
          const kindStyle = KIND_BG[item.kind] || "background: var(--accent-dim); color: var(--text);";
          li.innerHTML =
            `<span class="sr-kind" style="${kindStyle}">${item.kind}</span>` +
            `<span class="sr-name">${_esc(item.name)}</span>` +
            `<span class="sr-path">${_esc(item.filePath)}</span>`;
          li.addEventListener("click", () => {
            input.value = item.name;
            hide();
            if (callbacks && callbacks.onSelect) callbacks.onSelect(item.id);
          });
          results.appendChild(li);
        }
        results.classList.remove("hidden");
      }

      /* ── Debounced keyup handler ────────────────────────────── */
      input.addEventListener("input", () => {
        clearTimeout(_debounceTimer);
        const q = input.value.trim();
        if (q.length === 0) { hide(); return; }

        _debounceTimer = setTimeout(async () => {
          try {
            const items = await API.search(q);
            show(items);
          } catch (err) {
            console.error("Search error:", err);
            hide();
          }
        }, 300);
      });

      /* Hide results when clicking elsewhere */
      document.addEventListener("click", (e) => {
        if (!e.target.closest("#search-container")) hide();
      });

      /* Keyboard: Escape clears the search */
      input.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { input.value = ""; hide(); }
      });
    },
  };

  /** HTML-escape to prevent XSS in search results. */
  function _esc(s) {
    if (!s) return "";
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
})();
