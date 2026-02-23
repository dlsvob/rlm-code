"""File discovery — walk a project, respect .gitignore, return (path, language) pairs."""

import logging
import subprocess
from pathlib import Path

import pathspec

from .models import IndexConfig

log = logging.getLogger(__name__)

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".java": "java",
    ".ts": "typescript",
    ".tsx": "tsx",
}


def _load_gitignore_spec(root: Path) -> pathspec.PathSpec | None:
    gitignore = root / ".gitignore"
    if gitignore.exists():
        patterns = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    return None


def discover_files(config: IndexConfig) -> list[tuple[str, str]]:
    """
    Return a list of (relative_path, language) for all indexable source files
    under config.project_root.

    Respects .gitignore and config.exclude_dirs.
    Paths are relative to project_root and use forward slashes.
    """
    root = Path(config.project_root).resolve()
    gitignore_spec = _load_gitignore_spec(root)
    exclude_dirs = set(config.exclude_dirs)
    wanted_langs = set(config.languages)

    results: list[tuple[str, str]] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        rel = path.relative_to(root)
        rel_str = rel.as_posix()

        # skip excluded directories (check every part of the path)
        if any(part in exclude_dirs for part in rel.parts):
            continue

        # skip gitignored paths
        if gitignore_spec and gitignore_spec.match_file(rel_str):
            continue

        lang = _EXT_TO_LANG.get(path.suffix.lower())
        if lang is None or lang not in wanted_langs:
            continue

        results.append((rel_str, lang))

    log.info("Discovered %d files under %s", len(results), root)
    return results
