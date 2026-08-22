"""Package-level entry for ``python -m src``.

Usage:
    python -m src
    text2sql-server  (after pip install -e .)
"""

from .api.server import run_server

if __name__ == "__main__":
    run_server()
