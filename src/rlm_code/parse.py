"""Tree-sitter parsing with content-hash caching."""

import hashlib
import logging
from pathlib import Path

from tree_sitter import Node, Parser, Tree
from tree_sitter_language_pack import get_language

log = logging.getLogger(__name__)

_PARSERS: dict[str, Parser] = {}


def _get_parser(language: str) -> Parser:
    if language not in _PARSERS:
        lang_obj = get_language(language)
        parser = Parser(lang_obj)
        _PARSERS[language] = parser
    return _PARSERS[language]


def content_hash(source: bytes) -> str:
    return hashlib.sha256(source).hexdigest()


def parse_file(path: str, language: str, project_root: str) -> tuple[Tree, bytes] | None:
    """
    Parse a source file and return (Tree, source_bytes), or None on error.
    """
    full_path = Path(project_root) / path
    try:
        source = full_path.read_bytes()
    except OSError as e:
        log.warning("Cannot read %s: %s", full_path, e)
        return None

    parser = _get_parser(language)
    try:
        tree = parser.parse(source)
        return tree, source
    except Exception as e:
        log.warning("Parse error in %s: %s", path, e)
        return None


def walk_tree(node: Node):
    """Depth-first generator over all nodes in a tree."""
    yield node
    for child in node.children:
        yield from walk_tree(child)
