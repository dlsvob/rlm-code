"""
Cross-file symbol resolution.

Converts RawRef objects into resolved Edge objects by matching import/call
references against the global symbol index.
"""

import logging
import re
from pathlib import Path

from .models import Edge, RawRef, Symbol

log = logging.getLogger(__name__)


def _dotted_to_path(dotted: str, root: Path, current_file: str) -> list[str]:
    """
    Convert a Python dotted module name to candidate relative file paths.

    e.g. "foo.bar" → ["foo/bar.py", "foo/bar/__init__.py"]
    e.g. ".utils" (relative) → resolved relative to current file's directory
    """
    candidates: list[str] = []
    parts = dotted.lstrip(".").split(".")
    if parts and parts[0]:
        path = "/".join(parts)
        candidates.append(f"{path}.py")
        candidates.append(f"{path}/__init__.py")
    return candidates


def _parse_python_import(ref_text: str) -> list[str]:
    """
    Parse a Python import statement and return candidate symbol names / module paths.

    "import os.path"               → ["os.path"]
    "from extract import normalize_gene" → ["normalize_gene", "extract.normalize_gene"]
    "from .utils import helper"    → ["helper", "utils.helper"]
    """
    ref_text = ref_text.strip()
    names: list[str] = []

    # from X import a, b, c
    m = re.match(r"from\s+([\w.]+)\s+import\s+(.+)", ref_text)
    if m:
        module = m.group(1).lstrip(".")
        imported = [n.strip().split(" as ")[0].strip() for n in m.group(2).split(",")]
        for imp in imported:
            if imp == "*":
                names.append(module)
            else:
                names.append(imp)
                if module:
                    names.append(f"{module}.{imp}")
        return names

    # import X
    m = re.match(r"import\s+(.+)", ref_text)
    if m:
        for part in m.group(1).split(","):
            names.append(part.strip().split(" as ")[0].strip())
        return names

    return names


def _parse_java_import(ref_text: str) -> list[str]:
    """
    Parse a Java import declaration and return the simple class name.

    "import com.example.Foo;"  → ["Foo", "com.example.Foo"]
    "import java.util.*;"      → []  (wildcard, skip)
    """
    ref_text = ref_text.strip().rstrip(";")
    m = re.match(r"import\s+(static\s+)?([\w.]+)", ref_text)
    if not m:
        return []
    fqcn = m.group(2)
    if fqcn.endswith(".*"):
        return []
    simple = fqcn.split(".")[-1]
    return [simple, fqcn]


def _parse_typescript_import(ref_text: str) -> list[str]:
    """
    Parse a TypeScript import ref_text in the format "name|module_specifier".

    Returns candidate symbol names for resolution.

    "Foo|./utils"        → ["Foo"]
    "React|react"        → ["React"]
    "path|path"          → ["path"]
    """
    if "|" not in ref_text:
        return [ref_text] if ref_text else []
    name, _module = ref_text.split("|", 1)
    # The imported name is what we match against the symbol index
    return [name] if name else []


class Resolver:
    def __init__(self, symbols: list[Symbol]) -> None:
        # Index by simple name and by qualified name
        self._by_name: dict[str, list[Symbol]] = {}
        self._by_id: dict[str, Symbol] = {}

        for s in symbols:
            self._by_id[s.id] = s
            self._by_name.setdefault(s.name, []).append(s)
            # also index by qualified_name simple leaf
            leaf = s.qualified_name.split(".")[-1]
            if leaf != s.name:
                self._by_name.setdefault(leaf, []).append(s)

    def resolve(self, raw_refs: list[RawRef], language: str) -> list[Edge]:
        edges: list[Edge] = []

        for ref in raw_refs:
            if ref.kind == "calls":
                edges.extend(self._resolve_call(ref))
            elif ref.kind == "imports":
                edges.extend(self._resolve_import(ref, language))
            elif ref.kind == "inherits":
                edges.extend(self._resolve_inherits(ref))

        return edges

    def _resolve_call(self, ref: RawRef) -> list[Edge]:
        candidates = self._by_name.get(ref.ref_text, [])
        if not candidates:
            # Unresolved — keep it with a raw target
            return [Edge(
                source_id=ref.source_id,
                target_id=ref.ref_text,
                kind="calls",
                resolved=False,
            )]
        return [
            Edge(source_id=ref.source_id, target_id=c.id, kind="calls", resolved=True)
            for c in candidates
        ]

    def _resolve_import(self, ref: RawRef, language: str) -> list[Edge]:
        if language == "python":
            names = _parse_python_import(ref.ref_text)
        elif language == "java":
            names = _parse_java_import(ref.ref_text)
        elif language in ("typescript", "tsx"):
            names = _parse_typescript_import(ref.ref_text)
        else:
            return []

        edges: list[Edge] = []
        for name in names:
            # Try exact name match first
            leaf = name.split(".")[-1]
            candidates = self._by_name.get(leaf, [])
            if candidates:
                for c in candidates:
                    edges.append(Edge(
                        source_id=ref.source_id,
                        target_id=c.id,
                        kind="imports",
                        resolved=True,
                    ))
            else:
                edges.append(Edge(
                    source_id=ref.source_id,
                    target_id=name,
                    kind="imports",
                    resolved=False,
                ))
        return edges

    def _resolve_inherits(self, ref: RawRef) -> list[Edge]:
        name = ref.ref_text.split(".")[-1]
        candidates = self._by_name.get(name, [])
        if not candidates:
            return [Edge(
                source_id=ref.source_id,
                target_id=ref.ref_text,
                kind="inherits",
                resolved=False,
            )]
        return [
            Edge(source_id=ref.source_id, target_id=c.id, kind="inherits", resolved=True)
            for c in candidates
        ]


def resolve_all(all_symbols: list[Symbol], all_raw_refs: list[RawRef], language_map: dict[str, str]) -> list[Edge]:
    """
    Resolve all raw references across the codebase.

    language_map: {file_path → language}
    """
    resolver = Resolver(all_symbols)
    edges: list[Edge] = []

    # Group refs by source file language
    source_file: dict[str, str] = {}
    for s in all_symbols:
        source_file[s.id] = s.file_path

    for ref in all_raw_refs:
        file_path = source_file.get(ref.source_id, "")
        if not file_path:
            # module-level import ref — derive file from source_id
            file_path = ref.source_id.split("::")[0]
        language = language_map.get(file_path, "python")

        if ref.kind == "calls":
            edges.extend(resolver._resolve_call(ref))
        elif ref.kind == "imports":
            edges.extend(resolver._resolve_import(ref, language))
        elif ref.kind == "inherits":
            edges.extend(resolver._resolve_inherits(ref))

    # Deduplicate
    seen: set[tuple[str, str, str]] = set()
    unique: list[Edge] = []
    for e in edges:
        key = (e.source_id, e.target_id, e.kind)
        if key not in seen:
            seen.add(key)
            unique.append(e)

    resolved = sum(1 for e in unique if e.resolved)
    log.info(
        "Resolved %d/%d edges (%.0f%%)",
        resolved, len(unique),
        100 * resolved / len(unique) if unique else 0,
    )
    return unique
