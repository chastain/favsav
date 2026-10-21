"""Test package for favsav.

The repository root is put on ``sys.path`` so that ``python3 -m unittest discover -s
tests`` works from a checkout without installing anything.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
