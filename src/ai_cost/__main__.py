"""``python -m ai_cost`` and the zipapp entry point: the process exit status IS ``main()``'s return value."""

from __future__ import annotations

import sys

from . import cli


def run() -> None:
    """Exit with ``cli.main()``'s code."""
    sys.exit(cli.main())


if __name__ == "__main__":
    run()
