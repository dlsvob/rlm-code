"""
Symbol and edge extraction from tree-sitter ASTs.

Uses tree-walking (child_by_field_name, node.children) rather than the
Query API, which was removed in tree-sitter 0.25.
"""

import logging
from dataclasses import dataclass

from tree_sitter import Node, Tree

from .models import RawRef, Symbol
from .parse import walk_tree

log = logging.getLogger(__name__)


def _text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace") if node.text else ""


def _make_id(file_path: str, qualified_name: str) -> str:
    return f"{file_path}::{qualified_name}"


def _enclosing_node_of_type(node: Node, type_name: str) -> Node | None:
    """Walk up the parent chain looking for a node of the given type."""
    parent = node.parent
    while parent is not None:
        if parent.type == type_name:
            return parent
        parent = parent.parent
    return None


# ── Python ───────────────────────────────────────────────────────────────────

def _extract_python(
    file_path: str, tree: Tree, source: bytes
) -> tuple[list[Symbol], list[RawRef]]:
    symbols: list[Symbol] = []
    raw_refs: list[RawRef] = []

    # Pass 1: collect classes (we need their byte ranges to qualify methods)
    class_byte_to_name: dict[tuple[int, int], str] = {}
    for node in walk_tree(tree.root_node):
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(name_node)
                class_byte_to_name[(node.start_byte, node.end_byte)] = name
                sym_id = _make_id(file_path, name)
                # superclass inheritance
                args_node = node.child_by_field_name("superclasses")
                if args_node:
                    for child in args_node.children:
                        if child.type not in ("(", ")", ","):
                            base = _text(child).strip()
                            if base:
                                raw_refs.append(RawRef(
                                    source_id=sym_id,
                                    ref_text=base,
                                    kind="inherits",
                                ))
                symbols.append(Symbol(
                    id=sym_id,
                    file_path=file_path,
                    name=name,
                    qualified_name=name,
                    kind="class",
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    signature=f"class {name}",
                ))

    def _enclosing_class(node: Node) -> str | None:
        cls_node = _enclosing_node_of_type(node, "class_definition")
        if cls_node:
            return class_byte_to_name.get((cls_node.start_byte, cls_node.end_byte))
        return None

    # Pass 2: functions and methods
    func_byte_to_id: dict[tuple[int, int], str] = {}
    for node in walk_tree(tree.root_node):
        if node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue
            name = _text(name_node)
            enclosing = _enclosing_class(node)
            if enclosing:
                qname = f"{enclosing}.{name}"
                kind = "method"
            else:
                qname = name
                kind = "function"

            params_node = node.child_by_field_name("parameters")
            params_str = _text(params_node) if params_node else "()"
            ret_node = node.child_by_field_name("return_type")
            ret_str = _text(ret_node) if ret_node else ""
            sig = f"def {name}{params_str}" + (f" -> {ret_str}" if ret_str else "")

            sym_id = _make_id(file_path, qname)
            func_byte_to_id[(node.start_byte, node.end_byte)] = sym_id
            symbols.append(Symbol(
                id=sym_id,
                file_path=file_path,
                name=name,
                qualified_name=qname,
                kind=kind,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=sig,
            ))

    def _enclosing_func_id(node: Node) -> str | None:
        """Find the innermost function/method containing this node."""
        fn_node = _enclosing_node_of_type(node, "function_definition")
        if fn_node:
            return func_byte_to_id.get((fn_node.start_byte, fn_node.end_byte))
        return None

    # Pass 3: imports and calls
    for node in walk_tree(tree.root_node):
        if node.type in ("import_statement", "import_from_statement"):
            text = _text(node).strip()
            source_id = _enclosing_func_id(node) or f"{file_path}::__module__"
            raw_refs.append(RawRef(source_id=source_id, ref_text=text, kind="imports"))

        elif node.type == "call":
            func_node = node.child_by_field_name("function")
            if not func_node:
                continue
            # Direct call: foo(...)  or attribute call: obj.method(...)
            if func_node.type == "identifier":
                callee = _text(func_node)
            elif func_node.type == "attribute":
                attr = func_node.child_by_field_name("attribute")
                callee = _text(attr) if attr else ""
            else:
                continue
            if not callee or len(callee) <= 1:
                continue
            source_id = _enclosing_func_id(node)
            if source_id is None:
                continue
            raw_refs.append(RawRef(source_id=source_id, ref_text=callee, kind="calls"))

    return symbols, raw_refs


# ── Java ──────────────────────────────────────────────────────────────────────

def _extract_java(
    file_path: str, tree: Tree, source: bytes
) -> tuple[list[Symbol], list[RawRef]]:
    symbols: list[Symbol] = []
    raw_refs: list[RawRef] = []

    # Pass 1: classes
    class_byte_to_name: dict[tuple[int, int], str] = {}
    for node in walk_tree(tree.root_node):
        if node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue
            name = _text(name_node)
            class_byte_to_name[(node.start_byte, node.end_byte)] = name
            sym_id = _make_id(file_path, name)

            # superclass
            super_node = node.child_by_field_name("superclass")
            if super_node:
                # superclass node contains "extends ClassName"
                for child in super_node.children:
                    if child.type not in ("extends",) and child.is_named:
                        base = _text(child).strip()
                        if base:
                            raw_refs.append(RawRef(
                                source_id=sym_id,
                                ref_text=base,
                                kind="inherits",
                            ))

            symbols.append(Symbol(
                id=sym_id,
                file_path=file_path,
                name=name,
                qualified_name=name,
                kind="class",
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=f"class {name}",
            ))

    def _enclosing_class(node: Node) -> str | None:
        cls_node = _enclosing_node_of_type(node, "class_declaration")
        if cls_node:
            return class_byte_to_name.get((cls_node.start_byte, cls_node.end_byte))
        return None

    # Pass 2: methods
    method_byte_to_id: dict[tuple[int, int], str] = {}
    for node in walk_tree(tree.root_node):
        if node.type == "method_declaration":
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue
            name = _text(name_node)
            enclosing = _enclosing_class(node)
            qname = f"{enclosing}.{name}" if enclosing else name

            ret_node = node.child_by_field_name("type")
            ret_str = _text(ret_node) if ret_node else ""
            params_node = node.child_by_field_name("parameters")
            params_str = _text(params_node) if params_node else "()"
            sig = f"{ret_str} {name}{params_str}".strip()

            sym_id = _make_id(file_path, qname)
            method_byte_to_id[(node.start_byte, node.end_byte)] = sym_id
            symbols.append(Symbol(
                id=sym_id,
                file_path=file_path,
                name=name,
                qualified_name=qname,
                kind="method",
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=sig,
            ))

    def _enclosing_method_id(node: Node) -> str | None:
        m_node = _enclosing_node_of_type(node, "method_declaration")
        if m_node:
            return method_byte_to_id.get((m_node.start_byte, m_node.end_byte))
        return None

    # Pass 3: imports and calls
    for node in walk_tree(tree.root_node):
        if node.type == "import_declaration":
            text = _text(node).strip()
            raw_refs.append(RawRef(
                source_id=f"{file_path}::__module__",
                ref_text=text,
                kind="imports",
            ))

        elif node.type == "method_invocation":
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue
            callee = _text(name_node).strip()
            if not callee or len(callee) <= 1:
                continue
            source_id = _enclosing_method_id(node)
            if source_id is None:
                continue
            raw_refs.append(RawRef(source_id=source_id, ref_text=callee, kind="calls"))

    return symbols, raw_refs


# ── TypeScript / TSX ──────────────────────────────────────────────────────────

def _extract_typescript(
    file_path: str, tree: Tree, source: bytes
) -> tuple[list[Symbol], list[RawRef]]:
    symbols: list[Symbol] = []
    raw_refs: list[RawRef] = []

    # Pass 1: classes and interfaces (build byte-range-to-name map)
    class_byte_to_name: dict[tuple[int, int], str] = {}
    for node in walk_tree(tree.root_node):
        if node.type in ("class_declaration", "interface_declaration"):
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue
            name = _text(name_node)
            class_byte_to_name[(node.start_byte, node.end_byte)] = name
            sym_id = _make_id(file_path, name)

            # Inheritance: extends_clause lives inside class_heritage
            for child in node.children:
                if child.type == "class_heritage":
                    for heritage in child.children:
                        if heritage.type == "extends_clause":
                            # extends_clause has 'value' field for class,
                            # or child type_identifiers for interface extends
                            val = heritage.child_by_field_name("value")
                            if val:
                                raw_refs.append(RawRef(
                                    source_id=sym_id,
                                    ref_text=_text(val),
                                    kind="inherits",
                                ))
                            else:
                                # interface extends: `interface Foo extends Bar, Baz`
                                for ec in heritage.children:
                                    if ec.type in ("type_identifier", "identifier"):
                                        raw_refs.append(RawRef(
                                            source_id=sym_id,
                                            ref_text=_text(ec),
                                            kind="inherits",
                                        ))
                        elif heritage.type == "implements_clause":
                            for ic in heritage.children:
                                if ic.type in ("type_identifier", "identifier"):
                                    raw_refs.append(RawRef(
                                        source_id=sym_id,
                                        ref_text=_text(ic),
                                        kind="inherits",
                                    ))

                # interface_declaration: `interface Foo extends Bar`
                # extends_type_clause is a direct child (no class_heritage wrapper)
                if child.type == "extends_type_clause":
                    for ec in child.children:
                        if ec.type in ("type_identifier", "identifier"):
                            raw_refs.append(RawRef(
                                source_id=sym_id,
                                ref_text=_text(ec),
                                kind="inherits",
                            ))

            kind = "class"
            sig = ("interface " if node.type == "interface_declaration" else "class ") + name
            symbols.append(Symbol(
                id=sym_id,
                file_path=file_path,
                name=name,
                qualified_name=name,
                kind=kind,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=sig,
            ))

    def _enclosing_class(node: Node) -> str | None:
        for type_name in ("class_declaration", "interface_declaration"):
            cls_node = _enclosing_node_of_type(node, type_name)
            if cls_node:
                return class_byte_to_name.get((cls_node.start_byte, cls_node.end_byte))
        return None

    # Pass 2: functions and methods
    func_byte_to_id: dict[tuple[int, int], str] = {}
    for node in walk_tree(tree.root_node):
        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue
            name = _text(name_node)
            qname = name
            kind = "function"

            params_node = node.child_by_field_name("parameters")
            params_str = _text(params_node) if params_node else "()"
            ret_node = node.child_by_field_name("return_type")
            ret_str = _text(ret_node) if ret_node else ""
            sig = f"function {name}{params_str}" + (f"{ret_str}" if ret_str else "")

            sym_id = _make_id(file_path, qname)
            func_byte_to_id[(node.start_byte, node.end_byte)] = sym_id
            symbols.append(Symbol(
                id=sym_id, file_path=file_path, name=name,
                qualified_name=qname, kind=kind,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=sig,
            ))

        elif node.type == "method_definition":
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue
            name = _text(name_node)
            enclosing = _enclosing_class(node)
            if enclosing:
                qname = f"{enclosing}.{name}"
                kind = "method"
            else:
                qname = name
                kind = "function"

            params_node = node.child_by_field_name("parameters")
            params_str = _text(params_node) if params_node else "()"
            ret_node = node.child_by_field_name("return_type")
            ret_str = _text(ret_node) if ret_node else ""
            sig = f"{name}{params_str}" + (f"{ret_str}" if ret_str else "")

            sym_id = _make_id(file_path, qname)
            func_byte_to_id[(node.start_byte, node.end_byte)] = sym_id
            symbols.append(Symbol(
                id=sym_id, file_path=file_path, name=name,
                qualified_name=qname, kind=kind,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=sig,
            ))

        elif node.type == "arrow_function":
            # Only extract arrow functions assigned to a variable
            parent = node.parent
            if not parent or parent.type != "variable_declarator":
                continue
            name_node = parent.child_by_field_name("name")
            if not name_node:
                continue
            name = _text(name_node)
            qname = name
            kind = "function"

            params_node = node.child_by_field_name("parameters")
            params_str = _text(params_node) if params_node else "()"
            ret_node = node.child_by_field_name("return_type")
            ret_str = _text(ret_node) if ret_node else ""
            sig = f"const {name} = {params_str}" + (f"{ret_str}" if ret_str else "") + " => ..."

            sym_id = _make_id(file_path, qname)
            func_byte_to_id[(node.start_byte, node.end_byte)] = sym_id
            symbols.append(Symbol(
                id=sym_id, file_path=file_path, name=name,
                qualified_name=qname, kind=kind,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=sig,
            ))

    def _enclosing_func_id(node: Node) -> str | None:
        """Find innermost function/method/arrow containing this node."""
        parent = node.parent
        while parent is not None:
            if parent.type in ("function_declaration", "method_definition", "arrow_function"):
                fid = func_byte_to_id.get((parent.start_byte, parent.end_byte))
                if fid:
                    return fid
            parent = parent.parent
        return None

    # Pass 3: imports and calls
    for node in walk_tree(tree.root_node):
        if node.type == "import_statement":
            source_node = node.child_by_field_name("source")
            module_str = ""
            if source_node:
                # Strip quotes from string literal
                raw = _text(source_node)
                module_str = raw.strip("'\"")

            # Find imported names from the import_clause
            imported_names: list[str] = []
            for child in node.children:
                if child.type == "import_clause":
                    for ic_child in child.children:
                        if ic_child.type == "identifier":
                            # default import: import React from 'react'
                            imported_names.append(_text(ic_child))
                        elif ic_child.type == "named_imports":
                            for spec in ic_child.children:
                                if spec.type == "import_specifier":
                                    spec_name = spec.child_by_field_name("name")
                                    if spec_name:
                                        imported_names.append(_text(spec_name))
                        elif ic_child.type == "namespace_import":
                            # import * as X from 'module'
                            for ns_child in ic_child.children:
                                if ns_child.type == "identifier":
                                    imported_names.append(_text(ns_child))

            source_id = _enclosing_func_id(node) or f"{file_path}::__module__"
            for imp_name in imported_names:
                raw_refs.append(RawRef(
                    source_id=source_id,
                    ref_text=f"{imp_name}|{module_str}",
                    kind="imports",
                ))

        elif node.type == "call_expression":
            func_node = node.child_by_field_name("function")
            if not func_node:
                continue
            if func_node.type == "identifier":
                callee = _text(func_node)
            elif func_node.type == "member_expression":
                prop = func_node.child_by_field_name("property")
                callee = _text(prop) if prop else ""
            else:
                continue
            if not callee or len(callee) <= 1:
                continue
            source_id = _enclosing_func_id(node)
            if source_id is None:
                continue
            raw_refs.append(RawRef(source_id=source_id, ref_text=callee, kind="calls"))

    return symbols, raw_refs


# ── Dispatch ──────────────────────────────────────────────────────────────────

def extract(
    file_path: str,
    language: str,
    tree: Tree,
    source: bytes,
) -> tuple[list[Symbol], list[RawRef]]:
    """
    Extract symbols and raw references from a parsed AST.
    Never raises — returns empty lists on error.
    """
    try:
        if language == "python":
            return _extract_python(file_path, tree, source)
        elif language == "java":
            return _extract_java(file_path, tree, source)
        elif language in ("typescript", "tsx"):
            return _extract_typescript(file_path, tree, source)
        else:
            log.warning("No extractor for language: %s", language)
            return [], []
    except Exception as e:
        log.warning("Extraction error in %s (%s): %s", file_path, language, e, exc_info=True)
        return [], []
