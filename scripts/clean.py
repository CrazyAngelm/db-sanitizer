"""Remove local runtime artifacts without touching source files or .env."""

from __future__ import annotations

import shutil
from pathlib import Path

for relative_path in (".runs", "perf-results"):
    shutil.rmtree(Path(relative_path), ignore_errors=True)
