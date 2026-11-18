"""Compatibility import for the renamed project runtime preparation command."""

import sys
from scripts import prepare_codecode as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
