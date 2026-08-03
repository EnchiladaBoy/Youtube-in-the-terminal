#!/usr/bin/env python3
"""Build a standalone, single-file yt-ascii executable with PyInstaller.

PyInstaller cannot cross-compile, so run this once on each OS you want a binary
for (Linux, macOS, Windows). Output:

    dist/yt-ascii          (dist/yt-ascii.exe on Windows)

The build bundles numpy, yt-dlp (used as a library) and a static ffmpeg
(via imageio-ffmpeg), so the resulting binary plays video with nothing else
installed. Audio additionally uses ffplay if it is found on PATH.

    pip install pyinstaller imageio-ffmpeg yt-dlp numpy certifi
    python packaging/build.py
"""
import os
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "yt-ascii"
BUILD = ROOT / "build"
DIST = ROOT / "dist"


def main():
    if not SRC.exists():
        sys.exit(f"source not found: {SRC}")
    BUILD.mkdir(exist_ok=True)
    # PyInstaller derives the module name from the script filename; our source
    # has no .py extension, so build from a normal-named copy of it.
    entry = BUILD / "yt_ascii_entry.py"
    shutil.copyfile(SRC, entry)

    args = [
        str(entry),
        "--onefile",
        "--name", "yt-ascii",
        "--console",
        "--clean",
        "--noconfirm",
        # The executable entry is copied under build/, while the importable
        # renderer module stays at the repository root.
        "--paths", str(ROOT),
        # Pull in everything these packages need at runtime (yt-dlp's lazily
        # imported extractors, the bundled ffmpeg binary, CA certificates).
        "--collect-all", "yt_dlp",
        "--collect-all", "imageio_ffmpeg",
        "--collect-all", "certifi",
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "pyinstaller"),
        "--specpath", str(BUILD),
    ]
    print("pyinstaller " + " ".join(args[1:]), flush=True)
    PyInstaller.__main__.run(args)

    exe = DIST / ("yt-ascii.exe" if os.name == "nt" else "yt-ascii")
    if not exe.exists():
        sys.exit(f"build failed: {exe} not found")
    size_mb = exe.stat().st_size / 1e6
    print(f"\nBuilt {exe}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
