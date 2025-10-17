# Ensure the project root is on sys.path for test imports
import sys
from pathlib import Path

ROOT = str(Path(__file__).parents[1].absolute())
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
