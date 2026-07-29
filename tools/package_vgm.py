#!/usr/bin/env python3
"""Create a deterministic archive of the generated Genesis VGM tracks."""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

README = """SkyRoads — Sega Genesis VGM conversion

These 14 standard VGM 1.50 tracks were converted from the original SkyRoads
OPL2/MUZAX score by tools/opl2_to_vgm.py.

- Six melodic voices use the Yamaha YM2612 FM chip.
- OPL rhythm voices use the Genesis PSG so no Yamaha melody voice is lost.
- Tracks retain the original note, instrument, volume, timing, and loop events.
- Audible FM carriers reserve about 6 dB for the 32X PWM effect path.
- YM2612 clock: 7,670,454 Hz; VGM timing base: 44,100 Hz.

Track 00 is the menu score. Gameplay selects tracks 02 through 13.
SkyRoads' original music and copyright remain with Bluemoon Interactive.
"""


def add_bytes(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data, compresslevel=9)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    tracks = sorted(args.input.glob("skyroads-*.vgm"))
    if len(tracks) != 14:
        raise SystemExit(f"expected 14 VGM files in {args.input}, got {len(tracks)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w") as archive:
        add_bytes(archive, "README.txt", README.encode())
        for track in tracks:
            add_bytes(archive, track.name, track.read_bytes())
    print(f"Packaged {len(tracks)} VGM tracks in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
