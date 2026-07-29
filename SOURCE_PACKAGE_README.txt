SkyRoads 32X source package

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
