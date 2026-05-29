"""Package-level entry — 启动 Text2SQL 服务.

Usage:
    python -m src
    text2sql-server  (after pip install -e .)
"""

import sys

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.services.server import run_server

if __name__ == "__main__":
    run_server()
