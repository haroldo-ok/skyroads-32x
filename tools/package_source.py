#!/usr/bin/env python3
"""Create the redistributable source archive without SkyRoads game data."""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

PREFIX = "SkyRoads32X-source/"
ROOT_FILES = {".gitignore", "CMakeLists.txt", "LICENSE", "Makefile.32x", "README.md"}
ROOT_DIRS = {"cmake", "docs", "re", "scripts", "src", "tests", "tools"}
EXCLUDED_PARTS = {"generated", "__pycache__", ".git"}
EXCLUDED_SUFFIXES = {".o", ".elf", ".32x", ".bin", ".pyc", ".zip", ".sha256"}

PACKAGE_README = """SkyRoads 32X source package

This archive contains the portable engine, Sega 32X platform code, build-time
OPL2-to-YM2612 VGM converter/player, tests, documentation, and build files.

Bluemoon Interactive's original SkyRoads game data is freeware but is not
redistributed here. Before building, place the original *.LZS, *.DAT, *.SND,
and *.REC files in the project root. See docs/32X.md for the required 32XDK
20220418 toolchain and complete build/emulator-test commands.

Build:
  make -f Makefile.32x -j release

Outputs:
  release/SkyRoads32X.32x       playable 32X ROM
  release/SkyRoads32X-vgm.zip   14 standard Genesis VGM tracks
  release/SkyRoads32X-source.zip this source package
"""


def include(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if not relative.parts or any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if len(relative.parts) == 1:
        return relative.name in ROOT_FILES
    if relative.parts[0] not in ROOT_DIRS:
        return False
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    return path.is_file() and not path.is_symlink()


def add_bytes(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data, compresslevel=9)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    paths = sorted(path for path in root.rglob("*") if include(path, root))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w") as archive:
        add_bytes(archive, PREFIX + "SOURCE_PACKAGE_README.txt",
                  PACKAGE_README.encode())
        for path in paths:
            add_bytes(archive, PREFIX + path.relative_to(root).as_posix(),
                      path.read_bytes())
    print(f"Packaged {len(paths) + 1} source entries in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
