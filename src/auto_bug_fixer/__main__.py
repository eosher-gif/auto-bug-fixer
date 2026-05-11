"""Entry point: ``python -m auto_bug_fixer [daemon|index-once|run-once]``."""
from __future__ import annotations

import sys

from auto_bug_fixer.cli import main


if __name__ == "__main__":
    sys.exit(main())
