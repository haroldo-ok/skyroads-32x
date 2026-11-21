# SkyRoads — a 1:1 reverse-engineered portable port

In 1993, Bluemoon Interactive — the Estonian studio whose founders later
built the technology behind Kazaa and Skype — released **SkyRoads**, a
gorgeous space-racing game for DOS. The source code was never published.

This repo is a from-scratch reverse engineering of that game: the
original `SKYROADS.EXE` was disassembled, every file format and algorithm
was recovered from the machine code, and the whole engine was rewritten
as ~3,000 lines of portable C11. It runs today on **macOS, Windows,
Linux, the web (Emscripten), and iOS** — with the exact 36.0036 Hz timing,
fixed-point physics, and pre-baked-perspective renderer of the original.

Made as a love letter to Bluemoon Interactive and everyone who spent the
90s bunny-hopping down neon roads in space. All game content is theirs;
SkyRoads was released as freeware.

Original source: https://github.com/ammaarreshi/skyroads-port

## How faithful is it?

This is not a remake "in the spirit of" the original — it aims to be the
same game:

- **No original code executes.** The engine is new C, linking only SDL2.
- **Demo-proven physics.** The original's 1993 attract-demo input
  recording (`DEMO.REC`) is position-indexed raw input. Replayed through
  this engine's reimplemented physics, it completes the entire demo road
  tick-for-tick — jump arcs, boost pads, sticky tiles, collisions, and
  the hidden landing autopilot all have to be exact for that to work.
  It's a regression test: `ctest --test-dir build`.
- **Receipts included.** `docs/FORMATS.md` specifies every file format,
  and `re/notes/` documents the recovered game logic with the EXE
  instruction address for every constant.
- **Original data, untouched.** The engine reads the unmodified retail
  files at runtime. No converted assets.

## Getting the game data

**This repository contains no Bluemoon files.** SkyRoads is freeware —
download it from [bluemoon.ee](https://www.bluemoon.ee/history/skyroads/)
and copy the data files (`*.LZS`, `*.DAT`, `*.SND`, `DEMO.REC`) into the
repo root (they're gitignored), or pass their folder to the executable.

## Build & run

```sh
cmake -B build -S . && cmake --build build
./build/skyroads /path/to/skyroads-data     # or run from the data dir
```

Web, iOS (including TestFlight), and Windows cross-builds:
see [docs/PORTING.md](docs/PORTING.md).

### Sega 32X toolchain

The 32X build needs a cross-compiler for both CPUs — SH-2 (the main processors)
and M68k (the sound-side 68000):

```sh
# Chilly Willy's Sega devkit, release 20220418 (GCC 12.1.0)
curl -LO https://github.com/viciious/32XDK/releases/download/20220418/chillys-sega-devkit-20220418-opt.tar.zst

mkdir -p /opt/toolchains
tar --zstd -xf chillys-sega-devkit-20220418-opt.tar.zst -C /opt/toolchains
# the archive unpacks to opt/toolchains/sega; move it into place
mv /opt/toolchains/opt/toolchains/sega /opt/toolchains/sega
```

`--zstd` needs the `zstd` package (Debian/Ubuntu: `sudo apt install zstd`).
If you cannot write to `/opt`, extract anywhere and point the build at it with
`GENDEV`:

```sh
make -f Makefile.32x -j GENDEV=$HOME/toolchains/sega release
```

Verify both compilers before building:

```sh
/opt/toolchains/sega/sh-elf/bin/sh-elf-gcc --version    # GCC 12.1.0
/opt/toolchains/sega/m68k-elf/bin/m68k-elf-gcc --version # GCC 12.1.0
```

### Sega 32X

The 32X cartridge build, controller map, memory design and headless PicoDrive
point-to-point tests are documented in [docs/32X.md](docs/32X.md). The ROM is
built with `make -f Makefile.32x -j release` and written to
`release/SkyRoads32X.32x`.

Controls: arrows steer/throttle, Space jumps, Esc back, P pause — same as
DOS. Touch (iOS): d-pad lower-left, jump button lower-right.

#### Testing the 32X build

`make -f Makefile.32x release` runs these as part of the build:

| Suite | Coverage |
|---|---|
| `tests/verify_rom.py` | Genesis/MARS headers, checksum, SH-2 entry points, ELF sections, SDRAM/stack bounds |
| `tests/test_vgm_conversion.py` | Rebuilds all 14 tracks: pitch accuracy, slot map, carrier levels, loop/end, generated-file freshness, ROM size budget |
| `tests/test_pwm_sfx.py` | PWM sound-effect normalization |
| `tests/run_emulator_tests.py` | Boots the real ROM in PicoDrive and checks rendered frames + audio |

The emulator suite is the end-to-end one: it drives the cartridge from the
intro through the menus into gameplay, and verifies both the music and the
landing sound effect in the audio output. See [docs/32X.md](docs/32X.md) for
how to build the PicoDrive core it needs.

#### Music on the 32X

The 32X has a real Yamaha YM2612 and an SN76489, but the original soundtrack is
a DOS OPL2 score. At build time
[`tools/opl2_to_vgm.py`](tools/opl2_to_vgm.py) converts all fourteen MUZAX
songs into standard Genesis VGM files, which the 68000 resident player
([`md_vgm.c`](src/platform/32x/md_src/md_vgm.c)) streams to the actual sound
chips. Nothing is sampled and no emulation runs on the console — the hardware
plays it. The converted tracks also ship as `release/SkyRoads32X-vgm.zip` and
play in any VGM player.

Two details drive the design:

- **All six FM channels stay melodic.** The 32X's YM2612 channels are reserved
  for the OPL2's melodic voices, so all five OPL rhythm voices are
  resynthesised on the SN76489 — three tone generators plus its noise channel,
  with measured per-drum envelopes and noise colours taken from the DOS
  renders rather than guessed.
- **The music leaves headroom.** Music comes out of the YM2612/SN76489 and
  effects come out of the separate PWM DAC, so the two are summed by the
  hardware, not by software. The music is still deliberately attenuated
  (`YM_MUSIC_TL_BIAS`) so it cannot sit on top of the effects. That constant is
  pinned by `tests/test_vgm_conversion.py`.

The whole blob has to fit the 68000's fixed 512 KiB ROM window alongside
startup code and the Work RAM image, so the converter emits compact streams
and `tests/test_vgm_conversion.py` enforces the budget (currently 408,240 of
458,752 bytes). The suite also rebuilds every track and checks pitch accuracy
(within 3 cents over notes 0–87), the YM channel/slot mapping, carrier levels,
loop and end commands, and that the checked-in generated assets are fresh.

#### Sound effects on the 32X

The five DOS sound effects and the intro's digitized voice are original PCM
data, and they are played back rather than emulated: [`audio_32x.c`](src/platform/32x/audio_32x.c)
runs on the slave SH-2, which streams `SFX.SND` and `INTRO.SND` bytes over the
32X's PWM DAC while the 68000 keeps feeding the FM chips. The two processors are
independent, so an effect never interrupts the music.

`SFX.SND` records its five effects at wildly different levels, so
`tools/build_32x_assets.py` normalizes each one with a per-effect `gain_q8` to a
common 480-count PWM peak. The intro stays at unity. `tests/test_pwm_sfx.py`
pins those gains and the resulting peak.

**Pacing is the subtle part.** The slave loop must emit one sample per PWM
frame — 22050 Hz — so each sample gets 1043 SH-2 cycles (23 MHz / 22050). It
paces with `PWM_WRITE_PACE` rather than blocking on the PWM FIFO full flags.
Those flags can stay asserted indefinitely, and waiting on them wedged the
streaming loop after a handful of frames: every effect played only its first
few bytes and came out as a click instead of the full sample. `test_pwm_sfx.py`
asserts the loop never blocks on those flags, so the regression cannot return.

Measured in PicoDrive, the landing effect peaks about **10x above its own
music floor** and runs about 0.96 s (the 0.644 s source sample plus the bounce
retrigger).

## Make your own tracks

Because the formats are fully decoded, the game is moddable:

```sh
python3 tools/make_track.py /path/to/gamedir /tmp/mod
```

builds a brand-new road (custom layout, palette, gravity/fuel/oxygen) and
splices it into a copy of `ROADS.LZS`. Tile semantics are in
`docs/FORMATS.md` §3 — geometry, tunnels, boost/sticky/ice/burning
surfaces, fuel pickups, per-road palettes are all yours to edit.

## Layout

- `src/core/` — platform-independent engine core (C11, no OS calls)
- `src/platform/` — SDL2 shell (desktop/web/iOS), iOS bundle bits
- `src/tools/` — headless render/demo/music test harnesses
- `tools/` — Python: format extractor, LZS codec, track builder,
  OPL2→YM2612 VGM converter (`opl2_to_vgm.py`), 32X asset packager
- `docs/` — format specs and porting guide
- `re/` — function map and per-subsystem reverse-engineering notes

## Credits

- **Bluemoon Interactive** — SkyRoads (1993): game design, art, music,
  and three decades of inspiration. Copyright remains theirs.
- Port by **Ammaar Reshi** and **Claude (Fable 5)**, reverse engineered
  from the retail binary in roughly a day.
- [Nuked-OPL3](https://github.com/nukeykt/Nuked-OPL3) (LGPL-2.1) for
  OPL2 emulation on desktop targets (the 32X build uses converted YM2612 VGM);
  `font8x8` (public domain) for two strings of UI text.

## License

The port's source code is MIT (see [LICENSE](LICENSE)), except
`src/thirdparty/` which keeps its upstream licenses. SkyRoads game
content is not covered by this license and is not distributed here.
