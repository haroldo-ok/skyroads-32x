#!/usr/bin/env python3
"""Structural/regression tests for the OPL2-to-Genesis VGM conversion."""
from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from opl2_to_vgm import (OPL_FNUM_HI, OPL_FNUM_LO, YM2612_CLOCK,
                         YM_CHANNEL_REG, YM_MUSIC_TL_BIAS, convert_songs,
                         load_muzax, opl_note_hz, ym_note_hz,
                         ym_pitch)  # noqa: E402


def parse_stream(vgm: bytes) -> dict[str, int]:
    data_rel = struct.unpack_from("<I", vgm, 0x34)[0]
    pos = 0x34 + data_rel if data_rel else 0x40
    stats = {"ym0": 0, "ym1": 0, "psg": 0, "wait": 0,
             "key_on": 0, "end": 0}
    while pos < len(vgm):
        command = vgm[pos]
        pos += 1
        if command == 0x50:
            stats["psg"] += 1
            pos += 1
        elif command in (0x52, 0x53):
            stats["ym0" if command == 0x52 else "ym1"] += 1
            if pos + 1 < len(vgm) and vgm[pos] == 0x28 and vgm[pos + 1] & 0xF0:
                stats["key_on"] += 1
            pos += 2
        elif command == 0x61:
            stats["wait"] += 1
            pos += 2
        elif command in (0x62, 0x63) or 0x70 <= command <= 0x7F:
            stats["wait"] += 1
        elif command == 0x66:
            stats["end"] += 1
            break
        else:
            raise AssertionError(f"unsupported VGM command 0x{command:02x}")
    assert pos <= len(vgm), "truncated VGM command"
    return stats


def main() -> int:
    vgms = convert_songs(load_muzax(ROOT / "MUZAX.LZS"))
    assert YM_MUSIC_TL_BIAS == 8  # ~6 dB headroom for normalized PWM effects
    assert len(vgms) == 14

    # Pitch: the DOS F-number table spans most of the audible range, and the
    # YM2612 must place every note within a couple of cents of the OPL2's
    # frequency (the old converter was a factor of four flat).
    for note in range(0, 88):
        dos_hz = opl_note_hz(note)
        block, fnum = ym_pitch(note)
        assert 0 <= block <= 7 and 0 < fnum < 2048, (note, block, fnum)
        error = abs(1200 * math.log2(ym_note_hz(block, fnum) / dos_hz))
        assert error <= 3.0, (note, dos_hz, block, fnum, error)

    # FM operator slots: OPL's two operators land on YM OP1 (0x00) and OP2
    # (0x08).  Muting the wrong slot silently kills the carrier (algorithm 4
    # routes OP1 -> OP2), which is what made the first conversion sound thin.
    assert YM_CHANNEL_REG == (0, 1, 2, 0, 1, 2)
    vgm = vgms[1]
    carrier_levels = []
    pos = 0x40
    while pos < len(vgm):
        command = vgm[pos]
        if command in (0x52, 0x53):
            register, value = vgm[pos + 1], vgm[pos + 2]
            if command == 0x52 and register in (0x48, 0x49, 0x4A):
                carrier_levels.append(value)
            pos += 3
        elif command == 0x50:
            pos += 2
        elif command in (0x61, 0x62, 0x63) or 0x70 <= command <= 0x7F:
            pos += 2 if command == 0x61 else 3 - 2
        elif command == 0x66:
            break
        else:
            pos += 1
    assert carrier_levels and min(carrier_levels) < 0x40, carrier_levels[:8]
    total = 0
    for number, vgm in enumerate(vgms):
        total += len(vgm) + (len(vgm) & 1)
        assert vgm[:4] == b"Vgm "
        assert struct.unpack_from("<I", vgm, 0x04)[0] + 4 == len(vgm)
        assert struct.unpack_from("<I", vgm, 0x08)[0] >= 0x150
        assert struct.unpack_from("<I", vgm, 0x2C)[0] == YM2612_CLOCK
        loop_relative = struct.unpack_from("<I", vgm, 0x1C)[0]
        assert loop_relative, f"track {number} has no VGM loop"
        assert 0x1C + loop_relative < len(vgm)
        stats = parse_stream(vgm)
        assert stats["ym0"] > 100, (number, stats)
        assert stats["ym1"] > 100, (number, stats)
        assert stats["key_on"] > 20, (number, stats)
        assert stats["psg"] > 4, (number, stats)
        assert stats["wait"] > 10, (number, stats)
        assert stats["end"] == 1, (number, stats)

        generated = (ROOT / "src/platform/32x/generated/32x/vgm" /
                     f"skyroads-{number:02d}.vgm")
        if generated.exists():
            assert generated.read_bytes() == vgm, f"stale generated track {number}"

    # 68000's fixed 32X ROM window is 512 KiB.  Keep ample room for startup,
    # the VGM player, lookup table, and initialized Work RAM image.
    assert total < 448 * 1024, f"VGM blob is too large for fixed ROM: {total}"
    print(f"VGM conversion OK: {len(vgms)} looping tracks, {total:,} aligned bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
