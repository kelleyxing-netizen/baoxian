"""项目入口：python run.py [chat|demo]"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from qinghe.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
