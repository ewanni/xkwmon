"""Allow ``python -m xkwmon``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
