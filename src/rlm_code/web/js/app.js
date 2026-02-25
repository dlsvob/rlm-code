/**
 * app.js — Main initialization and cross-panel coordination.
 *
 * Fetches the overview, tree, and graph data in parallel on page load,
 * then wires up the callbacks so clicking in one panel updates the
 * others:
 *   tree click  → code viewer + graph sync + details show
 *   graph click → code viewer + tree expand + details show
 *   search click → code viewer + tree expand + graph center + details show
 *   details caller/callee click → same as graph click
 *   back-to-graph button → hide code panel, restore graph
 */

(async function main() {

  /* ── Load data in parallel ──────────────────────────────────────── */
  const [overview, treeData, graphData] = await Promise.all([
    API.overview(),
    API.tree(),
    API.graph(),
  ]);

  /* ── Overview stats (top bar) ───────────────────────────────────── */
  const statsEl = document.getElementById("overview-stats");
  statsEl.textContent =
    `${overview.files} files \u2022 ${overview.symbols} symbols \u2022 ${overview.edges} edges`;

  /* ── Shared callback: navigate to a symbol across all panels ────── */
  /**
   * Central navigation function — every user interaction that selects
   * a symbol routes through here so all panels stay in sync.
   *
   * Fetches the symbol's metadata (to get file path and line range),
   * then opens the code viewer focused on that symbol, updates the
   * tree and details sidebar, and keeps the graph selection in sync
   * (even though the graph is hidden while code is showing).
   *
   * @param {string} symbolId — the symbol's compound ID
   */
  async function navigateToSymbol(symbolId) {
    // Keep graph selection in sync even when hidden, so restoring the
    // graph later still shows the right node highlighted.
    Graph.selectNode(symbolId);
    Tree.expandToNode(symbolId);
    Details.showSymbol(symbolId, {
      onSymbolClick: navigateToSymbol,
      onLocationClick: (filePath, startLine, endLine) => {
        Code.show(filePath, startLine, endLine);
      },
    });

    // Fetch symbol data to get file path and line range for the code
    // viewer.  The details panel fetches this same data, but we need
    // the file/line info here to drive the code viewer.
    try {
      const sym = await API.symbol(symbolId);
      if (sym.filePath) {
        Code.show(sym.filePath, sym.startLine, sym.endLine);
      }
    } catch (_err) {
      // If the symbol fetch fails, the details panel will show an
      // error — no need to duplicate the error handling here.
    }
  }

  /* ── Tell the Code module how to handle gutter marker clicks ────── */
  Code.setSymbolClickHandler(navigateToSymbol);

  /* ── Tree (left sidebar) ────────────────────────────────────────── */
  Tree.render(treeData, document.getElementById("tree-container"), {
    /** Symbol clicked in tree → open code viewer focused on that symbol. */
    onSymbol: (symbolId) => navigateToSymbol(symbolId),

    /** File clicked → show full file in code viewer + file details. */
    onFile: (filePath) => {
      Graph.highlightFile(filePath);
      Code.showFile(filePath);
      Details.showFile(filePath, { onSymbolClick: navigateToSymbol });
    },

    /** Directory clicked → just show it in tree (no graph/details action). */
    onDir: (_path) => { /* no-op for now */ },
  });

  /* ── Graph (center panel) ───────────────────────────────────────── */
  Graph.init("#graph-svg", graphData, {
    /** Node clicked in graph → show in code viewer + tree + details. */
    onSelect: (symbolId) => {
      navigateToSymbol(symbolId);
    },
  });

  /* ── Reset button (appears after double-click-to-neighborhood) ──── */
  document.getElementById("btn-reset-graph")
    .addEventListener("click", () => Graph.resetView());

  /* ── Back to graph button (in the code panel header) ─────────────── */
  document.getElementById("btn-back-to-graph")
    .addEventListener("click", () => Code.hide());

  /* ── Close details button ───────────────────────────────────────── */
  document.getElementById("btn-close-details")
    .addEventListener("click", () => Details.hide());

  /* ── Search (top bar) ───────────────────────────────────────────── */
  Search.init({
    /** Search result clicked → navigate to symbol everywhere. */
    onSelect: (symbolId) => navigateToSymbol(symbolId),
  });

})();
