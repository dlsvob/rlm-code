"""
NetworkX graph construction, metric computation, and pattern detection.
"""

import logging
from collections import defaultdict

import networkx as nx

from .models import Edge, PatternReport, Symbol, SymbolMetrics

log = logging.getLogger(__name__)

# Thresholds for pattern detection
_GOD_OBJECT_DEGREE = 20     # in_degree + out_degree above this → god object
_BETWEENNESS_SAMPLE = 500   # use k-sample for large graphs


def build_graph(symbols: list[Symbol], edges: list[Edge]) -> nx.DiGraph:
    """Build a directed graph from symbols and resolved edges."""
    g: nx.DiGraph = nx.DiGraph()

    for s in symbols:
        g.add_node(s.id, name=s.name, kind=s.kind, file=s.file_path)

    for e in edges:
        if e.resolved:
            g.add_edge(e.source_id, e.target_id, kind=e.kind)

    log.info("Graph: %d nodes, %d edges", g.number_of_nodes(), g.number_of_edges())
    return g


def compute_metrics(g: nx.DiGraph) -> list[SymbolMetrics]:
    """Compute per-symbol metrics: degree, betweenness, pagerank."""
    if g.number_of_nodes() == 0:
        return []

    n = g.number_of_nodes()

    # PageRank — use pure-Python implementation to avoid scipy dependency
    try:
        pr = nx.pagerank_numpy(g) if n < 500 else nx.pagerank(g, max_iter=200)
    except Exception:
        try:
            # fall back to pure-Python power iteration
            pr = {node: 1.0 / n for node in g.nodes()}
            for _ in range(100):
                new_pr: dict = {}
                for node in g.nodes():
                    in_sum = sum(
                        pr[p] / (g.out_degree(p) or 1) for p in g.predecessors(node)
                    )
                    new_pr[node] = 0.15 / n + 0.85 * in_sum
                pr = new_pr
        except Exception:
            pr = {node: 1.0 / n for node in g.nodes()}

    # Betweenness — sample for large graphs
    k = min(n, _BETWEENNESS_SAMPLE)
    try:
        if n <= _BETWEENNESS_SAMPLE:
            bc = nx.betweenness_centrality(g, normalized=True)
        else:
            bc = nx.betweenness_centrality(g, k=k, normalized=True)
    except Exception as e:
        log.warning("Betweenness computation failed: %s", e)
        bc = {node: 0.0 for node in g.nodes()}

    metrics: list[SymbolMetrics] = []
    for node in g.nodes():
        metrics.append(SymbolMetrics(
            symbol_id=node,
            in_degree=g.in_degree(node),
            out_degree=g.out_degree(node),
            betweenness=bc.get(node, 0.0),
            pagerank=pr.get(node, 0.0),
        ))

    return metrics


def detect_patterns(
    g: nx.DiGraph,
    symbols: list[Symbol],
    top_n: int = 10,
) -> PatternReport:
    """Detect architectural patterns in the graph."""
    symbol_map = {s.id: s for s in symbols}

    # God objects: high total degree
    god_objects: list[str] = []
    for node in g.nodes():
        total = g.in_degree(node) + g.out_degree(node)
        if total >= _GOD_OBJECT_DEGREE:
            god_objects.append(node)
    god_objects.sort(key=lambda n: g.in_degree(n) + g.out_degree(n), reverse=True)

    # Orphans: no edges at all (dead code candidates)
    orphans: list[str] = [
        node for node in g.nodes()
        if g.in_degree(node) == 0 and g.out_degree(node) == 0
    ]

    # Cycles — find strongly connected components with size > 1
    cycles: list[list[str]] = []
    try:
        for scc in nx.strongly_connected_components(g):
            if len(scc) > 1:
                cycles.append(sorted(scc))
    except Exception as e:
        log.warning("Cycle detection failed: %s", e)

    # Hub files: files with most total edge count
    file_edge_count: dict[str, int] = defaultdict(int)
    for u, v in g.edges():
        u_sym = symbol_map.get(u)
        v_sym = symbol_map.get(v)
        if u_sym:
            file_edge_count[u_sym.file_path] += 1
        if v_sym:
            file_edge_count[v_sym.file_path] += 1
    hub_files = sorted(file_edge_count, key=file_edge_count.get, reverse=True)[:top_n]  # type: ignore[arg-type]

    return PatternReport(
        god_objects=god_objects[:top_n],
        orphans=orphans[:top_n],
        cycles=cycles[:top_n],
        hub_files=hub_files,
    )


def shortest_paths(g: nx.DiGraph, from_id: str, to_id: str, max_paths: int = 5) -> list[list[str]]:
    """Return up to max_paths shortest paths between two symbol IDs."""
    if from_id not in g or to_id not in g:
        return []
    try:
        paths = list(nx.all_shortest_paths(g, from_id, to_id))
        return paths[:max_paths]
    except nx.NetworkXNoPath:
        return []
    except nx.NodeNotFound:
        return []


def reachable_from(g: nx.DiGraph, start_id: str, depth: int = 3) -> list[str]:
    """Return all nodes reachable from start_id within depth hops."""
    if start_id not in g:
        return []
    nodes: set[str] = set()
    frontier = {start_id}
    for _ in range(depth):
        next_frontier: set[str] = set()
        for node in frontier:
            for succ in g.successors(node):
                if succ not in nodes:
                    nodes.add(succ)
                    next_frontier.add(succ)
        frontier = next_frontier
        if not frontier:
            break
    nodes.discard(start_id)
    return sorted(nodes)
