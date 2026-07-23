"""知识训练入口.

Usage:
    python -m src.train
    python src/train.py
    text2sql-train  (after pip install -e .)
"""

import sys

from pathlib import Path

from src.services.training import train_knowledge

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if __name__ == "__main__":
    train_knowledge()
