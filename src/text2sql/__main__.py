"""Package-level entry for ``python -m text2sql``.

Usage:
    python -m text2sql
    text2sql-server  (after pip install -e .)
"""

from .api.server import run_server

if __name__ == "__main__":
    run_server()
