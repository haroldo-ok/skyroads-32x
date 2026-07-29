#!/usr/bin/env python3
"""Predecode the freeware SkyRoads data into a ROM-friendly 32X asset blob.

The 32X has only 256 KiB of SDRAM. The desktop loader keeps more than 1 MiB
of decoded pictures and span tables on the heap, so the console build decodes
all immutable data at build time and reads it directly from cartridge ROM.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lzs import Stream, decompress
from opl2_to_vgm import convert_songs


@dataclass
class Section:
    base: int
    colors: list[tuple[int, int, int]]


@dataclass
class Picture:
    ofs: int
    w: int
    h: int
    pixels: bytes
    blob_ofs: int = 0


@dataclass
class Gfx:
    pictures: list[Picture] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)


class Blob:
    def __init__(self) -> None:
        self.data = bytearray()

    def add(self, data: bytes, align: int = 4) -> int:
        while len(self.data) % align:
            self.data.append(0)
        out = len(self.data)
        self.data.extend(data)
        return out


def upper_read(root: Path, name: str) -> bytes:
    return (root / name.upper()).read_bytes()


def load_gfx(root: Path, name: str, bases: list[int]) -> Gfx:
    data = upper_read(root, name)
    s = Stream(data, 0)
    out = Gfx()
    section = -1
    base = 0
    while s.pos < len(data):
        tag = s.read_bytes(4)
        if tag == b"CMAP":
            section += 1
            base = bases[min(section, len(bases) - 1)]
            count = s.read_byte()
            colors = []
            for _ in range(count):
                colors.append((s.read_byte(), s.read_byte(), s.read_byte()))
            s.read_bytes(count * 2)
            out.sections.append(Section(base, colors))
        elif tag == b"PICT":
            ofs = s.read_u16()
            h = s.read_u16()
            w = s.read_u16()
            px = bytearray(decompress(s, w * h))
            for i, p in enumerate(px):
                if p:
                    px[i] = (p + base) & 0xFF
            out.pictures.append(Picture(ofs, w, h, bytes(px)))
        else:
            break
    if not out.pictures:
        raise ValueError(f"{name}: no pictures")
    return out


def load_anim(root: Path) -> tuple[list[tuple[int, Picture]], int, Section]:
    data = upper_read(root, "anim.lzs")
    s = Stream(data, 0)
    if s.read_bytes(4) != b"ANIM":
        raise ValueError("ANIM.LZS: bad magic")
    frames = s.read_u16()
    if s.read_bytes(4) != b"CMAP":
        raise ValueError("ANIM.LZS: missing CMAP")
    count = s.read_byte()
    colors = [(s.read_byte(), s.read_byte(), s.read_byte()) for _ in range(count)]
    s.read_bytes(count * 2)
    records: list[tuple[int, Picture]] = []
    for frame in range(frames):
        count = s.read_u16()
        for _ in range(count):
            if s.read_bytes(4) != b"PICT":
                raise ValueError("ANIM.LZS: missing PICT")
            ofs = s.read_u16()
            h = s.read_u16()
            w = s.read_u16()
            records.append((frame, Picture(ofs, w, h, decompress(s, w * h))))
    return records, frames, Section(0, colors)


def load_gauge(root: Path, name: str) -> list[tuple[int, int, int, bytes]]:
    d = upper_read(root, name)
    out = []
    for i in range(10):
        off = struct.unpack_from("<H", d, i * 2)[0]
        p = 20 + off
        screen, w, h = struct.unpack_from("<HBB", d, p)
        out.append((screen, w, h, d[p + 4:p + 4 + w * h]))
    return out


def load_music(root: Path) -> list[tuple[int, bytes]]:
    """Decode the fourteen MUZAX songs for build-time VGM conversion."""
    d = upper_read(root, "muzax.lzs")
    songs = []
    # Retail directory has 15 slots; slot 14 is the all-zero terminator.
    for n in range(15):
        off, ninst, raw = struct.unpack_from("<HHH", d, n * 6)
        if off == 0 or raw == 0:
            break
        songs.append((ninst, decompress(Stream(d, off), raw)))
    if len(songs) < 14:
        raise ValueError(f"MUZAX.LZS: expected 14 songs, got {len(songs)}")
    return songs


def emit_vgm_assets(out: Path, vgms: list[bytes]) -> None:
    """Emit standard VGM files plus a ROM blob/table for the 68000 player."""
    vgm_dir = out / "vgm"
    vgm_dir.mkdir(parents=True, exist_ok=True)
    blob = bytearray()
    entries = []
    for number, vgm in enumerate(vgms):
        while len(blob) & 1:
            blob.append(0)
        entries.append((len(blob), len(vgm)))
        blob.extend(vgm)
        (vgm_dir / f"skyroads-{number:02d}.vgm").write_bytes(vgm)

    (out / "vgm_blob_68k.bin").write_bytes(blob)
    (out / "vgm_blob_68k.s").write_text(
        "    .section .vgm,\"a\"\n"
        "    .align 2\n"
        "    .global sr32_vgm_blob\n"
        "sr32_vgm_blob:\n"
        "    .incbin \"src/platform/32x/generated/32x/vgm_blob_68k.bin\"\n")
    lines = [
        "/* Generated by tools/build_32x_assets.py. Do not edit. */",
        '#include "vgm_data_68k.h"',
        "extern const uint8_t sr32_vgm_blob[];",
        f"const sr32_vgm_asset sr32_vgm_songs[{len(vgms)}] = {{",
    ]
    lines.extend(f"    {{sr32_vgm_blob + {offset}, {size}}},"
                 for offset, size in entries)
    lines.append("};\n")
    (out / "vgm_data_68k.c").write_text("\n".join(lines))
    (out / "vgm_data_68k.h").write_text(
        "#ifndef SR32_VGM_DATA_68K_H\n#define SR32_VGM_DATA_68K_H\n"
        "#include <stdint.h>\n"
        "typedef struct { const uint8_t *data; uint32_t size; } sr32_vgm_asset;\n"
        f"#define SR32_VGM_SONG_COUNT {len(vgms)}\n"
        "extern const sr32_vgm_asset sr32_vgm_songs[SR32_VGM_SONG_COUNT];\n"
        "#endif\n")


def pcm_gain_q8(data: bytes, target_pwm: int = 480) -> int:
    """Normalize unsigned 8-bit PCM close to full 32X PWM range.

    The source Sound Blaster effects have very different recorded levels. The
    old fixed 1.0x conversion only used about 27% of PWM range for the landing
    sample, so YM2612 music masked it. Keep 33 counts of hardware headroom and
    cap the boost at 4x; 256 is unity gain.
    """
    peak = max((abs(value - 128) for value in data), default=0)
    if peak == 0:
        return 256
    return max(256, min(1024, (target_pwm * 128 + peak // 2) // peak))


def load_sfx(root: Path) -> list[tuple[int, int, bytes]]:
    """Split SFX.SND into time constant, normalization gain, and PCM."""
    d = upper_read(root, "sfx.snd")
    count = struct.unpack_from("<H", d, 0)[0] // 2 - 1
    out = []
    for n in range(count):
        off, next_off = struct.unpack_from("<HH", d, n * 2)
        if not (off < next_off <= len(d)):
            raise ValueError(f"SFX.SND: invalid entry {n}")
        pcm = d[off + 1:next_off]
        out.append((d[off], pcm_gain_q8(pcm), pcm))
    return out


def load_roads(root: Path) -> list[dict]:
    d = upper_read(root, "roads.lzs")
    n = struct.unpack_from("<H", d, 0)[0] // 4
    roads = []
    for i in range(n):
        off, raw = struct.unpack_from("<HH", d, i * 4)
        s = Stream(d, off)
        gravity, word2, word3 = s.read_u16(), s.read_u16(), s.read_u16()
        palraw = s.read_bytes(216)
        pal = [tuple(palraw[j:j + 3]) for j in range(0, 216, 3)]
        rawcells = decompress(s, raw)
        words = struct.unpack("<" + "H" * (raw // 2), rawcells)
        # The SH-2 is big endian. Store words in native SH-2 byte order so a
        # const uint16_t* can point straight into ROM.
        cells = struct.pack(">" + "H" * len(words), *words)
        roads.append(dict(gravity=gravity, word2=word2, word3=word3,
                          palette=pal, rows=raw // 14, cells=cells))
    return roads


def load_trek_expanded(root: Path) -> list[bytes]:
    d = upper_read(root, "trekdat.lzs")
    s = Stream(d, 0)
    out = []
    while s.pos + 4 <= len(d) + 1:
        raw, comp = s.read_u16(), s.read_u16()
        if not raw or comp > raw:
            break
        obj = bytearray(raw)
        data_ofs = raw - comp
        obj[0:2] = struct.pack("<H", data_ofs)
        obj[data_ofs:data_ofs + comp] = decompress(s, comp)

        exp = bytearray(raw)
        src = data_ofs
        dst = 0
        exp[dst:dst + 0x270] = obj[src:src + 0x270]
        src += 0x270
        dst += 0x270
        for _ in range(0x410):
            if src >= raw:
                break
            exp[dst:dst + 3] = obj[src:src + 3]
            src += 3
            dst += 3
            while True:
                b = obj[src]
                src += 1
                exp[dst] = b
                dst += 1
                if b == 0xFF:
                    break
                exp[dst] = obj[src]
                exp[dst + 1] = 0
                src += 1
                dst += 2
        out.append(bytes(exp))
        if s.pos >= len(d):
            break
    if len(out) < 8:
        raise ValueError(f"TREKDAT.LZS: expected 8 objects, got {len(out)}")
    return out[:8]


def pristine(world: Gfx, dash: Gfx) -> bytes:
    fb = bytearray(320 * 200)
    p = world.pictures[0]
    x0, y0 = p.ofs % 320, p.ofs // 320
    for y in range(min(p.h, 200 - y0)):
        fb[(y0 + y) * 320 + x0:(y0 + y) * 320 + x0 + p.w] = \
            p.pixels[y * p.w:(y + 1) * p.w]
    p = dash.pictures[0]
    x0, y0 = p.ofs % 320, p.ofs // 320
    for y in range(p.h):
        for x in range(p.w):
            c = p.pixels[y * p.w + x]
            if c and 0 <= x0 + x < 320 and 0 <= y0 + y < 200:
                fb[(y0 + y) * 320 + x0 + x] = c
    return bytes(fb)


def color_init(colors: list[tuple[int, int, int]], indent: str = "    ") -> str:
    rows = []
    for i in range(0, len(colors), 6):
        rows.append(indent + ", ".join("{%d,%d,%d}" % c for c in colors[i:i + 6]))
    return ",\n".join(rows)


def bytes_init(data: bytes, width: int = 16, indent: str = "        ") -> str:
    rows = []
    for i in range(0, len(data), width):
        rows.append(indent + ",".join(str(x) for x in data[i:i + width]))
    return ",\n".join(rows)


def emit_gfx(c: list[str], ident: str, gfx: Gfx, blob: Blob) -> None:
    for p in gfx.pictures:
        p.blob_ofs = blob.add(p.pixels)
    c.append(f"static const sr_pict pict_{ident}[] = {{")
    for p in gfx.pictures:
        c.append(f"    {{{p.ofs}, {p.w}, {p.h}, (uint8_t *)(sr32_asset_blob + {p.blob_ofs})}},")
    c.append("};")
    c.append(f"static const sr_pal_section sec_{ident}[] = {{")
    for s in gfx.sections:
        c.append(f"    {{{s.base}, {len(s.colors)}, {{")
        c.append(color_init(s.colors, "        "))
        c.append("    }},")
    c.append("};")
    c.append(f"static const sr_gfxfile gfx_{ident} = {{")
    c.append(f"    (sr_pict *)pict_{ident}, {len(gfx.pictures)}, (sr_pal_section *)sec_{ident}, {len(gfx.sections)}")
    c.append("};\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data", type=Path)
    ap.add_argument("out", type=Path)
    ns = ap.parse_args()
    root = ns.data.resolve()
    out = ns.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    specs = {
        "mainmenu": ("mainmenu.lzs", [190]),
        "setmenu": ("setmenu.lzs", [200, 250]),
        "helpmenu": ("helpmenu.lzs", [200]),
        "gomenu": ("gomenu.lzs", [0, 240]),
        "intro": ("intro.lzs", [0]),
        "cars": ("cars.lzs", [72]),
        "dashbrd": ("dashbrd.lzs", [92]),
    }
    gfxs = {k: load_gfx(root, *v) for k, v in specs.items()}
    worlds = [load_gfx(root, f"world{i}.lzs", [142]) for i in range(10)]
    anim, anim_frames, anim_pal = load_anim(root)
    gauges = {"oxy": load_gauge(root, "oxy_disp.dat"),
              "ful": load_gauge(root, "ful_disp.dat")}
    songs = load_music(root)
    vgms = convert_songs(songs)
    emit_vgm_assets(out, vgms)
    sfx = load_sfx(root)
    roads = load_roads(root)
    trek = load_trek_expanded(root)

    blob = Blob()
    c: list[str] = [
        "/* Generated by tools/build_32x_assets.py. Do not edit. */",
        '#include "assets_data_32x.h"',
        '#include <string.h>',
        "",
        "extern const uint8_t sr32_asset_blob[];",
        "",
    ]
    for ident, gfx in gfxs.items():
        emit_gfx(c, ident, gfx, blob)
    for i, gfx in enumerate(worlds):
        emit_gfx(c, f"world{i}", gfx, blob)

    # Pre-composited immutable world/dashboard backgrounds.
    pristine_offsets = [blob.add(pristine(w, gfxs["dashbrd"])) for w in worlds]
    c.append("static const uint8_t *const pristine_world[10] = {")
    c.append("    " + ", ".join(f"sr32_asset_blob + {x}" for x in pristine_offsets))
    c.append("};\n")

    # TREKDAT's second expansion is also moved to build time.
    trek_offsets = [blob.add(x) for x in trek]
    c.append("const uint8_t *const sr32_trek_exp[8] = {")
    c.append("    " + ", ".join(f"sr32_asset_blob + {x}" for x in trek_offsets))
    c.append("};\n")

    # Original unsigned PCM effects remain on the 32X PWM slave.  Music is
    # emitted separately as Genesis VGM data for the 68000/YM2612 player.
    sfx_offsets = [blob.add(data) for _, _, data in sfx]
    c.append(f"const sr32_pcm_asset sr32_sfx[{len(sfx)}] = {{")
    for (tc, gain, data), off in zip(sfx, sfx_offsets):
        c.append(f"    {{sr32_asset_blob + {off}, {len(data)}, {tc}, {gain}}},")
    c.append("};\n")

    # Gauges.
    for name, values in gauges.items():
        c.append(f"static const sr_gauge_seg gauge_{name}[10] = {{")
        for ofs, w, h, cells in values:
            c.append(f"    {{{ofs}, {w}, {h}, {{")
            c.append(bytes_init(cells))
            c.append("    }},")
        c.append("};\n")

    # Intro animation records.
    for _, p in anim:
        p.blob_ofs = blob.add(p.pixels)
    c.append("static const sr_anim_rec anim_records[] = {")
    for frame, p in anim:
        c.append(f"    {{{frame}, {{{p.ofs}, {p.w}, {p.h}, (uint8_t *)(sr32_asset_blob + {p.blob_ofs})}}}},")
    c.append("};")
    c.append(f"static const sr_pal_section anim_palette = {{0, {len(anim_pal.colors)}, {{")
    c.append(color_init(anim_pal.colors, "    "))
    c.append("}};\n")

    # Roads.
    for r in roads:
        r["blob_ofs"] = blob.add(r["cells"])
    c.append("static const sr_road road_data[31] = {")
    for r in roads:
        c.append(f"    {{{r['gravity']}, {r['word2']}, {r['word3']}, {{")
        c.append(color_init(r["palette"], "        "))
        c.append(f"    }}, (uint16_t *)(sr32_asset_blob + {r['blob_ofs']}), {r['rows']}}},")
    c.append("};\n")

    # Small raw tables/samples needed by game logic and intro.
    raw_refs = {}
    for key, filename in (("speed", "SPEED.DAT"), ("demo", "DEMO.REC"),
                          ("intro_snd", "INTRO.SND")):
        data = upper_read(root, filename)
        raw_refs[key] = (blob.add(data), len(data))
    c.append("const sr32_pcm_asset sr32_intro_pcm = ")
    c.append(f"    {{sr32_asset_blob + {raw_refs['intro_snd'][0]}, "
             f"{raw_refs['intro_snd'][1]}, 0x5a, 256}};\n")

    c.extend([
        "void sr32_populate_assets(sr_assets *a)",
        "{",
    ])
    for ident in specs:
        c.append(f"    a->{ident} = gfx_{ident};")
    c.extend([
        "    /* Desktop loading applies these sections while decoding the files.",
        "     * Console pictures are predecoded, so seed their fixed palette", 
        "     * ranges explicitly before per-road/world palettes are loaded. */",
        "    sr_gfx_apply_pal(&a->cars, a->game_pal);",
        "    sr_gfx_apply_pal(&a->dashbrd, a->game_pal);",
        "    memcpy(a->oxy, gauge_oxy, sizeof a->oxy);",
        "    memcpy(a->ful, gauge_ful, sizeof a->ful);",
        "    a->anim = (sr_anim_rec *)anim_records;",
        f"    a->n_anim = {len(anim)};",
        f"    a->anim_frames = {anim_frames};",
        "    a->anim_pal = anim_palette;",
        f"    a->speed_dat = (uint8_t *)(sr32_asset_blob + {raw_refs['speed'][0]});",
        f"    a->speed_size = {raw_refs['speed'][1]};",
        f"    a->demo = (uint8_t *)(sr32_asset_blob + {raw_refs['demo'][0]});",
        f"    a->demo_size = {raw_refs['demo'][1]};",
        f"    a->intro_snd = (uint8_t *)(sr32_asset_blob + {raw_refs['intro_snd'][0]});",
        f"    a->intro_snd_size = {raw_refs['intro_snd'][1]};",
        "}",
        "",
        "bool sr32_load_road(sr_assets *a, int entry, sr_road *road)",
        "{",
        "    if (entry < 0 || entry >= 31) return false;",
        "    *road = road_data[entry];",
        "    memcpy(a->game_pal, road->palette, sizeof road->palette);",
        "    return true;",
        "}",
        "",
        "bool sr32_load_world(sr_assets *a, int world)",
        "{",
        "    static const sr_gfxfile *const all[10] = {",
        "        &gfx_world0,&gfx_world1,&gfx_world2,&gfx_world3,&gfx_world4,",
        "        &gfx_world5,&gfx_world6,&gfx_world7,&gfx_world8,&gfx_world9",
        "    };",
        "    if (world < 0 || world >= 10) return false;",
        "    a->world = *all[world];",
        "    a->pristine = pristine_world[world];",
        "    sr_gfx_apply_pal(&a->world, a->game_pal);",
        "    return true;",
        "}",
    ])

    (out / "assets_data_32x.c").write_text("\n".join(c) + "\n")
    (out / "assets_data_32x.h").write_text(
        "#ifndef SR_ASSETS_DATA_32X_H\n#define SR_ASSETS_DATA_32X_H\n"
        "#include \"../../../../core/assets.h\"\n"
        "typedef struct { const uint8_t *data; uint32_t size; uint8_t time_constant; uint16_t gain_q8; } sr32_pcm_asset;\n"
        f"#define SR32_SFX_COUNT {len(sfx)}\n"
        "void sr32_populate_assets(sr_assets *a);\n"
        "bool sr32_load_road(sr_assets *a, int entry, sr_road *road);\n"
        "bool sr32_load_world(sr_assets *a, int world);\n"
        "extern const uint8_t *const sr32_trek_exp[8];\n"
        "extern const sr32_pcm_asset sr32_sfx[SR32_SFX_COUNT];\n"
        "extern const sr32_pcm_asset sr32_intro_pcm;\n"
        "#endif\n")
    (out / "assets_32x.bin").write_bytes(blob.data)
    (out / "assets_blob_32x.s").write_text(
        "    .section .rodata\n"
        "    .align 4\n"
        "    .global _sr32_asset_blob\n"
        "_sr32_asset_blob:\n"
        "    .incbin \"src/platform/32x/generated/32x/assets_32x.bin\"\n")
    print(f"Generated {len(blob.data):,} SH-2 asset bytes and "
          f"{sum(map(len, vgms)):,} 68000 VGM bytes; {len(anim)} animation "
          f"records, {len(roads)} roads, {len(trek)} TREK objects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
