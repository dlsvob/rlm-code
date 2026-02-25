"""FastAPI visualization server for rlm-code.

Serves a single-page web app that renders the call graph, directory tree,
and symbol details from an indexed DuckDB database.  Launched via the
``rlm-code viz`` CLI command.

Data flow: Browser  ↔  FastAPI endpoints  ↔  CodeStore (DuckDB)
Static assets live in ``src/rlm_code/web/`` next to this file.
"""

import logging
import threading
import webbrowser
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .store import CodeStore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directory where the frontend files live (sibling ``web/`` directory)
# ---------------------------------------------------------------------------
WEB_DIR = Path(__file__).parent / "web"


def create_app(db_path: str) -> FastAPI:
    """Build and return the FastAPI application wired to *db_path*.

    All endpoints close over a single ``CodeStore`` instance that is
    opened at import time and kept alive for the lifetime of the process.
    """

    store = CodeStore(db_path)

    app = FastAPI(title="rlm-code viz", docs_url=None, redoc_url=None)

    # ── API endpoints ─────────────────────────────────────────────────────

    @app.get("/api/overview")
    def api_overview() -> dict:
        """High-level stats for the header bar."""
        stats = store.stats()
        return {
            "files": stats.get("files", 0),
            "symbols": stats.get("symbols", 0),
            "edges": stats.get("edges", 0),
            "by_language": stats.get("by_language", {}),
            "by_kind": stats.get("by_kind", {}),
        }

    @app.get("/api/tree")
    def api_tree() -> dict:
        """Nested directory → file → symbol tree for the left sidebar.

        Builds an in-memory dict tree from every file and symbol in the
        index, then converts it to the recursive JSON the frontend expects.
        """
        files = store.all_files()
        symbols = store.all_symbols()
        summaries = {s.target_id: s.summary_text for s in store.all_summaries()}

        # Group symbols by file path for quick lookup
        sym_by_file: dict[str, list] = defaultdict(list)
        for s in symbols:
            sym_by_file[s.file_path].append(s)

        # Build a nested dict keyed by path parts.
        # Each node: { "_files": {basename: FileRecord}, "_dirs": {name: subtree} }
        root: dict = {"_dirs": {}, "_files": {}}

        for f in files:
            parts = Path(f.path).parts
            node = root
            # Walk/create directory nodes for all but the last part (filename)
            for part in parts[:-1]:
                if part not in node["_dirs"]:
                    node["_dirs"][part] = {"_dirs": {}, "_files": {}}
                node = node["_dirs"][part]
            # Leaf is the file itself
            node["_files"][parts[-1]] = f

        def _build_node(name: str, subtree: dict, path_prefix: str) -> dict:
            """Recursively convert the dict tree into the JSON shape the
            frontend expects:
              { name, type, children, summary?, symbolCount?, kind? }
            """
            children: list[dict] = []
            full_path = f"{path_prefix}/{name}" if path_prefix else name
            symbol_count = 0

            # Sub-directories first (sorted alphabetically)
            for dname in sorted(subtree["_dirs"]):
                child = _build_node(dname, subtree["_dirs"][dname], full_path)
                children.append(child)
                symbol_count += child.get("symbolCount", 0)

            # Then files (sorted alphabetically)
            for fname in sorted(subtree["_files"]):
                frec = subtree["_files"][fname]
                file_path = frec.path
                file_symbols = sym_by_file.get(file_path, [])
                symbol_count += len(file_symbols)

                # Symbol children inside the file
                sym_children = []
                for s in sorted(file_symbols, key=lambda x: x.start_line):
                    sym_children.append({
                        "name": s.name,
                        "type": "symbol",
                        "id": s.id,
                        "kind": s.kind,
                        "line": s.start_line,
                        "summary": summaries.get(s.id),
                    })

                children.append({
                    "name": fname,
                    "type": "file",
                    "path": file_path,
                    "language": frec.language,
                    "lineCount": frec.line_count,
                    "symbolCount": len(file_symbols),
                    "summary": summaries.get(file_path),
                    "children": sym_children,
                })

            return {
                "name": name,
                "type": "dir",
                "path": full_path,
                "symbolCount": symbol_count,
                "summary": summaries.get(full_path),
                "children": children,
            }

        # The root node wraps everything; if there's only one top-level dir
        # we still wrap it so the frontend tree code is consistent.
        top_children: list[dict] = []
        total_sym = 0
        for dname in sorted(root["_dirs"]):
            child = _build_node(dname, root["_dirs"][dname], "")
            top_children.append(child)
            total_sym += child.get("symbolCount", 0)
        for fname in sorted(root["_files"]):
            frec = root["_files"][fname]
            file_syms = sym_by_file.get(frec.path, [])
            total_sym += len(file_syms)
            sym_ch = []
            for s in sorted(file_syms, key=lambda x: x.start_line):
                sym_ch.append({
                    "name": s.name, "type": "symbol", "id": s.id,
                    "kind": s.kind, "line": s.start_line,
                    "summary": summaries.get(s.id),
                })
            top_children.append({
                "name": fname, "type": "file", "path": frec.path,
                "language": frec.language, "lineCount": frec.line_count,
                "symbolCount": len(file_syms),
                "summary": summaries.get(frec.path),
                "children": sym_ch,
            })

        return {
            "name": "(root)",
            "type": "dir",
            "symbolCount": total_sym,
            "children": top_children,
        }

    @app.get("/api/graph")
    def api_graph() -> dict:
        """Full call graph: nodes (symbols + metrics) and resolved edges.

        The frontend sizes nodes by PageRank and colors them by kind.
        Only resolved edges are included — unresolved refs are noise for
        visualization purposes.
        """
        symbols = store.all_symbols()
        metrics_list = store.all_metrics()
        all_edges = store.all_edges()

        # Build a metrics lookup keyed by symbol_id
        metrics_map = {m.symbol_id: m for m in metrics_list}

        nodes = []
        for s in symbols:
            m = metrics_map.get(s.id)
            nodes.append({
                "id": s.id,
                "name": s.name,
                "qualifiedName": s.qualified_name,
                "kind": s.kind,
                "filePath": s.file_path,
                "line": s.start_line,
                "pagerank": m.pagerank if m else 0.0,
                "betweenness": m.betweenness if m else 0.0,
                "inDegree": m.in_degree if m else 0,
                "outDegree": m.out_degree if m else 0,
            })

        # Only keep resolved edges so the graph doesn't include dangling refs
        edges = []
        for e in all_edges:
            if e.resolved:
                edges.append({
                    "source": e.source_id,
                    "target": e.target_id,
                    "kind": e.kind,
                })

        return {"nodes": nodes, "edges": edges}

    @app.get("/api/symbol/{symbol_id:path}")
    def api_symbol(symbol_id: str) -> JSONResponse:
        """Full detail view for a single symbol — summary, metrics,
        callers, and callees with enough info to render clickable links."""
        sym = store.get_symbol(symbol_id)
        if not sym:
            return JSONResponse({"error": "Symbol not found"}, status_code=404)

        metrics = store.get_metrics(sym.id)
        summary = store.get_summary(sym.id)
        callers = store.get_callers(sym.id)
        callees = store.get_callees(sym.id)

        def _sym_ref(sid: str) -> dict:
            """Build a minimal reference dict for a caller/callee."""
            s = store.get_symbol(sid)
            if s:
                return {"id": s.id, "name": s.name, "filePath": s.file_path}
            return {"id": sid, "name": sid.split("::")[-1], "filePath": None}

        return JSONResponse({
            "id": sym.id,
            "name": sym.name,
            "qualifiedName": sym.qualified_name,
            "kind": sym.kind,
            "filePath": sym.file_path,
            "startLine": sym.start_line,
            "endLine": sym.end_line,
            "signature": sym.signature,
            "summary": summary.summary_text if summary and not summary.is_stale else None,
            "pagerank": metrics.pagerank if metrics else 0.0,
            "betweenness": metrics.betweenness if metrics else 0.0,
            "inDegree": metrics.in_degree if metrics else 0,
            "outDegree": metrics.out_degree if metrics else 0,
            "callers": [_sym_ref(c) for c in callers],
            "callees": [_sym_ref(c) for c in callees],
        })

    @app.get("/api/file/{file_path:path}")
    def api_file(file_path: str) -> JSONResponse:
        """Detail view for a file — language, line count, summary, symbol list."""
        files = store.all_files()
        frec = None
        for f in files:
            if f.path == file_path:
                frec = f
                break

        if not frec:
            return JSONResponse({"error": "File not found"}, status_code=404)

        syms = store.symbols_in_file(file_path)
        summary = store.get_summary(file_path)

        return JSONResponse({
            "path": frec.path,
            "language": frec.language,
            "lineCount": frec.line_count,
            "summary": summary.summary_text if summary and not summary.is_stale else None,
            "symbols": [
                {
                    "id": s.id,
                    "name": s.name,
                    "kind": s.kind,
                    "startLine": s.start_line,
                    "endLine": s.end_line,
                    "signature": s.signature,
                }
                for s in sorted(syms, key=lambda x: x.start_line)
            ],
        })

    @app.get("/api/search")
    def api_search(q: str = Query(default="", min_length=1)) -> list[dict]:
        """Typeahead symbol search — returns top 20 ILIKE matches."""
        results = store.search_symbols(q, limit=20)
        return [
            {
                "id": s.id,
                "name": s.name,
                "qualifiedName": s.qualified_name,
                "kind": s.kind,
                "filePath": s.file_path,
            }
            for s in results
        ]

    # ── Source file endpoint ─────────────────────────────────────────────
    # Serves raw source code from the project root.  The project root is
    # derived from db_path's parent — same convention used throughout the
    # CLI (the .rlm-code.duckdb file sits in the project root).

    project_root = Path(db_path).resolve().parent

    @app.get("/api/source/{file_path:path}")
    def api_source(file_path: str) -> PlainTextResponse:
        """Read a source file from the project and return its raw text.

        Guards against path traversal by resolving the requested path and
        verifying it stays within the project root.  Returns 404 for
        missing files or paths that escape the root.
        """
        resolved = (project_root / file_path).resolve()

        # Path traversal guard — the resolved path must be inside the
        # project root.  ``is_relative_to`` raises no exceptions; it
        # simply returns False for paths that escape the root.
        if not resolved.is_relative_to(project_root):
            return PlainTextResponse("Not found", status_code=404)

        if not resolved.is_file():
            return PlainTextResponse("Not found", status_code=404)

        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return PlainTextResponse("Could not read file", status_code=500)

        return PlainTextResponse(text)

    # ── Static file serving ───────────────────────────────────────────────
    # Serve the SPA's index.html at "/" and all other static assets
    # (css, js) from the web/ directory.

    @app.get("/")
    def index_html() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    # Mount static sub-directories so /css/style.css and /js/app.js work
    app.mount("/css", StaticFiles(directory=WEB_DIR / "css"), name="css")
    app.mount("/js", StaticFiles(directory=WEB_DIR / "js"), name="js")

    return app


def start_viz_in_thread(db_path: str, port: int = 8420, open_browser: bool = True) -> bool:
    """Start the viz server in a daemon thread.

    Creates the FastAPI app via ``create_app`` and runs uvicorn inside a
    daemon thread so it dies automatically when the parent process exits.
    This is the shared implementation used by both the CLI entry point
    (``run_viz_server``) and the MCP auto-start path in ``server.py``.

    Args:
        db_path:      Path to the ``.rlm-code.duckdb`` database file.
        port:         TCP port to listen on (default 8420).
        open_browser: If True, opens a browser tab after a 1-second delay.

    Returns:
        True if the server was started successfully, False if the port was
        already in use (``OSError`` / ``EADDRINUSE``).
    """
    import socket
    import uvicorn

    # Pre-check: try to bind the port before creating the app and thread.
    # This avoids a race where uvicorn silently fails inside the daemon thread
    # and the caller never knows the port was taken.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", port))
        probe.close()
    except OSError:
        # Port already in use — another viz instance (or something else)
        return False

    app = create_app(db_path)
    url = f"http://localhost:{port}"

    def _run_uvicorn() -> None:
        """Target for the daemon thread — blocks on uvicorn.run()."""
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

    thread = threading.Thread(target=_run_uvicorn, daemon=True)
    thread.start()

    if open_browser:
        # Open browser after a brief delay so uvicorn has time to start
        threading.Timer(1.0, webbrowser.open, args=[url]).start()

    return True


def run_viz_server(db_path: str, port: int = 8420, open_browser: bool = True) -> None:
    """CLI entry point — start the viz server and block until interrupted.

    Delegates the actual server startup to ``start_viz_in_thread``.  If the
    port is already in use, prints an error and exits.  Otherwise blocks the
    main thread (via ``thread.join()``) so the CLI stays alive.

    Args:
        db_path:      Path to the ``.rlm-code.duckdb`` database file.
        port:         TCP port to listen on (default 8420).
        open_browser: If True, opens a browser tab after a short delay.
    """
    import uvicorn

    app = create_app(db_path)
    url = f"http://localhost:{port}"
    print(f"rlm-code viz → {url}")

    if open_browser:
        # Open browser after a brief delay so uvicorn has time to start
        threading.Timer(1.0, webbrowser.open, args=[url]).start()

    # Block the main thread — this keeps the CLI process alive until Ctrl-C.
    # We use uvicorn.run() directly here (instead of start_viz_in_thread)
    # because the CLI wants blocking behavior.
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
