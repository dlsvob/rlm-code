"""
Git-based staleness detection.

Tracks the last-indexed commit SHA and diffs against HEAD to find
changed, deleted, and new files since the last index run.
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_META_KEY_COMMIT = "last_indexed_commit"


@dataclass
class ChangeSet:
    changed: list[str]      # modified files (relative paths)
    deleted: list[str]      # deleted files
    added: list[str]        # new files not previously indexed
    is_full_reindex: bool   # True when no prior commit recorded


def get_head_sha(project_root: str) -> str | None:
    """Return the current HEAD commit SHA, or None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def diff_since(project_root: str, since_sha: str) -> tuple[list[str], list[str]]:
    """
    Return (changed_files, deleted_files) between since_sha and HEAD.
    Paths are relative to project_root.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", since_sha, "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("git diff failed: %s", result.stderr.strip())
            return [], []
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("git diff error: %s", e)
        return [], []

    changed: list[str] = []
    deleted: list[str] = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[1]
        path = path.strip()
        if status.startswith("D"):
            deleted.append(path)
        elif status.startswith("R"):
            # Renamed: "R100\told_name\tnew_name"
            rename_parts = line.split("\t")
            if len(rename_parts) == 3:
                deleted.append(rename_parts[1])
                changed.append(rename_parts[2])
        else:
            changed.append(path)

    return changed, deleted


def compute_changeset(
    project_root: str,
    last_commit: str | None,
    indexed_paths: list[str],
) -> ChangeSet:
    """
    Compute what needs re-indexing.

    If last_commit is None, returns a full-reindex changeset.
    indexed_paths: list of file paths currently in the index.
    """
    if last_commit is None:
        return ChangeSet(changed=[], deleted=[], added=[], is_full_reindex=True)

    changed, deleted = diff_since(project_root, last_commit)

    # Files that exist on disk but aren't in the index yet
    indexed_set = set(indexed_paths)
    added = [p for p in changed if p not in indexed_set]
    changed = [p for p in changed if p in indexed_set]

    return ChangeSet(
        changed=changed,
        deleted=deleted,
        added=added,
        is_full_reindex=False,
    )
