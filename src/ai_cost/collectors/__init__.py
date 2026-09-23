"""One collector per source; each returns a ``Collected`` (rows, work items, skipped files) for a window.

``BUILTIN`` lists the sources every report runs through the plugin protocol; the Claude transcripts are read first
by the report itself because they decide the window, and the live GitHub query runs only when asked for.
"""

from __future__ import annotations

from ..plugins import Source
from .claude import collect_claude, find_session_files
from .codex import CodexSource, collect_codex
from .gemini_cli import GeminiCliSource, collect_gemini_cli
from .github import collect_github
from .grok_build import GrokBuildSource, collect_grok_build
from .usage_log import UsageLogSource, collect_usage_log

BUILTIN: tuple[Source, ...] = (CodexSource(), GeminiCliSource(), GrokBuildSource(), UsageLogSource())

__all__ = [
    "BUILTIN",
    "collect_claude",
    "collect_codex",
    "collect_gemini_cli",
    "collect_github",
    "collect_grok_build",
    "collect_usage_log",
    "find_session_files",
]
