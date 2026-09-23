"""The distribution's identity: the PyPI name is ``ai-costs`` (``ai-cost`` belongs to another project) and the
version the code reports is the version the package metadata declares. Both live in ``pyproject.toml``, which a
checkout has and an installed package does not: outside a checkout there is nothing to compare and the test passes.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import __version__

DISTRIBUTION = "ai-costs"
ROOT = Path(__file__).resolve().parents[3]


def test_the_distribution_is_ai_costs_at_the_code_version() -> None:
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.is_file():
        return
    text = pyproject.read_text(encoding="utf-8")
    assert re.search(r'^name = "ai-costs"$', text, re.M), "the PyPI distribution is ai-costs"
    assert re.search(
        rf'^version = "{re.escape(__version__)}"$', text, re.M
    ), "pyproject version == __version__"
    for doc in ("README.md", "docs/SETUP.md", "standalone/SKILL.md"):
        assert f"pipx install {DISTRIBUTION}" in (ROOT / doc).read_text(encoding="utf-8"), doc
