/**
 * app.js — Main initialization and cross-panel coordination.
 *
 * Fetches the overview, tree, and graph data in parallel on page load,
 * then wires up the callbacks so clicking in one panel updates the
 * others:
 *   tree click  → graph highlight + details show
 *   graph click → tree expand + details show
 *   search click → tree expand + graph center + details show
 *   details caller/callee click → same as graph click
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
   * a symbol routes through here so all three panels stay in sync.
   *
   * @param {string} symbolId — the symbol's compound ID
   */
  function navigateToSymbol(symbolId) {
    Graph.selectNode(symbolId);
    Tree.expandToNode(symbolId);
    Details.showSymbol(symbolId, { onSymbolClick: navigateToSymbol });
  }

  /* ── Tree (left sidebar) ────────────────────────────────────────── */
  Tree.render(treeData, document.getElementById("tree-container"), {
    /** Symbol clicked in tree → show in graph + details. */
    onSymbol: (symbolId) => navigateToSymbol(symbolId),

    /** File clicked → highlight its symbols in graph + show file details. */
    onFile: (filePath) => {
      Graph.highlightFile(filePath);
      Details.showFile(filePath, { onSymbolClick: navigateToSymbol });
    },

    /** Directory clicked → just show it in tree (no graph/details action). */
    onDir: (_path) => { /* no-op for now */ },
  });

  /* ── Graph (center panel) ───────────────────────────────────────── */
  Graph.init("#graph-svg", graphData, {
    /** Node clicked in graph → show in tree + details. */
    onSelect: (symbolId) => {
      Tree.expandToNode(symbolId);
      Details.showSymbol(symbolId, { onSymbolClick: navigateToSymbol });
    },
  });

  /* ── Reset button (appears after double-click-to-neighborhood) ──── */
  document.getElementById("btn-reset-graph")
    .addEventListener("click", () => Graph.resetView());

  /* ── Close details button ───────────────────────────────────────── */
  document.getElementById("btn-close-details")
    .addEventListener("click", () => Details.hide());

  /* ── Search (top bar) ───────────────────────────────────────────── */
  Search.init({
    /** Search result clicked → navigate to symbol everywhere. */
    onSelect: (symbolId) => navigateToSymbol(symbolId),
  });

})();
