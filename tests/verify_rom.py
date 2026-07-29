#!/usr/bin/env python3
"""Static point checks for a built Sega 32X ROM and its SH-2 ELF."""
from __future__ import annotations

import struct
import sys
from pathlib import Path


def fail(msg: str) -> None:
    raise SystemExit(f"ROM VERIFY FAILED: {msg}")


def elf_sections(path: Path) -> dict[str, tuple[int, int, int]]:
    d = path.read_bytes()
    if d[:4] != b"\x7fELF" or d[4] != 1:
        fail("SH-2 output is not ELF32")
    endian = ">" if d[5] == 2 else "<"
    e_shoff = struct.unpack_from(endian + "I", d, 32)[0]
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(endian + "HHH", d, 46)
    headers = [struct.unpack_from(endian + "10I", d, e_shoff + i * e_shentsize)
               for i in range(e_shnum)]
    shstr = headers[e_shstrndx]
    names = d[shstr[4]:shstr[4] + shstr[5]]
    out = {}
    for h in headers:
        end = names.find(b"\0", h[0])
        name = names[h[0]:end].decode("ascii", "replace") if end >= 0 else ""
        out[name] = (h[3], h[4], h[5])  # address, file offset, size
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} ROM.32x ROM.elf", file=sys.stderr)
        return 2
    rom_path, elf_path = map(Path, sys.argv[1:])
    rom = rom_path.read_bytes()

    if len(rom) < 1_000_000 or len(rom) > 4 * 1024 * 1024:
        fail(f"implausible cartridge size: {len(rom)}")
    if len(rom) % 8192:
        fail("ROM is not padded to an 8 KiB cartridge boundary")
    if not rom[0x100:0x120].startswith(b"SEGA 32X"):
        fail("Genesis header does not identify SEGA 32X")
    if b"SKYROADS 32X" not in rom[0x100:0x190]:
        fail("SkyRoads title missing from Genesis header")
    if rom[0x3C0:0x3D0] != b"SKYROADS 32X    ":
        fail("MARS module header missing")

    source, dest, data_size, master, slave, mvbr, svbr = struct.unpack_from(
        ">7I", rom, 0x3D4)
    if not (0x1000 < source < len(rom)):
        fail(f"bad SH-2 data source offset: {source:#x}")
    if dest != 0:
        fail(f"bad SH-2 SDRAM destination: {dest:#x}")
    if not (0x100 <= data_size < 0x3F000):
        fail(f"bad SH-2 initialized-data size: {data_size:#x}")
    if (master, slave, mvbr, svbr) != (0x06000240, 0x06000244,
                                       0x06000000, 0x06000120):
        fail("bad SH-2 entry points/vector bases")

    stored_end = struct.unpack_from(">I", rom, 0x1A4)[0]
    if stored_end != len(rom) - 1:
        fail(f"header ROM end {stored_end:#x} != actual {len(rom)-1:#x}")
    stored_sum = struct.unpack_from(">H", rom, 0x18E)[0]
    calc_sum = sum(struct.unpack_from(">" + "H" * ((len(rom) - 0x200) // 2),
                                      rom, 0x200)) & 0xFFFF
    if stored_sum != calc_sum:
        fail(f"checksum mismatch: header {stored_sum:04x}, calculated {calc_sum:04x}")

    # These strings are in separate engine/UI translation units. Their
    # presence catches accidental --gc-sections builds that retain only the
    # cartridge header (a valid-looking ROM that boots to black).
    for marker in (b"Ported by Ammaar and Fable", b"Road Completed"):
        if marker not in rom:
            fail(f"linked game-code marker missing: {marker!r}")

    # All fourteen standard VGM headers must be in the 68000's fixed low ROM
    # window. A missing/shifted blob otherwise produces a valid game ROM with
    # silent music or unreliable 32X startup.
    vgm_headers = []
    offset = 0
    while True:
        offset = rom.find(b"Vgm ", offset)
        if offset < 0:
            break
        vgm_headers.append(offset)
        offset += 4
    if len(vgm_headers) != 14:
        fail(f"expected 14 embedded VGM headers, got {len(vgm_headers)}")
    if max(vgm_headers) >= 0x80000:
        fail(f"VGM data escaped fixed 68000 ROM window: {max(vgm_headers):#x}")
    for number, offset in enumerate(vgm_headers):
        eof = offset + struct.unpack_from("<I", rom, offset + 4)[0] + 4
        clock = struct.unpack_from("<I", rom, offset + 0x2C)[0]
        if not (offset + 0x40 < eof <= 0x80000):
            fail(f"VGM {number} has bad/far EOF: {eof:#x}")
        if clock != 7_670_454:
            fail(f"VGM {number} has bad YM2612 clock: {clock}")

    nonzero = sum(x != 0 for x in rom)
    if nonzero < len(rom) // 8:
        fail("ROM payload is mostly empty")

    sections = elf_sections(elf_path)
    for name in (".text", ".data", ".bss"):
        if name not in sections:
            fail(f"ELF section missing: {name}")
    text_addr, _, text_size = sections[".text"]
    data_addr, _, data_bytes = sections[".data"]
    bss_addr, _, bss_size = sections[".bss"]
    if text_addr != 0x02000000 or text_size < 1_000_000:
        fail(f"bad/empty ROM text section: {text_addr:#x}+{text_size:#x}")
    if data_addr != 0x06000000:
        fail(f"initialized data is not in SDRAM: {data_addr:#x}")
    if bss_addr + bss_size >= 0x0603E000:
        fail(f"BSS collides with SH-2 stacks: end={bss_addr+bss_size:#x}")

    print(f"ROM verify OK: {len(rom):,} bytes, checksum {stored_sum:04X}, "
          f"SH-2 text {text_size:,}, SDRAM data+bss {data_bytes+bss_size:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
