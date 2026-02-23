"""Core data structures for rlm-code."""

from dataclasses import dataclass, field


@dataclass
class FileRecord:
    path: str                   # relative to project root
    language: str               # "python" | "java"
    content_hash: str           # SHA256 of file bytes
    line_count: int
    last_indexed: str           # ISO 8601 timestamp


@dataclass
class Symbol:
    id: str                     # "{rel_path}::{qualified_name}"
    file_path: str              # relative to project root
    name: str                   # simple name, e.g. "normalize_gene"
    qualified_name: str         # dotted path, e.g. "MyClass.normalize_gene"
    kind: str                   # "function" | "class" | "method"
    start_line: int
    end_line: int
    signature: str              # "def normalize_gene(name: str) -> str"


@dataclass
class RawRef:
    """An unresolved reference extracted from source — call or import."""
    source_id: str              # Symbol.id that contains this reference
    ref_text: str               # raw text: "normalize_gene" or "from extract import normalize_gene"
    kind: str                   # "calls" | "imports" | "inherits"


@dataclass
class Edge:
    source_id: str              # Symbol.id
    target_id: str              # Symbol.id (resolved) or raw ref text (unresolved)
    kind: str                   # "calls" | "imports" | "inherits"
    resolved: bool              # True when target_id is a known Symbol.id


@dataclass
class SymbolMetrics:
    symbol_id: str
    in_degree: int              # callers / importers
    out_degree: int             # callees / imports
    betweenness: float          # bridge/bottleneck score (0.0–1.0)
    pagerank: float             # importance score


@dataclass
class PatternReport:
    god_objects: list[str]      # Symbol IDs with very high in+out degree
    orphans: list[str]          # Symbol IDs with zero in+out degree
    cycles: list[list[str]]     # each inner list is a cycle of Symbol IDs
    hub_files: list[str]        # file paths with most total edges


@dataclass
class Summary:
    target_id: str              # Symbol.id, file path, or directory path
    target_kind: str            # "symbol" | "file" | "directory"
    summary_text: str
    model: str                  # LLM model used
    generated_at: str           # ISO 8601 timestamp
    is_stale: bool


@dataclass
class IndexConfig:
    project_root: str
    db_path: str                # path to bmdx.duckdb
    languages: list[str] = field(default_factory=lambda: ["python", "java"])
    exclude_dirs: list[str] = field(default_factory=lambda: [
        ".git", "__pycache__", ".venv", "venv", "node_modules",
        "target", "build", "dist", ".mypy_cache", ".pytest_cache",
    ])
