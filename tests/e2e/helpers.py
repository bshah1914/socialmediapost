"""Helpers for the end-to-end tests."""

import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADMIN = ("admin", "Admin@12345")


def db(server):
    """The app's own database (to set up a story the way the scanner would)."""
    return sqlite3.connect(server["data"] / "technews.db")


def make_clip(path: Path, size="640x360", seconds=3) -> Path:
    sys.path.insert(0, str(ROOT))
    from technews import motion

    subprocess.run([motion.ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30", "-f", "lavfi", "-i",
                    "sine=frequency=440", "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
                   check=True, capture_output=True)  # fmt: skip
    return path
