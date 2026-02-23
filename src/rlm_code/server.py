"""
MCP server for rlm-code — exposes graph-aware code navigation tools to Claude.

IMPORTANT: Uses stdio transport. Never print to stdout — all logging goes to stderr.
"""

import json
import logging
import sys
from pathlib import Path

# All logging must go to stderr in stdio MCP mode
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)

from mcp.server.fastmcp import FastMCP

from .graph import build_graph, detect_patterns, reachable_from, shortest_paths
from .indexer import run_index
from .models import IndexConfig
from .store import CodeStore

log = logging.getLogger(__name__)

mcp = FastMCP(
    "rlm-code",
    instructions=(
        "Graph-aware code navigation for large codebases. "
        "Use index_project first, then query symbols, trace flows, "
        "and detect architectural patterns."
    ),
)


def _default_db(project_root: str) -> str:
    return str(Path(project_root).resolve() / ".rlm-code.duckdb")


def _open_store(project: str) -> CodeStore:
    root = str(Path(project).resolve())
    return CodeStore(_default_db(root))


# ── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
def index_project(path: str = ".", force: bool = False) -> str:
    """
    Index or re-index a project directory. Builds the call/dependency graph.
    Run this first before using other tools on a project.

    Args:
        path: Absolute or relative path to the project root directory.
        force: If True, discard the existing index and rebuild from scratch.
    """
    root = str(Path(path).resolve())
    db_path = _default_db(root)
    config = IndexConfig(project_root=root, db_path=db_path)
    stats = run_index(config, force=force)
    return (
        f"Indexed {root}\n"
        f"  files:   {stats.get('files', 0)}\n"
        f"  symbols: {stats.get('symbols', 0)}\n"
        f"  edges:   {stats.get('edges', 0)}\n"
        + (f"  errors:  {stats['errors']}\n" if stats.get("errors") else "")
    )


@mcp.tool()
def symbol_info(name: str, project: str = ".") -> str:
    """
    Look up a symbol by name. Returns definition location, signature,
    callers, callees, and graph metrics.

    Args:
        name: The function, class, or method name to look up.
        project: Path to the project root (must have been indexed).
    """
    store = _open_store(project)
    try:
        symbols = store.find_symbols_by_name(name)
        if not symbols:
            return f"Symbol not found: {name}"

        lines: list[str] = []
        for sym in symbols:
            metrics = store.get_metrics(sym.id)
            callers = store.get_callers(sym.id)
            callees = store.get_callees(sym.id)
            lines.append(f"{sym.kind.upper()}: {sym.qualified_name}")
            lines.append(f"  location:  {sym.file_path}:{sym.start_line}–{sym.end_line}")
            lines.append(f"  signature: {sym.signature}")
            if metrics:
                lines.append(f"  pagerank: {metrics.pagerank:.4f}  betweenness: {metrics.betweenness:.4f}")
                lines.append(f"  callers: {metrics.in_degree}  callees: {metrics.out_degree}")
            if callers:
                caller_names = []
                for c in callers[:10]:
                    csym = store.get_symbol(c)
                    caller_names.append(csym.qualified_name if csym else c.split("::")[-1])
                lines.append(f"  called by: {', '.join(caller_names)}")
            if callees:
                callee_names = []
                for c in callees[:10]:
                    csym = store.get_symbol(c)
                    callee_names.append(csym.qualified_name if csym else c.split("::")[-1])
                lines.append(f"  calls: {', '.join(callee_names)}")
            lines.append("")
        return "\n".join(lines)
    finally:
        store.close()


@mcp.tool()
def trace_flow(from_symbol: str, to_symbol: str, project: str = ".") -> str:
    """
    Find execution paths between two symbols in the call graph.
    Useful for understanding how one function eventually leads to another.

    Args:
        from_symbol: Starting symbol name.
        to_symbol: Target symbol name.
        project: Path to the project root.
    """
    store = _open_store(project)
    try:
        all_symbols = []
        for fp in store.all_file_paths():
            all_symbols.extend(store.symbols_in_file(fp))
        all_edges = store.all_edges()
        g = build_graph(all_symbols, all_edges)

        from_syms = store.find_symbols_by_name(from_symbol)
        to_syms = store.find_symbols_by_name(to_symbol)

        if not from_syms:
            return f"Symbol not found: {from_symbol}"
        if not to_syms:
            return f"Symbol not found: {to_symbol}"

        lines: list[str] = []
        found = False
        for fsym in from_syms:
            for tsym in to_syms:
                paths = shortest_paths(g, fsym.id, tsym.id)
                if paths:
                    found = True
                    lines.append(f"{fsym.qualified_name} → {tsym.qualified_name}:")
                    for i, path in enumerate(paths, 1):
                        step_names = []
                        for step_id in path:
                            ssym = store.get_symbol(step_id)
                            step_names.append(ssym.qualified_name if ssym else step_id.split("::")[-1])
                        lines.append(f"  Path {i}: {' → '.join(step_names)}")
                    lines.append("")

        if not found:
            return f"No path found from '{from_symbol}' to '{to_symbol}'"
        return "\n".join(lines)
    finally:
        store.close()


@mcp.tool()
def find_related(symbol: str, project: str = ".") -> str:
    """
    Find symbols related to the given symbol by graph proximity:
    callers, callees, and symbols reachable within 2 hops.

    Args:
        symbol: Symbol name to find relations for.
        project: Path to the project root.
    """
    store = _open_store(project)
    try:
        symbols = store.find_symbols_by_name(symbol)
        if not symbols:
            return f"Symbol not found: {symbol}"

        all_syms = []
        for fp in store.all_file_paths():
            all_syms.extend(store.symbols_in_file(fp))
        g = build_graph(all_syms, store.all_edges())

        lines: list[str] = []
        sym = symbols[0]
        lines.append(f"Relations for: {sym.qualified_name}  ({sym.file_path})")

        callers = store.get_callers(sym.id)
        if callers:
            lines.append(f"\nDirect callers ({len(callers)}):")
            for c in callers[:10]:
                csym = store.get_symbol(c)
                name = csym.qualified_name if csym else c.split("::")[-1]
                loc = f"  ({csym.file_path}:{csym.start_line})" if csym else ""
                lines.append(f"  ← {name}{loc}")

        callees = store.get_callees(sym.id)
        if callees:
            lines.append(f"\nDirect callees ({len(callees)}):")
            for c in callees[:10]:
                csym = store.get_symbol(c)
                name = csym.qualified_name if csym else c.split("::")[-1]
                loc = f"  ({csym.file_path}:{csym.start_line})" if csym else ""
                lines.append(f"  → {name}{loc}")

        nearby = reachable_from(g, sym.id, depth=2)
        new_nearby = [n for n in nearby if n not in {c for c in callers + callees}]
        if new_nearby:
            lines.append(f"\nAlso reachable (2 hops, {len(new_nearby)} total):")
            for n in new_nearby[:8]:
                nsym = store.get_symbol(n)
                name = nsym.qualified_name if nsym else n.split("::")[-1]
                lines.append(f"  ·· {name}")

        return "\n".join(lines)
    finally:
        store.close()


@mcp.tool()
def hot_paths(entry_point: str, project: str = ".") -> str:
    """
    Find the most important symbols reachable from an entry point,
    ranked by PageRank (graph importance).

    Args:
        entry_point: Starting symbol name (e.g. "main", "run", "handle_request").
        project: Path to the project root.
    """
    store = _open_store(project)
    try:
        syms = store.find_symbols_by_name(entry_point)
        if not syms:
            return f"Symbol not found: {entry_point}"

        all_syms = []
        for fp in store.all_file_paths():
            all_syms.extend(store.symbols_in_file(fp))
        g = build_graph(all_syms, store.all_edges())

        sym = syms[0]
        reachable = reachable_from(g, sym.id, depth=5)

        # Rank by pagerank
        ranked = []
        for node_id in reachable:
            m = store.get_metrics(node_id)
            pr = m.pagerank if m else 0.0
            ranked.append((node_id, pr))
        ranked.sort(key=lambda x: x[1], reverse=True)

        lines = [f"Hot paths from: {sym.qualified_name}\n"]
        for node_id, pr in ranked[:15]:
            nsym = store.get_symbol(node_id)
            if nsym:
                m = store.get_metrics(node_id)
                in_d = m.in_degree if m else 0
                lines.append(
                    f"  {nsym.qualified_name:<45} pr={pr:.4f}  callers={in_d}"
                    f"  ({nsym.file_path}:{nsym.start_line})"
                )
        return "\n".join(lines)
    finally:
        store.close()


@mcp.tool()
def module_summary(path: str, project: str = ".") -> str:
    """
    Get a structural summary of a file or directory:
    symbols defined, their kinds, call counts, and any cached LLM summary.

    Args:
        path: Relative path to a file or directory within the project.
        project: Path to the project root.
    """
    store = _open_store(project)
    try:
        root = Path(project).resolve()
        target = Path(path)
        if not target.is_absolute():
            target = root / target
        rel = target.relative_to(root).as_posix()

        # File or directory?
        if target.is_file():
            syms = store.symbols_in_file(rel)
            lines = [f"File: {rel}  ({len(syms)} symbols)\n"]
            for sym in sorted(syms, key=lambda s: s.start_line):
                m = store.get_metrics(sym.id)
                in_d = m.in_degree if m else 0
                out_d = m.out_degree if m else 0
                lines.append(
                    f"  {sym.kind:<8} {sym.qualified_name:<40} "
                    f"L{sym.start_line}  in={in_d} out={out_d}"
                )
            summary = store.get_summary(rel)
            if summary and summary.summary_text and not summary.is_stale:
                lines.append(f"\nSummary: {summary.summary_text}")
            return "\n".join(lines)

        elif target.is_dir():
            # Summarise all files under this directory
            rel_prefix = rel.rstrip("/") + "/"
            all_fps = [fp for fp in store.all_file_paths()
                       if fp.startswith(rel_prefix) or fp == rel]
            if not all_fps:
                return f"No indexed files found under: {rel}"

            lines = [f"Directory: {rel}  ({len(all_fps)} files)\n"]
            total_syms = 0
            for fp in sorted(all_fps):
                syms = store.symbols_in_file(fp)
                total_syms += len(syms)
                kinds = {}
                for s in syms:
                    kinds[s.kind] = kinds.get(s.kind, 0) + 1
                kind_str = "  ".join(f"{k}×{v}" for k, v in sorted(kinds.items()))
                lines.append(f"  {fp:<50} {len(syms):3} symbols  {kind_str}")
            lines.append(f"\nTotal: {total_syms} symbols across {len(all_fps)} files")
            return "\n".join(lines)

        else:
            return f"Path not found: {path}"
    finally:
        store.close()


@mcp.tool()
def detect_patterns_tool(project: str = ".") -> str:
    """
    Detect architectural patterns in the codebase:
    god objects, orphaned symbols, circular dependencies, and hub files.

    Args:
        project: Path to the project root.
    """
    store = _open_store(project)
    try:
        all_syms = []
        for fp in store.all_file_paths():
            all_syms.extend(store.symbols_in_file(fp))
        g = build_graph(all_syms, store.all_edges())
        report = detect_patterns(g, all_syms)

        lines = ["=== Architectural Patterns ===\n"]

        lines.append(f"God Objects ({len(report.god_objects)}) — very high in+out degree:")
        for sid in report.god_objects:
            sym = store.get_symbol(sid)
            m = store.get_metrics(sid)
            if sym and m:
                lines.append(
                    f"  {sym.qualified_name}  (in={m.in_degree} out={m.out_degree})"
                    f"  {sym.file_path}"
                )

        lines.append(f"\nOrphans ({len(report.orphans)}) — no callers, no callees:")
        for sid in report.orphans:
            sym = store.get_symbol(sid)
            if sym:
                lines.append(f"  {sym.qualified_name}  {sym.file_path}:{sym.start_line}")

        lines.append(f"\nCircular Dependencies ({len(report.cycles)}):")
        for cycle in report.cycles:
            names = [c.split("::")[-1] for c in cycle]
            lines.append(f"  {' ↔ '.join(names)}")

        lines.append(f"\nHub Files (most edges):")
        for fp in report.hub_files:
            lines.append(f"  {fp}")

        return "\n".join(lines)
    finally:
        store.close()


@mcp.tool()
def project_overview(project: str = ".") -> str:
    """
    High-level overview of an indexed project: file count, language breakdown,
    symbol counts, top symbols by importance, and graph density.

    Args:
        project: Path to the project root.
    """
    store = _open_store(project)
    try:
        stats = store.stats()
        last_commit = store.get_meta("last_indexed_commit")
        top = store.top_by_pagerank(n=10)

        root = str(Path(project).resolve())
        lines = [
            f"Project: {root}",
            f"Indexed commit: {last_commit or '(unknown)'}",
            f"",
            f"Files:   {stats.get('files', 0)}",
        ]
        if "by_language" in stats:
            for lang, count in stats["by_language"].items():
                lines.append(f"  {lang}: {count}")

        lines.append(f"Symbols: {stats.get('symbols', 0)}")
        if "by_kind" in stats:
            for kind, count in stats["by_kind"].items():
                lines.append(f"  {kind}: {count}")

        edges = stats.get("edges", 0)
        syms = stats.get("symbols", 0)
        density = edges / (syms * (syms - 1)) if syms > 1 else 0
        lines.append(f"Edges:   {edges}  (graph density: {density:.4f})")

        lines.append(f"\nTop symbols by PageRank:")
        for sym_id, pr in top:
            sym = store.get_symbol(sym_id)
            if sym:
                lines.append(f"  {sym.qualified_name:<45} pr={pr:.4f}  ({sym.file_path})")

        return "\n".join(lines)
    finally:
        store.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def run_server(http: bool = False, port: int = 8000) -> None:
    if http:
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    run_server()
