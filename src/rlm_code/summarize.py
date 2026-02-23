"""
LLM recursive bottom-up summarization via Claude CLI.

Shells out to `claude -p` (print mode) for non-interactive summary generation.
Bottom-up order: symbols → files → directories.
"""

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .models import Summary, Symbol
from .store import CodeStore

log = logging.getLogger(__name__)

DEFAULT_MODEL = "haiku"
MAX_FILE_CHARS = 100_000  # skip files larger than this


class Summarizer:
    def __init__(
        self,
        store: CodeStore,
        project_root: str,
        model: str = DEFAULT_MODEL,
    ):
        self.store = store
        self.project_root = Path(project_root)
        self.model = model
        self._call_count = 0

    def _call_claude(self, prompt: str) -> str | None:
        """Call claude CLI in print mode. Returns response text or None on failure."""
        try:
            # Clean env: unset CLAUDECODE to allow nested invocation
            env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
            result = subprocess.run(
                [
                    "claude", "-p",
                    "--model", self.model,
                    "--tools", "",
                    "--output-format", "json",
                    "--no-session-persistence",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            if result.returncode != 0:
                log.warning(
                    "claude CLI failed (rc=%d): %s",
                    result.returncode, result.stderr[:200],
                )
                return None

            envelope = json.loads(result.stdout)
            self._call_count += 1

            if envelope.get("is_error"):
                log.warning("claude returned error: %s", envelope.get("result", "")[:200])
                return None

            return envelope.get("result", "")

        except FileNotFoundError:
            log.error("claude CLI not found — install Claude Code first")
            return None
        except subprocess.TimeoutExpired:
            log.warning("claude CLI timed out")
            return None
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("Failed to parse claude response: %s", e)
            return None

    def _read_file(self, rel_path: str) -> str | None:
        """Read a source file from the project."""
        full_path = self.project_root / rel_path
        try:
            return full_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log.warning("Cannot read %s: %s", rel_path, e)
            return None

    def summarize_file(
        self, rel_path: str, symbols: list[Symbol],
    ) -> dict[str, str]:
        """
        Summarize a file and its symbols in a single LLM call.

        Returns a dict mapping target_id → summary_text.
        Keys include the file path (for the file summary) and symbol IDs.
        """
        source = self._read_file(rel_path)
        if source is None:
            return {}

        if len(source) > MAX_FILE_CHARS:
            log.info("Skipping %s (too large: %d chars)", rel_path, len(source))
            return {}

        # Build symbol list for the prompt
        sym_lines = []
        for s in sorted(symbols, key=lambda x: x.start_line):
            sym_lines.append(
                f"  - {s.kind} {s.qualified_name} (L{s.start_line}-{s.end_line}): "
                f"{s.signature}"
            )

        prompt = (
            "Analyze this source file and provide concise summaries.\n\n"
            f"File: {rel_path}\n"
            "Symbols:\n" + "\n".join(sym_lines) + "\n\n"
            f"Source:\n```\n{source}\n```\n\n"
            "Respond with ONLY a JSON object (no markdown fences, no extra text):\n"
            '{"file_summary": "1-2 sentence summary of the file\'s purpose", '
            '"symbols": {"qualified_name": "1-sentence summary", ...}}\n'
            "Include an entry in symbols for each symbol listed above. "
            "Use the exact qualified_name as the key."
        )

        response = self._call_claude(prompt)
        if response is None:
            return {}

        return self._parse_file_response(rel_path, symbols, response)

    def _parse_file_response(
        self, rel_path: str, symbols: list[Symbol], response: str,
    ) -> dict[str, str]:
        """Parse the LLM response into a target_id → summary dict."""
        summaries: dict[str, str] = {}

        try:
            text = response.strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = json.loads(text)

            # File summary
            if "file_summary" in data:
                summaries[rel_path] = data["file_summary"]

            # Symbol summaries — match by qualified name
            sym_map = {s.qualified_name: s.id for s in symbols}
            if "symbols" in data and isinstance(data["symbols"], dict):
                for qname, summary_text in data["symbols"].items():
                    if qname in sym_map:
                        summaries[sym_map[qname]] = str(summary_text)
                    else:
                        # Fuzzy match: try just the method name
                        for s in symbols:
                            if s.name == qname or s.qualified_name.endswith("." + qname):
                                summaries[s.id] = str(summary_text)
                                break

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.warning("Failed to parse summaries for %s: %s", rel_path, e)
            # Fall back: use entire response as a file summary
            if response.strip():
                summaries[rel_path] = response.strip()[:500]

        return summaries

    def summarize_directory(
        self, dir_path: str, file_summaries: dict[str, str],
    ) -> str | None:
        """Summarize a directory using its file summaries."""
        if not file_summaries:
            return None

        entries = []
        for fp, summary in sorted(file_summaries.items()):
            entries.append(f"  - {fp}: {summary}")

        prompt = (
            "Summarize this source code directory based on its files.\n\n"
            f"Directory: {dir_path}\n"
            "Files:\n" + "\n".join(entries) + "\n\n"
            "Provide a 1-3 sentence summary of the directory's purpose and contents. "
            "Respond with ONLY the summary text, no extra formatting."
        )

        response = self._call_claude(prompt)
        return response.strip() if response else None

    def run(self, skip_fresh: bool = True) -> dict:
        """
        Run bottom-up summarization: symbols → files → directories.

        Args:
            skip_fresh: If True, skip targets that already have non-stale summaries.

        Returns:
            Stats dict with counts of summarized symbols, files, directories.
        """
        now = datetime.now(timezone.utc).isoformat()
        stats = {
            "symbols": 0, "files": 0, "directories": 0,
            "skipped": 0, "errors": 0,
        }

        all_files = self.store.all_file_paths()
        total = len(all_files)

        # ── Phase 1: files + their symbols ────────────────────────────────────
        file_summaries: dict[str, str] = {}  # rel_path → summary text

        for i, fp in enumerate(sorted(all_files), 1):
            # Check freshness
            if skip_fresh:
                existing = self.store.get_summary(fp)
                if existing and not existing.is_stale and existing.summary_text:
                    file_summaries[fp] = existing.summary_text
                    stats["skipped"] += 1
                    continue

            log.info("Summarizing [%d/%d]: %s", i, total, fp)
            symbols = self.store.symbols_in_file(fp)
            results = self.summarize_file(fp, symbols)

            if not results:
                stats["errors"] += 1
                continue

            # Store each summary
            for target_id, text in results.items():
                if target_id == fp:
                    kind = "file"
                    file_summaries[fp] = text
                    stats["files"] += 1
                else:
                    kind = "symbol"
                    stats["symbols"] += 1

                self.store.upsert_summary(Summary(
                    target_id=target_id,
                    target_kind=kind,
                    summary_text=text,
                    model=self.model,
                    generated_at=now,
                    is_stale=False,
                ))

        # ── Phase 2: directories (bottom-up) ─────────────────────────────────
        dirs: dict[str, dict[str, str]] = {}
        for fp, summary in file_summaries.items():
            parent = str(Path(fp).parent)
            if parent == ".":
                parent = ""
            dirs.setdefault(parent, {})[fp] = summary

        # Sort deepest-first for bottom-up aggregation
        sorted_dirs = sorted(
            dirs.keys(),
            key=lambda d: d.count("/"),
            reverse=True,
        )

        for dir_path in sorted_dirs:
            if not dir_path:
                continue  # skip project root

            if skip_fresh:
                existing = self.store.get_summary(dir_path)
                if existing and not existing.is_stale and existing.summary_text:
                    stats["skipped"] += 1
                    continue

            log.info("Summarizing directory: %s", dir_path)
            summary = self.summarize_directory(dir_path, dirs[dir_path])

            if summary:
                self.store.upsert_summary(Summary(
                    target_id=dir_path,
                    target_kind="directory",
                    summary_text=summary,
                    model=self.model,
                    generated_at=now,
                    is_stale=False,
                ))
                stats["directories"] += 1

        log.info(
            "Summarization complete: %d symbols, %d files, %d dirs "
            "(%d skipped, %d errors, %d LLM calls)",
            stats["symbols"], stats["files"], stats["directories"],
            stats["skipped"], stats["errors"], self._call_count,
        )
        stats["llm_calls"] = self._call_count
        return stats


def run_summarize(
    project_root: str,
    db_path: str,
    model: str = DEFAULT_MODEL,
    skip_fresh: bool = True,
) -> dict:
    """Top-level entry point for summarization."""
    store = CodeStore(db_path)
    try:
        summarizer = Summarizer(store, project_root, model=model)
        return summarizer.run(skip_fresh=skip_fresh)
    finally:
        store.close()
