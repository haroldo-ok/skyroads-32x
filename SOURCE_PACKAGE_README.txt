SkyRoads 32X source package

This archive contains the portable engine, Sega 32X platform code, build-time
OPL2-to-YM2612 VGM converter/player, tests, documentation, and build files.

Bluemoon Interactive's original SkyRoads game data is freeware but is not
redistributed here. Before building, place the original *.LZS, *.DAT, *.SND,
and *.REC files in the project root.

Toolchain (required for the 32X build; desktop builds only need CMake + SDL2):

  curl -LO https://github.com/viciious/32XDK/releases/download/20220418/chillys-sega-devkit-20220418-opt.tar.zst
  mkdir -p /opt/toolchains
  tar --zstd -xf chillys-sega-devkit-20220418-opt.tar.zst -C /opt/toolchains
  mv /opt/toolchains/opt/toolchains/sega /opt/toolchains/sega

  (--zstd needs the zstd package. Without root access, extract anywhere and
   pass GENDEV=/path/to/sega to make.)

Build:
  make -f Makefile.32x -j release
  make -f Makefile.32x -j GENDEV=$HOME/toolchains/sega release   # custom path

Outputs:
  release/SkyRoads32X.32x       playable 32X ROM
  release/SkyRoads32X-vgm.zip   14 standard Genesis VGM tracks
  release/SkyRoads32X-source.zip this source package
