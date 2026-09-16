"""Syntax compatibility guard for Python 3.11 (Railway target).

Runs two checks:
  1. py_compile on every .py source file — catches SyntaxErrors in general.
  2. Text scan for f-string backslash-in-expression: illegal in Python < 3.12
     (PEP 701 introduced this in 3.12; Railway uses 3.11.9).

These tests are intentionally pessimistic: they flag the pattern even when
the local interpreter would accept it, because Railway won't.
"""
from __future__ import annotations

import py_compile
import re
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_SOURCE_DIRS = ["api", "engine", "residential", "school"]

# ── helpers ──────────────────────────────────────────────────────────────────


def _source_files() -> list[Path]:
    files = []
    for d in _SOURCE_DIRS:
        files.extend(((_ROOT / d).rglob("*.py")))
    return [f for f in files if "__pycache__" not in str(f)]


# ── 1. py_compile: basic SyntaxError guard ────────────────────────────────────


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(_ROOT)))
def test_compiles(path: Path) -> None:
    """Every source file must compile without SyntaxError."""
    with tempfile.NamedTemporaryFile(suffix=".pyc", delete=True) as tmp:
        try:
            py_compile.compile(str(path), cfile=tmp.name, doraise=True)
        except py_compile.PyCompileError as exc:
            pytest.fail(str(exc))


# ── 2. f-string backslash-in-expression (illegal in Python < 3.12) ───────────

# Matches: f"...{  any \' or \" inside the braces  }..."
# This is the pattern `f"{'string with \' escape'}"` — valid in 3.12+, SyntaxError in 3.11.
_FSTR_BACKSLASH = re.compile(r"""f["'][^"'\n]*\{[^}]*\\'[^}]*\}""")


def test_no_fstring_backslash_in_expression() -> None:
    """F-string backslash-in-expression is illegal in Python < 3.12 (PEP 701).
    Railway target is Python 3.11 — flag the pattern before it reaches prod."""
    offenders: list[str] = []
    for py in _source_files():
        try:
            text = py.read_text(errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _FSTR_BACKSLASH.search(line):
                offenders.append(
                    f"{py.relative_to(_ROOT)}:{lineno}: {line.strip()[:120]}"
                )
    assert not offenders, (
        "Backslash inside f-string expression — illegal in Python < 3.12 "
        "(Railway uses 3.11):\n" + "\n".join(offenders)
    )
