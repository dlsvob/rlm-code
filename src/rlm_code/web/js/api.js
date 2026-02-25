/**
 * api.js — Thin async wrapper around the viz server REST endpoints.
 *
 * Every function returns a Promise that resolves to the parsed JSON.
 * All modules import from the global `API` object so there's one place
 * to change if the base URL or error handling ever needs to change.
 */

const API = (() => {
  /**
   * Internal fetch helper.  Throws on non-2xx responses so callers can
   * use try/catch or .catch() uniformly.
   *
   * @param {string} path — URL path (e.g. "/api/overview")
   * @returns {Promise<any>} — parsed JSON body
   */
  async function _get(path) {
    const res = await fetch(path);
    if (!res.ok) {
      const body = await res.text();
      throw new Error(`API ${path} → ${res.status}: ${body}`);
    }
    return res.json();
  }

  return {
    /** High-level project stats (file/symbol/edge counts). */
    overview: () => _get("/api/overview"),

    /** Nested directory → file → symbol tree for the sidebar. */
    tree: () => _get("/api/tree"),

    /** Full graph: { nodes, edges } with metrics on each node. */
    graph: () => _get("/api/graph"),

    /**
     * Full details for a single symbol by its compound ID.
     * @param {string} id — e.g. "src/foo.py::MyClass.method"
     */
    symbol: (id) => _get(`/api/symbol/${encodeURIComponent(id)}`),

    /**
     * Full details for a file.
     * @param {string} path — relative file path
     */
    file: (path) => _get(`/api/file/${encodeURIComponent(path)}`),

    /**
     * Typeahead search — returns top 20 ILIKE matches.
     * @param {string} q — search query
     */
    search: (q) => _get(`/api/search?q=${encodeURIComponent(q)}`),

    /**
     * Fetch raw source text for a file.  Returns a string (not JSON)
     * because the endpoint returns plain text.
     * @param {string} filePath — relative file path within the project
     * @returns {Promise<string>} — the file's raw source text
     */
    source: async (filePath) => {
      const res = await fetch(`/api/source/${encodeURIComponent(filePath)}`);
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`API /api/source/${filePath} → ${res.status}: ${body}`);
      }
      return res.text();
    },
  };
})();
