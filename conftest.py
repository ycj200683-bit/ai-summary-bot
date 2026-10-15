"""
让 pytest 无论如何都能 import 到项目根目录的 main.py。

为什么需要它：
  1. pytest.ini 里的 pythonpath = . 已经能解决，这里是双保险
  2. 更重要的：pytest 会自动把「最上层 conftest.py 所在的目录」加进 sys.path，
     所以你从任何目录敲 pytest，都能正确 import main

⚠️ 注意：本文件【只对 pytest 生效】。
   直接 `python tests/test_main.py` 是"当脚本运行"，Python 会把 sys.path[0]
   设成脚本所在目录（tests/），那里没有 main.py —— 必然 ModuleNotFoundError。
   测试文件的正确跑法永远是 pytest，不是 ▶。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
