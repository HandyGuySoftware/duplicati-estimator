"""Make the top-level profiler/calc scripts importable from the tests directory."""
import os
import sys

# The modules live in the project root (one level up from tests/).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
