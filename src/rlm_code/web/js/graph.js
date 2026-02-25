/**
 * graph.js — D3 force-directed call graph for the center panel.
 *
 * Renders every symbol as a node (sized by PageRank, colored by kind)
 * and every resolved edge as a line.  Supports zoom/pan, drag, hover
 * tooltips, click-to-select, and double-click-to-neighborhood.
 *
 * Public interface:
 *   Graph.init(svgSelector, graphData, callbacks)
 *   Graph.selectNode(symbolId)
 *   Graph.highlightFile(filePath)
 *   Graph.filterNeighborhood(symbolId)
 *   Graph.resetView()
 */

const Graph = (() => {

  /* ── State ──────────────────────────────────────────────────────── */
  let _svg, _g;                     /* D3 selections: root SVG, inner <g> */
  let _simulation;                  /* d3.forceSimulation instance */
  let _nodeEls, _edgeEls, _labelEls; /* D3 selections for the drawn elements */
  let _allNodes = [];               /* full dataset — kept for reset */
  let _allEdges = [];
  let _nodeMap = {};                /* id → node object for quick lookup */
  let _selectedId = null;           /* currently selected node ID */
  let _callbacks = {};              /* { onSelect(id) } from the app */
  let _isFiltered = false;          /* true when showing a neighborhood subset */

  /* ── Constants ──────────────────────────────────────────────────── */
  const KIND_COLOR = {
    class:    "#89b4fa",
    function: "#a6e3a1",
    method:   "#fab387",
  };
  const DEFAULT_COLOR = "#7f849c";

  /** Compute node radius from PageRank using a sqrt scale so very
   *  high-PR nodes don't dominate the visual too much. */
  function _radius(pr) {
    return Math.max(3, Math.sqrt(pr * 5000));
  }

  /* ── Drawing helpers ────────────────────────────────────────────── */

  /**
   * (Re)draw the graph with the given node/edge subsets.
   * Called on initial load and when filtering to a neighborhood.
   */
  function _draw(nodes, edges) {
    _g.selectAll("*").remove();

    /* Build id-indexed lookup and node set for edge filtering */
    const nodeSet = new Set(nodes.map(n => n.id));

    /* Only draw edges whose both ends are in the current node set */
    const visibleEdges = edges.filter(
      e => nodeSet.has(e.source.id || e.source) && nodeSet.has(e.target.id || e.target)
    );

    /* ── Edges (drawn first so nodes render on top) ──────────── */
    _edgeEls = _g.append("g").attr("class", "edges")
      .selectAll("line")
      .data(visibleEdges)
      .enter().append("line")
        .attr("class", e => `edge ${e.kind}`)
        .attr("stroke-width", 1);

    /* ── Nodes ────────────────────────────────────────────────── */
    const nodeGroup = _g.append("g").attr("class", "nodes")
      .selectAll("g")
      .data(nodes, d => d.id)
      .enter().append("g")
        .attr("class", "node")
        .call(d3.drag()
          .on("start", _dragStart)
          .on("drag", _dragging)
          .on("end", _dragEnd));

    nodeGroup.append("circle")
      .attr("r", d => _radius(d.pagerank))
      .attr("fill", d => KIND_COLOR[d.kind] || DEFAULT_COLOR);

    /* Labels — only shown for nodes above a minimum PageRank so the
       graph doesn't become an unreadable soup of text. */
    _labelEls = nodeGroup.append("text")
      .text(d => d.name)
      .attr("dy", d => _radius(d.pagerank) + 10)
      .style("display", d => d.pagerank > 0.005 || nodes.length < 80 ? null : "none");

    _nodeEls = nodeGroup;

    /* ── Hover tooltip ────────────────────────────────────────── */
    const tooltip = document.getElementById("tooltip");

    nodeGroup
      .on("mouseover", (event, d) => {
        tooltip.classList.remove("hidden");
        tooltip.textContent = `${d.qualifiedName}\n${d.kind}  PR: ${d.pagerank.toFixed(4)}`;
      })
      .on("mousemove", (event) => {
        tooltip.style.left = (event.pageX + 12) + "px";
        tooltip.style.top  = (event.pageY - 10) + "px";
      })
      .on("mouseout", () => {
        tooltip.classList.add("hidden");
      });

    /* ── Click → select node ──────────────────────────────────── */
    nodeGroup.on("click", (event, d) => {
      event.stopPropagation();
      _select(d.id);
    });

    /* ── Double-click → filter to neighborhood ─────────────────── */
    nodeGroup.on("dblclick", (event, d) => {
      event.stopPropagation();
      Graph.filterNeighborhood(d.id);
    });

    /* ── Click on background → deselect ───────────────────────── */
    _svg.on("click", () => _select(null));

    /* ── Force simulation ─────────────────────────────────────── */
    const width  = _svg.node().clientWidth;
    const height = _svg.node().clientHeight;

    _simulation = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(visibleEdges)
        .id(d => d.id)
        .distance(60))
      .force("charge", d3.forceManyBody()
        .strength(-80))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("collision", d3.forceCollide()
        .radius(d => _radius(d.pagerank) + 2))
      .on("tick", _tick);
  }

  /** Called every simulation tick — update positions of lines and nodes. */
  function _tick() {
    _edgeEls
      .attr("x1", d => d.source.x)
      .attr("y1", d => d.source.y)
      .attr("x2", d => d.target.x)
      .attr("y2", d => d.target.y);

    _nodeEls
      .attr("transform", d => `translate(${d.x},${d.y})`);
  }

  /* ── Drag callbacks ─────────────────────────────────────────────── */
  function _dragStart(event, d) {
    if (!event.active) _simulation.alphaTarget(0.3).restart();
    d.fx = d.x;
    d.fy = d.y;
  }
  function _dragging(event, d) {
    d.fx = event.x;
    d.fy = event.y;
  }
  function _dragEnd(event, d) {
    if (!event.active) _simulation.alphaTarget(0);
    d.fx = null;
    d.fy = null;
  }

  /* ── Selection ──────────────────────────────────────────────────── */

  /**
   * Select a node by ID (or deselect if null).  Highlights the node
   * and its connected edges, and notifies the app via callback.
   */
  function _select(id) {
    _selectedId = id;

    /* Visual: mark the selected circle and dim non-connected edges */
    _nodeEls.selectAll("circle")
      .classed("selected", d => d.id === id);

    _edgeEls
      .classed("highlighted", d =>
        id && ((d.source.id || d.source) === id || (d.target.id || d.target) === id)
      );

    if (id && _callbacks.onSelect) _callbacks.onSelect(id);
  }


  /* ── Public API ─────────────────────────────────────────────────── */

  return {
    /**
     * Initialize the graph: set up SVG, zoom, and draw the full graph.
     *
     * @param {string} svgSelector — CSS selector for the <svg> element
     * @param {Object} data — { nodes: [...], edges: [...] } from /api/graph
     * @param {Object} callbacks — { onSelect(symbolId) }
     */
    init(svgSelector, data, callbacks) {
      _callbacks = callbacks || {};
      _allNodes = data.nodes;
      _allEdges = data.edges;
      _nodeMap = {};
      for (const n of _allNodes) _nodeMap[n.id] = n;

      _svg = d3.select(svgSelector);
      _g = _svg.append("g");

      /* Zoom and pan */
      const zoom = d3.zoom()
        .scaleExtent([0.1, 8])
        .on("zoom", (event) => _g.attr("transform", event.transform));
      _svg.call(zoom);

      _draw(_allNodes, _allEdges);

      /* Update status text */
      document.getElementById("graph-status").textContent =
        `${_allNodes.length} nodes, ${_allEdges.length} edges`;
    },

    /**
     * Programmatically select a node (e.g. from tree or search click).
     * Centers the viewport on the node.
     */
    selectNode(symbolId) {
      _select(symbolId);

      /* Try to center the view on the selected node */
      const node = _nodeMap[symbolId];
      if (node && node.x != null) {
        const width  = _svg.node().clientWidth;
        const height = _svg.node().clientHeight;
        const transform = d3.zoomIdentity
          .translate(width / 2 - node.x, height / 2 - node.y);
        _svg.transition().duration(500)
          .call(d3.zoom().transform, transform);
      }
    },

    /**
     * Dim all nodes except those belonging to the given file.
     * Used when the user clicks a file in the tree.
     */
    highlightFile(filePath) {
      if (!_nodeEls) return;
      _nodeEls.selectAll("circle")
        .style("opacity", d => d.filePath === filePath ? 1 : 0.15);
      _nodeEls.selectAll("text")
        .style("display", d => d.filePath === filePath ? null : "none");
      _edgeEls.style("opacity", 0.05);
    },

    /**
     * Filter the graph to show only the immediate neighborhood of a
     * node: the node itself plus all callers and callees.  The "Reset
     * view" button restores the full graph.
     */
    filterNeighborhood(symbolId) {
      /* Collect the set of neighbor IDs from the edge data */
      const neighbors = new Set([symbolId]);
      for (const e of _allEdges) {
        const src = e.source.id || e.source;
        const tgt = e.target.id || e.target;
        if (src === symbolId) neighbors.add(tgt);
        if (tgt === symbolId) neighbors.add(src);
      }

      /* Deep-clone nodes to avoid mutating positions from the full sim */
      const subNodes = _allNodes
        .filter(n => neighbors.has(n.id))
        .map(n => ({ ...n, x: undefined, y: undefined, vx: 0, vy: 0 }));
      const subEdges = _allEdges
        .filter(e => {
          const src = e.source.id || e.source;
          const tgt = e.target.id || e.target;
          return neighbors.has(src) && neighbors.has(tgt);
        })
        .map(e => ({
          source: e.source.id || e.source,
          target: e.target.id || e.target,
          kind: e.kind,
        }));

      if (_simulation) _simulation.stop();
      _draw(subNodes, subEdges);
      _isFiltered = true;
      document.getElementById("btn-reset-graph").classList.remove("hidden");
      document.getElementById("graph-status").textContent =
        `Neighborhood: ${subNodes.length} nodes, ${subEdges.length} edges`;

      /* Auto-select the center node */
      _select(symbolId);
    },

    /**
     * Reset back to the full graph after a neighborhood filter.
     * Clones nodes to get fresh positions for the simulation.
     */
    resetView() {
      if (!_isFiltered) return;
      /* Reset positions so the simulation re-layouts from scratch */
      const freshNodes = _allNodes.map(n => ({
        ...n, x: undefined, y: undefined, vx: 0, vy: 0,
      }));
      const freshEdges = _allEdges.map(e => ({
        source: e.source.id || e.source,
        target: e.target.id || e.target,
        kind: e.kind,
      }));
      _allNodes = freshNodes;
      _allEdges = freshEdges;
      _nodeMap = {};
      for (const n of _allNodes) _nodeMap[n.id] = n;

      if (_simulation) _simulation.stop();
      _draw(_allNodes, _allEdges);
      _isFiltered = false;
      document.getElementById("btn-reset-graph").classList.add("hidden");
      document.getElementById("graph-status").textContent =
        `${_allNodes.length} nodes, ${_allEdges.length} edges`;

      /* Clear file highlighting */
      if (_nodeEls) {
        _nodeEls.selectAll("circle").style("opacity", 1);
        _edgeEls.style("opacity", 1);
      }
    },
  };
})();
