"""CLI entry point for rlm-code."""

import argparse
import json
import logging
import sys
from pathlib import Path

from .graph import build_graph, detect_patterns, shortest_paths
from .indexer import run_index
from .models import IndexConfig
from .store import CodeStore
from .summarize import run_summarize

log = logging.getLogger(__name__)


def _default_db(project_root: str) -> str:
    return str(Path(project_root).resolve() / ".rlm-code.duckdb")


def _get_store(project_root: str, db_path: str | None = None) -> CodeStore:
    root = str(Path(project_root).resolve())
    path = db_path or _default_db(root)
    return CodeStore(path)


def cmd_index(args: argparse.Namespace) -> int:
    root = str(Path(args.path).resolve())
    db_path = args.db or _default_db(root)

    config = IndexConfig(
        project_root=root,
        db_path=db_path,
    )

    print(f"Indexing {root} → {db_path}", file=sys.stderr)
    stats = run_index(config, force=args.force)

    print(f"  files:   {stats.get('files', 0)}")
    print(f"  symbols: {stats.get('symbols', 0)}")
    print(f"  edges:   {stats.get('edges', 0)}")
    if "by_language" in stats:
        for lang, count in stats["by_language"].items():
            print(f"    {lang}: {count} files")
    if "by_kind" in stats:
        for kind, count in stats["by_kind"].items():
            print(f"    {kind}: {count}")
    if stats.get("errors"):
        print(f"  errors:  {stats['errors']}", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = str(Path(args.path).resolve())
    store = _get_store(root, args.db)
    try:
        stats = store.stats()
        last_commit = store.get_meta("last_indexed_commit")
        print(f"Project:  {root}")
        print(f"Database: {args.db or _default_db(root)}")
        print(f"Commit:   {last_commit or '(not indexed yet)'}")
        print(f"Files:    {stats.get('files', 0)}")
        print(f"Symbols:  {stats.get('symbols', 0)}")
        print(f"Edges:    {stats.get('edges', 0)}")
        if "by_language" in stats:
            for lang, count in stats["by_language"].items():
                print(f"  {lang}: {count} files")
    finally:
        store.close()
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    root = str(Path(args.path).resolve())
    store = _get_store(root, args.db)
    try:
        symbols = store.find_symbols_by_name(args.symbol)
        if not symbols:
            print(f"Symbol not found: {args.symbol}")
            return 1

        for sym in symbols:
            metrics = store.get_metrics(sym.id)
            callers = store.get_callers(sym.id)
            callees = store.get_callees(sym.id)
            print(f"\n{sym.kind.upper()}  {sym.qualified_name}")
            print(f"  file:      {sym.file_path}:{sym.start_line}–{sym.end_line}")
            print(f"  signature: {sym.signature}")
            summary = store.get_summary(sym.id)
            if summary and summary.summary_text and not summary.is_stale:
                print(f"  summary:   {summary.summary_text}")
            if metrics:
                print(f"  pagerank:  {metrics.pagerank:.4f}  betweenness: {metrics.betweenness:.4f}")
                print(f"  callers:   {metrics.in_degree}  callees: {metrics.out_degree}")
            if callers:
                print(f"  called by: {', '.join(c.split('::')[-1] for c in callers[:8])}")
            if callees:
                print(f"  calls:     {', '.join(c.split('::')[-1] for c in callees[:8])}")
    finally:
        store.close()
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    root = str(Path(args.path).resolve())
    store = _get_store(root, args.db)
    try:
        all_symbols = []
        for fp in store.all_file_paths():
            all_symbols.extend(store.symbols_in_file(fp))
        all_edges = store.all_edges()
        g = build_graph(all_symbols, all_edges)

        # Resolve names to IDs
        from_syms = store.find_symbols_by_name(args.from_symbol)
        to_syms = store.find_symbols_by_name(args.to_symbol)

        if not from_syms:
            print(f"Symbol not found: {args.from_symbol}")
            return 1
        if not to_syms:
            print(f"Symbol not found: {args.to_symbol}")
            return 1

        found = False
        for fsym in from_syms:
            for tsym in to_syms:
                paths = shortest_paths(g, fsym.id, tsym.id)
                if paths:
                    found = True
                    print(f"\n{fsym.qualified_name} → {tsym.qualified_name}")
                    for i, path in enumerate(paths, 1):
                        names = [p.split("::")[-1] for p in path]
                        print(f"  Path {i}: {' → '.join(names)}")

        if not found:
            print(f"No path found from {args.from_symbol} to {args.to_symbol}")
            return 1
    finally:
        store.close()
    return 0


def cmd_related(args: argparse.Namespace) -> int:
    root = str(Path(args.path).resolve())
    store = _get_store(root, args.db)
    try:
        symbols = store.find_symbols_by_name(args.symbol)
        if not symbols:
            print(f"Symbol not found: {args.symbol}")
            return 1

        for sym in symbols[:1]:  # first match
            callers = store.get_callers(sym.id)
            callees = store.get_callees(sym.id)
            print(f"\nRelated to: {sym.qualified_name}  ({sym.file_path})")
            if callers:
                print(f"\nCallers ({len(callers)}):")
                for c in callers[:15]:
                    csym = store.get_symbol(c)
                    label = csym.qualified_name if csym else c.split("::")[-1]
                    print(f"  ← {label}")
            if callees:
                print(f"\nCallees ({len(callees)}):")
                for c in callees[:15]:
                    csym = store.get_symbol(c)
                    label = csym.qualified_name if csym else c.split("::")[-1]
                    print(f"  → {label}")
    finally:
        store.close()
    return 0


def cmd_hotspots(args: argparse.Namespace) -> int:
    root = str(Path(args.path).resolve())
    store = _get_store(root, args.db)
    try:
        top = store.top_by_pagerank(n=args.n)
        print(f"Top {len(top)} symbols by PageRank:\n")
        for rank, (sym_id, pr) in enumerate(top, 1):
            sym = store.get_symbol(sym_id)
            if sym:
                m = store.get_metrics(sym_id)
                in_d = m.in_degree if m else 0
                out_d = m.out_degree if m else 0
                print(f"  {rank:2}. {sym.qualified_name:<40} "
                      f"pr={pr:.4f}  in={in_d}  out={out_d}  ({sym.file_path})")
    finally:
        store.close()
    return 0


def cmd_patterns(args: argparse.Namespace) -> int:
    root = str(Path(args.path).resolve())
    store = _get_store(root, args.db)
    try:
        all_symbols = []
        for fp in store.all_file_paths():
            all_symbols.extend(store.symbols_in_file(fp))
        all_edges = store.all_edges()
        g = build_graph(all_symbols, all_edges)
        report = detect_patterns(g, all_symbols)

        print("=== Architectural Patterns ===\n")

        print(f"God Objects ({len(report.god_objects)}):")
        for sid in report.god_objects:
            sym = store.get_symbol(sid)
            m = store.get_metrics(sid)
            if sym and m:
                print(f"  {sym.qualified_name}  (in={m.in_degree} out={m.out_degree})  {sym.file_path}")

        print(f"\nOrphans — zero connections ({len(report.orphans)}):")
        for sid in report.orphans[:10]:
            sym = store.get_symbol(sid)
            if sym:
                print(f"  {sym.qualified_name}  {sym.file_path}:{sym.start_line}")

        print(f"\nCircular Dependency Clusters ({len(report.cycles)}):")
        for cycle in report.cycles[:5]:
            names = [c.split("::")[-1] for c in cycle]
            print(f"  {' ↔ '.join(names)}")

        print(f"\nHub Files (most edges):")
        for fp in report.hub_files:
            print(f"  {fp}")
    finally:
        store.close()
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    root = str(Path(args.path).resolve())
    db_path = args.db or _default_db(root)

    model = args.model or "haiku"
    force = args.force

    print(f"Summarizing {root} (model={model}, force={force})", file=sys.stderr)
    stats = run_summarize(
        project_root=root,
        db_path=db_path,
        model=model,
        skip_fresh=not force,
    )

    print(f"  symbols:     {stats.get('symbols', 0)}")
    print(f"  files:       {stats.get('files', 0)}")
    print(f"  directories: {stats.get('directories', 0)}")
    print(f"  skipped:     {stats.get('skipped', 0)}")
    print(f"  errors:      {stats.get('errors', 0)}")
    print(f"  LLM calls:   {stats.get('llm_calls', 0)}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import run_server
    run_server(http=args.http, port=args.port)
    return 0


def cmd_viz(args: argparse.Namespace) -> int:
    """Launch the interactive visualization web app for an indexed project."""
    root = str(Path(args.path).resolve())
    db_path = args.db or _default_db(root)

    # Verify the database exists before trying to serve
    if not Path(db_path).exists():
        print(f"No index found at {db_path}", file=sys.stderr)
        print("Run 'rlm-code index' first to index the project.", file=sys.stderr)
        return 1

    from .viz_server import run_viz_server
    run_viz_server(db_path=db_path, port=args.port, open_browser=not args.no_open)
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="rlm-code",
        description="Graph-aware code indexer for Claude",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    sub = parser.add_subparsers(dest="command", required=True)

    # index
    p = sub.add_parser("index", help="Index a project directory")
    p.add_argument("path", nargs="?", default=".", help="Project root (default: .)")
    p.add_argument("--db", help="Database path (default: <project>/.rlm-code.duckdb)")
    p.add_argument("--force", action="store_true", help="Force full reindex")

    # status
    p = sub.add_parser("status", help="Show index status")
    p.add_argument("path", nargs="?", default=".", help="Project root")
    p.add_argument("--db", help="Database path")

    # query
    p = sub.add_parser("query", help="Look up a symbol")
    p.add_argument("symbol", help="Symbol name")
    p.add_argument("--path", default=".", help="Project root")
    p.add_argument("--db", help="Database path")

    # trace
    p = sub.add_parser("trace", help="Trace execution paths between two symbols")
    p.add_argument("from_symbol", help="Starting symbol")
    p.add_argument("to_symbol", help="Target symbol")
    p.add_argument("--path", default=".", help="Project root")
    p.add_argument("--db", help="Database path")

    # related
    p = sub.add_parser("related", help="Find related symbols")
    p.add_argument("symbol", help="Symbol name")
    p.add_argument("--path", default=".", help="Project root")
    p.add_argument("--db", help="Database path")

    # hotspots
    p = sub.add_parser("hotspots", help="Show high-importance symbols by PageRank")
    p.add_argument("path", nargs="?", default=".", help="Project root")
    p.add_argument("-n", type=int, default=20, help="Number of results")
    p.add_argument("--db", help="Database path")

    # patterns
    p = sub.add_parser("patterns", help="Detect architectural patterns")
    p.add_argument("path", nargs="?", default=".", help="Project root")
    p.add_argument("--db", help="Database path")

    # summarize
    p = sub.add_parser("summarize", help="Generate LLM summaries for indexed symbols/files")
    p.add_argument("path", nargs="?", default=".", help="Project root")
    p.add_argument("--db", help="Database path")
    p.add_argument("--model", help="LLM model (default: haiku)")
    p.add_argument("--force", action="store_true", help="Re-summarize even if fresh")

    # serve
    p = sub.add_parser("serve", help="Start MCP server")
    p.add_argument("--http", action="store_true", help="HTTP transport instead of stdio")
    p.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")

    # viz — interactive web visualization
    p = sub.add_parser("viz", help="Launch interactive visualization web app")
    p.add_argument("path", nargs="?", default=".", help="Project root (default: .)")
    p.add_argument("--db", help="Database path (default: <project>/.rlm-code.duckdb)")
    p.add_argument("--port", type=int, default=8420, help="HTTP port (default: 8420)")
    p.add_argument("--no-open", action="store_true", help="Don't auto-open browser")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger("rlm_code").setLevel(logging.DEBUG)

    handlers = {
        "index": cmd_index,
        "status": cmd_status,
        "query": cmd_query,
        "trace": cmd_trace,
        "related": cmd_related,
        "hotspots": cmd_hotspots,
        "patterns": cmd_patterns,
        "summarize": cmd_summarize,
        "serve": cmd_serve,
        "viz": cmd_viz,
    }

    sys.exit(handlers[args.command](args))


if __name__ == "__main__":
    main()
