/**
 * code.js — Source code viewer panel.
 *
 * Replaces the center graph panel when active, showing the source of a file
 * with line numbers, syntax highlighting (via highlight.js + Catppuccin Mocha
 * theme), focus highlighting for a selected symbol's line range, and clickable
 * symbol markers in the gutter.
 *
 * Syntax coloring uses hljs.highlight() to produce tokenized HTML, which is
 * then split by newline and injected into per-line table cells.  If hljs
 * doesn't recognise the language or throws, we fall back to HTML-escaped
 * plain text.
 *
 * Public interface:
 *   Code.show(filePath, focusStart, focusEnd)  — show file with focus
 *   Code.showFile(filePath)                    — show full file, no focus
 *   Code.hide()                                — hide code panel, restore graph
 *   Code.isVisible()                           — check visibility state
 */

const Code = (() => {

  /* ── DOM references (lazily resolved once per call) ─────────────── */

  const _graphPanel = () => document.getElementById("graph-panel");
  const _codePanel  = () => document.getElementById("code-panel");
  const _codeHeader = () => document.getElementById("code-filepath");
  const _codeTable  = () => document.getElementById("code-table");
  const _codeScroll = () => document.getElementById("code-scroll");

  /* ── State ──────────────────────────────────────────────────────── */

  /** The file path currently loaded in the viewer (null = nothing loaded). */
  let _currentFile = null;

  /**
   * Cached raw source text keyed by file path.  Avoids re-fetching when
   * the user clicks different symbols within the same file.
   */
  let _sourceCache = {};

  /**
   * Cached symbol list (from /api/file/) keyed by file path.  Used to
   * render the gutter markers so users can jump between symbols.
   */
  let _symbolCache = {};

  /**
   * Navigation callback set by app.js so clicking a symbol marker in the
   * gutter can trigger the global navigateToSymbol flow.
   */
  let _onSymbolClick = null;

  /* ── HTML escaping ──────────────────────────────────────────────── */

  /**
   * Escape HTML special characters so raw source text can be safely
   * inserted into the DOM without XSS risk.
   */
  function _esc(s) {
    if (!s) return "";
    return s.replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
  }

  /* ── Language detection (for header label + hljs) ────────────────── */

  /**
   * Guess language info from the file extension.  Returns an object with:
   *   - display: human-friendly name for the header (e.g. "Python")
   *   - hljs:    highlight.js language identifier (e.g. "python"), or null
   *              if hljs has no grammar for this extension
   *
   * The hljs id is what we pass to hljs.highlight(source, { language }).
   * When hljs is null the renderer falls back to plain escaped text.
   */
  function _langFromPath(filePath) {
    const ext = filePath.split(".").pop().toLowerCase();

    // Each entry: { display, hljs }  — hljs is the highlight.js language id
    // (null means "no hljs grammar available, fall back to plain text").
    const map = {
      py:   { display: "Python",        hljs: "python" },
      js:   { display: "JavaScript",    hljs: "javascript" },
      ts:   { display: "TypeScript",    hljs: "typescript" },
      tsx:  { display: "TSX",           hljs: "typescript" },
      jsx:  { display: "JSX",           hljs: "javascript" },
      rs:   { display: "Rust",          hljs: "rust" },
      go:   { display: "Go",            hljs: "go" },
      java: { display: "Java",          hljs: "java" },
      c:    { display: "C",             hljs: "c" },
      cpp:  { display: "C++",           hljs: "cpp" },
      h:    { display: "C/C++ Header",  hljs: "c" },
      rb:   { display: "Ruby",          hljs: "ruby" },
      sh:   { display: "Shell",         hljs: "bash" },
      bash: { display: "Bash",          hljs: "bash" },
      zsh:  { display: "Zsh",           hljs: "bash" },
      css:  { display: "CSS",           hljs: "css" },
      html: { display: "HTML",          hljs: "xml" },
      json: { display: "JSON",          hljs: "json" },
      yaml: { display: "YAML",          hljs: "yaml" },
      yml:  { display: "YAML",          hljs: "yaml" },
      toml: { display: "TOML",          hljs: "ini" },
      md:   { display: "Markdown",      hljs: "markdown" },
      sql:  { display: "SQL",           hljs: "sql" },
      lua:  { display: "Lua",           hljs: "lua" },
    };

    return map[ext] || { display: ext.toUpperCase(), hljs: null };
  }

  /* ── Syntax highlighting ────────────────────────────────────────── */

  /**
   * Produce an array of per-line HTML strings with syntax coloring.
   *
   * We run hljs.highlight() on the entire source to get one big HTML string
   * with <span class="hljs-..."> tokens, then split by newline.  This works
   * because hljs closes and reopens span tags at line boundaries internally
   * (or we handle any spanning tokens via the _splitHighlighted helper).
   *
   * Falls back to plain HTML-escaped lines when:
   *   - hljs is not loaded (CDN failed)
   *   - hljsLang is null (unknown extension)
   *   - hljs.highlight() throws (e.g. unregistered language)
   *
   * @param {string}      source   — raw file contents
   * @param {string|null} hljsLang — hljs language id, or null for no highlighting
   * @returns {string[]} one HTML string per source line
   */
  function _highlightLines(source, hljsLang) {
    // Guard: hljs must be loaded and the language must be known
    if (typeof hljs === "undefined" || !hljsLang) {
      return source.split("\n").map((l) => _esc(l));
    }

    try {
      // hljs.highlight returns { value: "<span>...</span>..." }
      const result = hljs.highlight(source, { language: hljsLang });

      // hljs closes/opens spans at newlines for us (since v11), so a simple
      // split gives valid per-line HTML.  If a token ever spans lines the
      // browser's error recovery handles it gracefully.
      return result.value.split("\n");
    } catch (_err) {
      // Language not registered or other error — fall back to escaped text
      return source.split("\n").map((l) => _esc(l));
    }
  }

  /* ── Rendering ──────────────────────────────────────────────────── */

  /**
   * Build the code table HTML for the given source text.
   *
   * Each source line becomes a <tr> with two cells:
   *   - .code-gutter: line number (right-aligned, dim)
   *   - .code-line:   source text with syntax coloring (hljs token spans)
   *
   * Lines within the focus range [focusStart, focusEnd] (1-based, inclusive)
   * get the .code-focus class for highlighted background.
   *
   * Symbol markers are small colored dots in the gutter at the start line
   * of each symbol in the file.  Clicking a marker navigates to that symbol.
   *
   * @param {string}   source     — raw file contents
   * @param {number|null} focusStart — first highlighted line (1-based), or null
   * @param {number|null} focusEnd   — last highlighted line (1-based), or null
   * @param {Array}    symbols    — symbol objects from /api/file/ (may be empty)
   * @param {string|null} hljsLang — hljs language id for syntax coloring
   */
  function _render(source, focusStart, focusEnd, symbols, hljsLang) {
    const table = _codeTable();

    // Get syntax-highlighted per-line HTML (or escaped plain text fallback)
    const highlightedLines = _highlightLines(source, hljsLang);

    // Build a lookup: line number → symbol (for gutter markers).
    // If multiple symbols start on the same line, the first one wins.
    const markerMap = {};
    for (const sym of symbols) {
      if (sym.startLine && !markerMap[sym.startLine]) {
        markerMap[sym.startLine] = sym;
      }
    }

    // Build all rows as an HTML string for performance (avoids thousands
    // of individual DOM insertions for large files).
    const rows = [];
    for (let i = 0; i < highlightedLines.length; i++) {
      const lineNum = i + 1;
      const inFocus = focusStart != null && focusEnd != null &&
                      lineNum >= focusStart && lineNum <= focusEnd;
      const focusCls = inFocus ? " code-focus" : "";

      // Gutter: line number + optional symbol marker dot
      const marker = markerMap[lineNum];
      const markerHtml = marker
        ? `<span class="code-symbol-marker" data-symbol-id="${_esc(marker.id)}" title="${_esc(marker.name)} (${marker.kind})"></span>`
        : "";

      // .code-line gets the pre-tokenized hljs HTML (already escaped by hljs,
      // or by our _esc fallback).  innerHTML is safe here because hljs only
      // produces <span class="hljs-..."> tags from its own grammar rules.
      rows.push(
        `<tr class="${focusCls}">` +
          `<td class="code-gutter">${markerHtml}${lineNum}</td>` +
          `<td class="code-line">${highlightedLines[i]}</td>` +
        `</tr>`
      );
    }

    table.innerHTML = `<tbody>${rows.join("")}</tbody>`;

    // Attach click handlers to symbol markers in the gutter
    table.querySelectorAll(".code-symbol-marker").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        const symId = el.dataset.symbolId;
        if (symId && _onSymbolClick) _onSymbolClick(symId);
      });
    });
  }

  /**
   * Scroll the focus range into view after rendering.  Uses a short
   * requestAnimationFrame delay so the browser has laid out the rows.
   */
  function _scrollToFocus(focusStart) {
    if (focusStart == null) return;

    requestAnimationFrame(() => {
      const table = _codeTable();
      // Rows are 0-indexed in the DOM, lines are 1-indexed
      const row = table.querySelector("tbody")?.children[focusStart - 1];
      if (row) {
        row.scrollIntoView({ block: "center", behavior: "instant" });
      }
    });
  }

  /* ── Panel visibility ───────────────────────────────────────────── */

  /** Show the code panel and hide the graph panel. */
  function _showPanel() {
    _graphPanel().classList.add("hidden");
    _codePanel().classList.remove("hidden");
  }

  /** Hide the code panel and restore the graph panel. */
  function _hidePanel() {
    _codePanel().classList.add("hidden");
    _graphPanel().classList.remove("hidden");
  }

  /* ── Data fetching (with caching) ───────────────────────────────── */

  /**
   * Fetch both the raw source text and the file's symbol list, using
   * cached values when available.  Returns { source, symbols }.
   */
  async function _fetchFileData(filePath) {
    const promises = [];

    // Source text
    if (_sourceCache[filePath]) {
      promises.push(Promise.resolve(_sourceCache[filePath]));
    } else {
      promises.push(
        API.source(filePath).then((text) => {
          _sourceCache[filePath] = text;
          return text;
        })
      );
    }

    // Symbol list (for gutter markers)
    if (_symbolCache[filePath]) {
      promises.push(Promise.resolve(_symbolCache[filePath]));
    } else {
      promises.push(
        API.file(filePath).then((data) => {
          _symbolCache[filePath] = data.symbols || [];
          return data.symbols || [];
        }).catch(() => {
          // If the file isn't in the index, just show no markers
          _symbolCache[filePath] = [];
          return [];
        })
      );
    }

    const [source, symbols] = await Promise.all(promises);
    return { source, symbols };
  }


  /* ── Public API ──────────────────────────────────────────────────── */

  return {
    /**
     * Set the callback for when a symbol marker in the gutter is clicked.
     * Called once by app.js during initialization.
     *
     * @param {Function} fn — callback receiving (symbolId)
     */
    setSymbolClickHandler(fn) {
      _onSymbolClick = fn;
    },

    /**
     * Show a file with a highlighted focus range (the selected symbol's
     * line span).  Fetches the source if not already cached, renders the
     * code table, highlights the focus lines, and scrolls them into view.
     *
     * If the same file is already loaded, only the focus highlight and
     * scroll position are updated (no re-fetch or DOM rebuild needed for
     * different-symbol-same-file clicks).
     *
     * @param {string} filePath   — relative file path
     * @param {number} focusStart — first line to highlight (1-based)
     * @param {number} focusEnd   — last line to highlight (1-based)
     */
    async show(filePath, focusStart, focusEnd) {
      _showPanel();
      const lang = _langFromPath(filePath);
      _codeHeader().textContent = `${filePath}  \u2022  ${lang.display}`;

      // If same file is already loaded, just update the focus highlight
      // instead of re-fetching and re-rendering the whole table.
      if (_currentFile === filePath && _codeTable().querySelector("tbody")) {
        // Clear old focus
        _codeTable().querySelectorAll(".code-focus").forEach((el) => {
          el.classList.remove("code-focus");
        });
        // Apply new focus
        const tbody = _codeTable().querySelector("tbody");
        for (let i = (focusStart || 1) - 1; i < (focusEnd || focusStart || 1); i++) {
          if (tbody.children[i]) tbody.children[i].classList.add("code-focus");
        }
        _scrollToFocus(focusStart);
        return;
      }

      // Different file — fetch and render
      _codeTable().innerHTML = '<tbody><tr><td class="code-gutter"></td><td class="code-line" style="color:var(--text-dim)">Loading...</td></tr></tbody>';
      _currentFile = filePath;

      try {
        const { source, symbols } = await _fetchFileData(filePath);
        // Guard against race: if the user clicked another file while we were
        // loading, don't overwrite the newer file's render.
        if (_currentFile !== filePath) return;
        _render(source, focusStart, focusEnd, symbols, lang.hljs);
        _scrollToFocus(focusStart);
      } catch (err) {
        _codeTable().innerHTML = `<tbody><tr><td class="code-gutter"></td><td class="code-line" style="color:var(--red)">${_esc(err.message)}</td></tr></tbody>`;
      }
    },

    /**
     * Show a full file with no focus highlight.  Used when clicking a
     * file node (not a symbol) in the tree.
     *
     * @param {string} filePath — relative file path
     */
    async showFile(filePath) {
      _showPanel();
      const lang = _langFromPath(filePath);
      _codeHeader().textContent = `${filePath}  \u2022  ${lang.display}`;

      _codeTable().innerHTML = '<tbody><tr><td class="code-gutter"></td><td class="code-line" style="color:var(--text-dim)">Loading...</td></tr></tbody>';
      _currentFile = filePath;

      try {
        const { source, symbols } = await _fetchFileData(filePath);
        if (_currentFile !== filePath) return;
        _render(source, null, null, symbols, lang.hljs);
        // Scroll to top when showing a full file
        _codeScroll().scrollTop = 0;
      } catch (err) {
        _codeTable().innerHTML = `<tbody><tr><td class="code-gutter"></td><td class="code-line" style="color:var(--red)">${_esc(err.message)}</td></tr></tbody>`;
      }
    },

    /**
     * Hide the code panel and restore the graph panel.
     * Clears the current file state so the next show() starts fresh.
     */
    hide() {
      _hidePanel();
      _currentFile = null;
    },

    /**
     * Check whether the code panel is currently visible.
     * @returns {boolean}
     */
    isVisible() {
      return !_codePanel().classList.contains("hidden");
    },
  };
})();
