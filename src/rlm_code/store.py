"""DuckDB schema and CodeStore — all persistence for rlm-code.

DuckDB connections are NOT thread-safe: concurrent queries from different
threads corrupt internal state ("unsuccessful or closed pending query result").
Every public method on CodeStore holds a threading.Lock for the full duration
of execute-through-fetch so callers (e.g. uvicorn's thread pool in the viz
server) don't need to coordinate themselves.
"""

import json
import logging
import threading
from pathlib import Path

import duckdb

from .models import Edge, FileRecord, Summary, Symbol, SymbolMetrics

log = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key     VARCHAR PRIMARY KEY,
    value   VARCHAR
);

CREATE TABLE IF NOT EXISTS files (
    path            VARCHAR PRIMARY KEY,
    language        VARCHAR NOT NULL,
    content_hash    VARCHAR NOT NULL,
    line_count      INTEGER,
    last_indexed    TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS symbols (
    id              VARCHAR PRIMARY KEY,
    file_path       VARCHAR NOT NULL,
    name            VARCHAR NOT NULL,
    qualified_name  VARCHAR NOT NULL,
    kind            VARCHAR NOT NULL,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    signature       VARCHAR
);

CREATE TABLE IF NOT EXISTS edges (
    source_id   VARCHAR NOT NULL,
    target_id   VARCHAR NOT NULL,
    kind        VARCHAR NOT NULL,
    resolved    BOOLEAN DEFAULT false,
    PRIMARY KEY (source_id, target_id, kind)
);

CREATE TABLE IF NOT EXISTS metrics (
    symbol_id   VARCHAR PRIMARY KEY,
    in_degree   INTEGER DEFAULT 0,
    out_degree  INTEGER DEFAULT 0,
    betweenness DOUBLE DEFAULT 0.0,
    pagerank    DOUBLE DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS summaries (
    target_id       VARCHAR PRIMARY KEY,
    target_kind     VARCHAR NOT NULL,
    summary_text    VARCHAR,
    model           VARCHAR,
    generated_at    TIMESTAMP,
    is_stale        BOOLEAN DEFAULT true
);

CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_kind   ON edges(kind);
"""


class CodeStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # DuckDB connections are NOT thread-safe — concurrent queries from
        # different threads corrupt internal state.  The lock serializes all
        # access so callers don't need to coordinate themselves.
        self._lock = threading.Lock()
        self._con = duckdb.connect(db_path)
        self._con.execute("PRAGMA threads=4")
        for stmt in _DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self._con.execute(stmt)
        log.debug("CodeStore opened: %s", db_path)

    def close(self) -> None:
        self._con.close()

    # ── meta ────────────────────────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._con.execute(
                "SELECT value FROM meta WHERE key = ?", [key]
            ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value]
            )

    # ── files ────────────────────────────────────────────────────────────────

    def upsert_file(self, f: FileRecord) -> None:
        with self._lock:
            self._con.execute(
                """
                INSERT INTO files (path, language, content_hash, line_count, last_indexed)
                VALUES (?, ?, ?, ?, now())
                ON CONFLICT (path) DO UPDATE SET
                    language     = excluded.language,
                    content_hash = excluded.content_hash,
                    line_count   = excluded.line_count,
                    last_indexed = now()
                """,
                [f.path, f.language, f.content_hash, f.line_count],
            )

    def get_file_hash(self, path: str) -> str | None:
        with self._lock:
            row = self._con.execute(
                "SELECT content_hash FROM files WHERE path = ?", [path]
            ).fetchone()
        return row[0] if row else None

    def delete_file(self, path: str) -> None:
        """Remove a file and all its symbols/edges from the index."""
        with self._lock:
            symbol_ids = [
                r[0] for r in self._con.execute(
                    "SELECT id FROM symbols WHERE file_path = ?", [path]
                ).fetchall()
            ]
            if symbol_ids:
                placeholders = ", ".join("?" * len(symbol_ids))
                self._con.execute(
                    f"DELETE FROM edges WHERE source_id IN ({placeholders})", symbol_ids
                )
                self._con.execute(
                    f"DELETE FROM metrics WHERE symbol_id IN ({placeholders})", symbol_ids
                )
                self._con.execute(
                    f"DELETE FROM symbols WHERE id IN ({placeholders})", symbol_ids
                )
            self._con.execute("DELETE FROM files WHERE path = ?", [path])

    def all_file_paths(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._con.execute("SELECT path FROM files").fetchall()]

    # ── symbols ──────────────────────────────────────────────────────────────

    def upsert_symbol(self, s: Symbol) -> None:
        with self._lock:
            self._con.execute(
                """
                INSERT INTO symbols
                    (id, file_path, name, qualified_name, kind, start_line, end_line, signature)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    file_path      = excluded.file_path,
                    name           = excluded.name,
                    qualified_name = excluded.qualified_name,
                    kind           = excluded.kind,
                    start_line     = excluded.start_line,
                    end_line       = excluded.end_line,
                    signature      = excluded.signature
                """,
                [s.id, s.file_path, s.name, s.qualified_name,
                 s.kind, s.start_line, s.end_line, s.signature],
            )

    def get_symbol(self, symbol_id: str) -> Symbol | None:
        with self._lock:
            row = self._con.execute(
                "SELECT id, file_path, name, qualified_name, kind, "
                "start_line, end_line, signature FROM symbols WHERE id = ?",
                [symbol_id],
            ).fetchone()
        if row:
            return Symbol(*row)
        return None

    def find_symbols_by_name(self, name: str) -> list[Symbol]:
        with self._lock:
            rows = self._con.execute(
                "SELECT id, file_path, name, qualified_name, kind, "
                "start_line, end_line, signature FROM symbols WHERE name = ?",
                [name],
            ).fetchall()
        return [Symbol(*r) for r in rows]

    def symbols_in_file(self, file_path: str) -> list[Symbol]:
        with self._lock:
            rows = self._con.execute(
                "SELECT id, file_path, name, qualified_name, kind, "
                "start_line, end_line, signature FROM symbols WHERE file_path = ?",
                [file_path],
            ).fetchall()
        return [Symbol(*r) for r in rows]

    def delete_symbols_for_file(self, path: str) -> None:
        with self._lock:
            self._con.execute("DELETE FROM symbols WHERE file_path = ?", [path])

    # ── edges ────────────────────────────────────────────────────────────────

    def add_edge(self, e: Edge) -> None:
        with self._lock:
            self._con.execute(
                """
                INSERT INTO edges (source_id, target_id, kind, resolved)
                VALUES (?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                [e.source_id, e.target_id, e.kind, e.resolved],
            )

    def add_edges(self, edges: list[Edge]) -> None:
        if not edges:
            return
        rows = [(e.source_id, e.target_id, e.kind, e.resolved) for e in edges]
        with self._lock:
            self._con.executemany(
                "INSERT INTO edges (source_id, target_id, kind, resolved) "
                "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                rows,
            )

    def delete_edges_for_file(self, path: str) -> None:
        with self._lock:
            symbol_ids = [r[0] for r in self._con.execute(
                "SELECT id FROM symbols WHERE file_path = ?", [path]
            ).fetchall()]
            if symbol_ids:
                placeholders = ", ".join("?" * len(symbol_ids))
                self._con.execute(
                    f"DELETE FROM edges WHERE source_id IN ({placeholders})", symbol_ids
                )

    def get_callers(self, symbol_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self._con.execute(
                "SELECT source_id FROM edges WHERE target_id = ? AND kind = 'calls'",
                [symbol_id],
            ).fetchall()]

    def get_callees(self, symbol_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self._con.execute(
                "SELECT target_id FROM edges WHERE source_id = ? AND kind = 'calls' AND resolved = true",
                [symbol_id],
            ).fetchall()]

    def all_edges(self) -> list[Edge]:
        with self._lock:
            rows = self._con.execute(
                "SELECT source_id, target_id, kind, resolved FROM edges"
            ).fetchall()
        return [Edge(*r) for r in rows]

    # ── metrics ──────────────────────────────────────────────────────────────

    def upsert_metrics(self, m: SymbolMetrics) -> None:
        with self._lock:
            self._con.execute(
                """
                INSERT INTO metrics (symbol_id, in_degree, out_degree, betweenness, pagerank)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (symbol_id) DO UPDATE SET
                    in_degree   = excluded.in_degree,
                    out_degree  = excluded.out_degree,
                    betweenness = excluded.betweenness,
                    pagerank    = excluded.pagerank
                """,
                [m.symbol_id, m.in_degree, m.out_degree, m.betweenness, m.pagerank],
            )

    def bulk_upsert_metrics(self, metrics: list[SymbolMetrics]) -> None:
        if not metrics:
            return
        rows = [(m.symbol_id, m.in_degree, m.out_degree, m.betweenness, m.pagerank)
                for m in metrics]
        with self._lock:
            self._con.executemany(
                """
                INSERT INTO metrics (symbol_id, in_degree, out_degree, betweenness, pagerank)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (symbol_id) DO UPDATE SET
                    in_degree   = excluded.in_degree,
                    out_degree  = excluded.out_degree,
                    betweenness = excluded.betweenness,
                    pagerank    = excluded.pagerank
                """,
                rows,
            )

    def get_metrics(self, symbol_id: str) -> SymbolMetrics | None:
        with self._lock:
            row = self._con.execute(
                "SELECT symbol_id, in_degree, out_degree, betweenness, pagerank "
                "FROM metrics WHERE symbol_id = ?",
                [symbol_id],
            ).fetchone()
        return SymbolMetrics(*row) if row else None

    def top_by_pagerank(self, n: int = 20) -> list[tuple[str, float]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT symbol_id, pagerank FROM metrics ORDER BY pagerank DESC LIMIT ?", [n]
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ── summaries ────────────────────────────────────────────────────────────

    def upsert_summary(self, s: Summary) -> None:
        with self._lock:
            self._con.execute(
                """
                INSERT INTO summaries
                    (target_id, target_kind, summary_text, model, generated_at, is_stale)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (target_id) DO UPDATE SET
                    target_kind  = excluded.target_kind,
                    summary_text = excluded.summary_text,
                    model        = excluded.model,
                    generated_at = excluded.generated_at,
                    is_stale     = excluded.is_stale
                """,
                [s.target_id, s.target_kind, s.summary_text,
                 s.model, s.generated_at, s.is_stale],
            )

    def get_summary(self, target_id: str) -> Summary | None:
        with self._lock:
            row = self._con.execute(
                "SELECT target_id, target_kind, summary_text, model, generated_at, is_stale "
                "FROM summaries WHERE target_id = ?",
                [target_id],
            ).fetchone()
        return Summary(*row) if row else None

    def mark_stale(self, target_id: str) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE summaries SET is_stale = true WHERE target_id = ?", [target_id]
            )

    # ── bulk queries (used by viz API for efficient full-dataset loads) ────

    def all_symbols(self) -> list[Symbol]:
        """Return every symbol in one query — used by the viz server to build
        the full graph and tree without N+1 queries per file."""
        with self._lock:
            rows = self._con.execute(
                "SELECT id, file_path, name, qualified_name, kind, "
                "start_line, end_line, signature FROM symbols"
            ).fetchall()
        return [Symbol(*r) for r in rows]

    def all_metrics(self) -> list[SymbolMetrics]:
        """Return every metrics row at once — avoids per-symbol lookups
        when building the graph node list."""
        with self._lock:
            rows = self._con.execute(
                "SELECT symbol_id, in_degree, out_degree, betweenness, pagerank "
                "FROM metrics"
            ).fetchall()
        return [SymbolMetrics(*r) for r in rows]

    def all_summaries(self) -> list[Summary]:
        """Return all summaries (symbols, files, directories) in one query."""
        with self._lock:
            rows = self._con.execute(
                "SELECT target_id, target_kind, summary_text, model, "
                "generated_at, is_stale FROM summaries"
            ).fetchall()
        return [Summary(*r) for r in rows]

    def all_files(self) -> list[FileRecord]:
        """Return every file record — used for tree building and file detail views."""
        with self._lock:
            rows = self._con.execute(
                "SELECT path, language, content_hash, line_count, last_indexed FROM files"
            ).fetchall()
        return [FileRecord(*r) for r in rows]

    def search_symbols(self, query: str, limit: int = 20) -> list[Symbol]:
        """Case-insensitive search on symbol name and qualified_name.
        Used by the search endpoint for typeahead results."""
        pattern = f"%{query}%"
        with self._lock:
            rows = self._con.execute(
                "SELECT id, file_path, name, qualified_name, kind, "
                "start_line, end_line, signature FROM symbols "
                "WHERE name ILIKE ? OR qualified_name ILIKE ? "
                "LIMIT ?",
                [pattern, pattern, limit],
            ).fetchall()
        return [Symbol(*r) for r in rows]

    # ── stats ────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            counts = {}
            for table in ("files", "symbols", "edges", "metrics", "summaries"):
                row = self._con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                counts[table] = row[0] if row else 0

            lang_rows = self._con.execute(
                "SELECT language, COUNT(*) FROM files GROUP BY language"
            ).fetchall()
            counts["by_language"] = {r[0]: r[1] for r in lang_rows}

            kind_rows = self._con.execute(
                "SELECT kind, COUNT(*) FROM symbols GROUP BY kind"
            ).fetchall()
            counts["by_kind"] = {r[0]: r[1] for r in kind_rows}

        return counts
