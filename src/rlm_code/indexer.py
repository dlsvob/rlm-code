"""
Main indexing pipeline — wires discover → parse → extract → resolve → graph → store.
"""

import logging
from pathlib import Path

from .discover import discover_files
from .extract import extract
from .freshness import ChangeSet, compute_changeset, get_head_sha
from .graph import build_graph, compute_metrics, detect_patterns
from .models import FileRecord, IndexConfig, RawRef, Symbol
from .parse import content_hash, parse_file
from .resolve import resolve_all
from .store import CodeStore

log = logging.getLogger(__name__)


def _index_file(
    rel_path: str,
    language: str,
    config: IndexConfig,
    store: CodeStore,
) -> tuple[list[Symbol], list[RawRef]] | None:
    """Parse and extract a single file. Returns (symbols, raw_refs) or None on failure."""
    result = parse_file(rel_path, language, config.project_root)
    if result is None:
        return None

    tree, source = result
    chash = content_hash(source)
    line_count = source.count(b"\n") + 1

    # Remove stale data for this file before re-inserting
    store.delete_edges_for_file(rel_path)
    store.delete_symbols_for_file(rel_path)

    store.upsert_file(FileRecord(
        path=rel_path,
        language=language,
        content_hash=chash,
        line_count=line_count,
        last_indexed="",  # store sets this via now()
    ))

    symbols, raw_refs = extract(rel_path, language, tree, source)

    for s in symbols:
        store.upsert_symbol(s)

    store.mark_stale(rel_path)  # invalidate any existing summary

    return symbols, raw_refs


def run_index(config: IndexConfig, force: bool = False) -> dict:
    """
    Run the full indexing pipeline for a project.

    Returns a stats dict.
    """
    db_path = config.db_path
    store = CodeStore(db_path)

    try:
        return _run_index_inner(config, store, force)
    finally:
        store.close()


def _run_index_inner(config: IndexConfig, store: CodeStore, force: bool) -> dict:
    root = config.project_root

    # Determine what needs indexing
    last_commit = store.get_meta("last_indexed_commit")
    current_commit = get_head_sha(root)

    if force or last_commit is None:
        log.info("Full index of %s", root)
        files_to_index = discover_files(config)
        for indexed_path in store.all_file_paths():
            store.delete_file(indexed_path)
    else:
        indexed_paths = store.all_file_paths()
        changeset = compute_changeset(root, last_commit, indexed_paths)

        if changeset.is_full_reindex:
            log.info("Full reindex (no prior commit recorded)")
            files_to_index = discover_files(config)
        else:
            # Delete removed files
            for path in changeset.deleted:
                log.debug("Deleting %s from index", path)
                store.delete_file(path)

            # Discover all files, then figure out what needs processing
            all_files = {p: lang for p, lang in discover_files(config)}
            to_process = set(changeset.changed) | set(changeset.added)
            # Also include newly discovered files not yet in the index
            # (handles untracked files not visible in git diff)
            indexed_set = set(indexed_paths)
            for p in all_files:
                if p not in indexed_set:
                    to_process.add(p)
            files_to_index = [
                (p, lang) for p, lang in all_files.items() if p in to_process
            ]
            log.info(
                "Incremental: %d changed, %d added, %d deleted",
                len(changeset.changed), len(changeset.added), len(changeset.deleted),
            )

    if not files_to_index:
        log.info("Nothing to index — already up to date")
        return store.stats()

    # Index each file
    all_symbols: list[Symbol] = []
    all_raw_refs: list[RawRef] = []
    language_map: dict[str, str] = {}
    errors = 0

    for rel_path, language in files_to_index:
        language_map[rel_path] = language
        result = _index_file(rel_path, language, config, store)
        if result is None:
            errors += 1
            continue
        symbols, raw_refs = result
        all_symbols.extend(symbols)
        all_raw_refs.extend(raw_refs)

    log.info(
        "Extracted %d symbols, %d raw refs from %d files (%d errors)",
        len(all_symbols), len(all_raw_refs), len(files_to_index), errors,
    )

    # For incremental runs, load existing symbols for resolution context
    if not force and last_commit is not None:
        existing_symbols = []
        for path in store.all_file_paths():
            existing_symbols.extend(store.symbols_in_file(path))
        # Merge: existing + newly extracted (newly extracted already re-inserted)
        existing_ids = {s.id for s in all_symbols}
        for s in existing_symbols:
            if s.id not in existing_ids:
                all_symbols.append(s)
                language_map[s.file_path] = language_map.get(s.file_path, "python")

    # Resolve cross-file references
    edges = resolve_all(all_symbols, all_raw_refs, language_map)
    store.add_edges(edges)

    log.info("Stored %d edges", len(edges))

    # Rebuild graph and compute metrics over the full symbol set
    all_stored_symbols = []
    for path in store.all_file_paths():
        all_stored_symbols.extend(store.symbols_in_file(path))

    all_edges = store.all_edges()
    g = build_graph(all_stored_symbols, all_edges)
    metrics = compute_metrics(g)
    store.bulk_upsert_metrics(metrics)

    log.info("Computed metrics for %d symbols", len(metrics))

    # Update last-indexed commit
    if current_commit:
        store.set_meta("last_indexed_commit", current_commit)

    stats = store.stats()
    stats["errors"] = errors
    return stats
