/**
 * tree.js — Collapsible directory/file/symbol tree for the left sidebar.
 *
 * Renders the nested JSON from /api/tree into a <ul>/<li> structure.
 * Directories and files are expandable; clicking a symbol or file
 * notifies the app (via callbacks) so the graph and details panels react.
 *
 * Public interface:
 *   Tree.render(treeData, container, callbacks)
 *   Tree.highlightNode(symbolId)
 *   Tree.expandToNode(symbolId)
 */

const Tree = (() => {

  /** Currently highlighted row element (for .active styling). */
  let _activeRow = null;

  /**
   * Icon character + CSS class for a given node type/kind.
   * Keeps the tree visually scannable at a glance.
   */
  function _icon(node) {
    if (node.type === "dir")    return { char: "\u{1F4C1}", cls: "dir" };
    if (node.type === "file")   return { char: "\u{1F4C4}", cls: "file" };
    /* symbol — color by kind */
    if (node.kind === "class")  return { char: "C", cls: "cls" };
    if (node.kind === "method") return { char: "M", cls: "meth" };
    return { char: "f", cls: "fn" };  /* function or fallback */
  }

  /**
   * Recursively build DOM nodes for one tree entry.
   *
   * @param {Object} node — tree node from the API
   * @param {Object} cb   — { onSymbol, onFile, onDir } callback set
   * @returns {HTMLElement} — <li> with nested <ul> if applicable
   */
  function _buildNode(node, cb) {
    const li = document.createElement("li");
    const hasChildren = node.children && node.children.length > 0;

    /* ── Clickable row ──────────────────────────────────────────── */
    const row = document.createElement("div");
    row.className = "tree-row";

    /* Collapse toggle (arrow) or spacer for leaf nodes */
    if (hasChildren) {
      const toggle = document.createElement("span");
      toggle.className = "tree-toggle open";
      toggle.textContent = "\u25B6";  /* right-pointing triangle */
      row.appendChild(toggle);
    } else {
      const spacer = document.createElement("span");
      spacer.className = "tree-spacer";
      row.appendChild(spacer);
    }

    /* Type/kind icon */
    const ico = _icon(node);
    const iconEl = document.createElement("span");
    iconEl.className = `tree-icon ${ico.cls}`;
    iconEl.textContent = ico.char;
    row.appendChild(iconEl);

    /* Name label */
    const name = document.createElement("span");
    name.className = "tree-name";
    name.textContent = node.name;
    row.appendChild(name);

    /* Badge — symbol count for dirs/files */
    if ((node.type === "dir" || node.type === "file") && node.symbolCount > 0) {
      const badge = document.createElement("span");
      badge.className = "tree-badge";
      badge.textContent = node.symbolCount;
      row.appendChild(badge);
    }

    li.appendChild(row);

    /* ── Children container ──────────────────────────────────────── */
    let childrenUl = null;
    if (hasChildren) {
      childrenUl = document.createElement("ul");
      childrenUl.className = "tree-children";
      for (const child of node.children) {
        childrenUl.appendChild(_buildNode(child, cb));
      }
      li.appendChild(childrenUl);
    }

    /* ── Click handler ──────────────────────────────────────────── */
    row.addEventListener("click", (e) => {
      e.stopPropagation();

      /* Toggle expand/collapse for dirs and files with children */
      if (hasChildren) {
        const toggle = row.querySelector(".tree-toggle");
        if (toggle) {
          const isOpen = toggle.classList.toggle("open");
          childrenUl.classList.toggle("collapsed", !isOpen);
        }
      }

      /* Highlight this row */
      if (_activeRow) _activeRow.classList.remove("active");
      row.classList.add("active");
      _activeRow = row;

      /* Notify the app about the selection */
      if (node.type === "symbol" && cb.onSymbol) cb.onSymbol(node.id);
      if (node.type === "file"   && cb.onFile)   cb.onFile(node.path);
      if (node.type === "dir"    && cb.onDir)     cb.onDir(node.path);
    });

    /* Store node data on the DOM element so we can look it up later */
    row.dataset.nodeId = node.id || node.path || node.name;
    row.dataset.nodeType = node.type;

    return li;
  }

  return {
    /**
     * Render the full tree into a container element.
     *
     * @param {Object} treeData — root node from /api/tree
     * @param {HTMLElement} container — DOM element to fill
     * @param {Object} callbacks — { onSymbol(id), onFile(path), onDir(path) }
     */
    render(treeData, container, callbacks) {
      container.innerHTML = "";
      const ul = document.createElement("ul");
      /* Skip the synthetic (root) wrapper — render its children directly */
      if (treeData.children) {
        for (const child of treeData.children) {
          ul.appendChild(_buildNode(child, callbacks));
        }
      }
      container.appendChild(ul);
    },

    /**
     * Expand all tree nodes on the path to a given symbol and highlight it.
     * Finds the row by scanning data-node-id attributes.
     *
     * @param {string} nodeId — symbol ID, file path, or dir path
     */
    expandToNode(nodeId) {
      const row = document.querySelector(
        `.tree-row[data-node-id="${CSS.escape(nodeId)}"]`
      );
      if (!row) return;

      /* Expand all ancestor <ul class="tree-children"> that are collapsed */
      let el = row.parentElement;
      while (el) {
        if (el.classList && el.classList.contains("tree-children")) {
          el.classList.remove("collapsed");
          /* Also flip the toggle arrow in the parent row */
          const parentRow = el.previousElementSibling;
          if (parentRow) {
            const toggle = parentRow.querySelector(".tree-toggle");
            if (toggle) toggle.classList.add("open");
          }
        }
        el = el.parentElement;
      }

      /* Highlight the row */
      if (_activeRow) _activeRow.classList.remove("active");
      row.classList.add("active");
      _activeRow = row;

      /* Scroll into view */
      row.scrollIntoView({ block: "center", behavior: "smooth" });
    },
  };
})();
