"""Stopgap session CLI: python -m daedalus.state show | set-active <name> | pull <name> | drop <name>.

The logic lives in daedalus.core.state; commit 6's /vault and /pull call the same functions.
"""

import sys

from daedalus.core.state import main

if __name__ == "__main__":
    sys.exit(main())
