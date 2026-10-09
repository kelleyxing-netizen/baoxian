"""测试配置：把 src/ 加入导入路径，使 unittest 与 pytest 都能直接运行。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
