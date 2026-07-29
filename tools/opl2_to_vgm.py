#!/usr/bin/env python3
"""Convert SkyRoads' decoded OPL2 event songs to Genesis VGM streams.

The six melodic OPL2 channels are translated to the six YM2612 FM channels.
The OPL rhythm voices are arranged on the Genesis PSG so percussion does not
steal an FM voice.  The resulting files are standard, uncompressed VGM 1.50
streams and can be played by ordinary VGM tools as well as the ROM's 68000
player.
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    from lzs import Stream, decompress
except ImportError:  # Imported as tools.opl2_to_vgm by tests.
    from tools.lzs import Stream, decompress

VGM_RATE = 44_100
OPL_TICK_NUM = 1_193_182
OPL_TICK_DEN = 6_628
YM2612_CLOCK = 7_670_454
PSG_CLOCK = 3_579_545
# Leave analog headroom for the 32X PWM effect path. YM2612 TL steps are
# approximately 0.75 dB, so +8 is about -6 dB on audible carrier operators.
YM_MUSIC_TL_BIAS = 8

# One octave of equal-tempered YM2612 F-numbers.  Block 3 produces C3..B3,
# matching the original driver's OPL block 2/F-number table for notes 0..11.
YM_FNUM = (644, 681, 722, 765, 810, 858, 910, 964, 1021, 1081, 1146, 1214)
YM_CHANNEL_CODE = (0, 1, 2, 4, 5, 6)
YM_CHANNEL_REG = (0, 1, 2, 0, 1, 2)
YM_CHANNEL_PORT = (0x52, 0x52, 0x52, 0x53, 0x53, 0x53)
YM_SLOT_OFFSET = (0, 8, 4, 12)  # YM register order for logical OP1..OP4.

# Same attenuation curve used by the original DOS OPL driver.
VOL_TAB = (
    0x3F, 0x14, 0x10, 0x0E, 0x0C, 0x0A, 0x09, 0x08,
    0x07, 0x06, 0x06, 0x05, 0x05, 0x04, 0x04, 0x04,
    0x04, 0x04, 0x03, 0x03, 0x03, 0x03, 0x02, 0x02,
    0x02, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00,
)

# Relative-tick, PSG attenuation envelopes.  0 is loud and 15 is muted.
PSG_ENVELOPES = {
    0: ((1, 2), (2, 4), (4, 7), (6, 11), (9, 15)),   # bass drum
    1: ((1, 3), (2, 7), (4, 11), (6, 15)),           # tom
    3: ((1, 4), (2, 9), (4, 15)),                    # noise percussion
}


def load_muzax(path: Path) -> list[tuple[int, bytes]]:
    data = path.read_bytes()
    songs: list[tuple[int, bytes]] = []
    for number in range(15):
        off, ninst, raw = struct.unpack_from("<HHH", data, number * 6)
        if not off or not raw:
            break
        songs.append((ninst, decompress(Stream(data, off), raw)))
    if len(songs) != 14:
        raise ValueError(f"{path}: expected 14 songs, got {len(songs)}")
    return songs


@dataclass
class PsgEnvelope:
    generation: int = 0
    changes: list[tuple[int, int, int]] = field(default_factory=list)


class VgmBuilder:
    def __init__(self, number: int, ninst: int, song: bytes):
        if ninst <= 0 or ninst * 16 >= len(song):
            raise ValueError(f"song {number}: invalid instrument table")
        self.number = number
        self.ninst = ninst
        self.song = song
        self.instruments = song[:ninst * 16]
        self.events = song[ninst * 16:]
        self.stream = bytearray()
        self.channel_instrument = [0] * 11
        self.channel_volume = [30] * 11
        self.psg = [PsgEnvelope() for _ in range(4)]
        self.tick = 0
        self.sample_position = 0
        self.loop_stream_offset: int | None = None
        self.loop_sample_position = 0
        self.command_count = 0

    def ym(self, channel: int, register: int, value: int) -> None:
        self.stream.extend((YM_CHANNEL_PORT[channel], register & 0xFF,
                            value & 0xFF))

    def ym_global(self, register: int, value: int) -> None:
        self.stream.extend((0x52, register & 0xFF, value & 0xFF))

    def psg_write(self, value: int) -> None:
        self.stream.extend((0x50, value & 0xFF))

    def psg_volume(self, voice: int, attenuation: int) -> None:
        attenuation = max(0, min(15, attenuation))
        self.psg_write(0x90 | (voice << 5) | attenuation)

    def psg_tone(self, voice: int, frequency: float) -> None:
        period = max(1, min(1023, round(PSG_CLOCK / (32.0 * frequency))))
        self.psg_write(0x80 | (voice << 5) | (period & 0x0F))
        self.psg_write((period >> 4) & 0x3F)

    @staticmethod
    def _rate(value: int) -> int:
        """Expand an OPL 4-bit envelope rate to the YM2612's 5-bit range."""
        return 0 if value == 0 else min(31, value * 2 + 1)

    @staticmethod
    def _total_level(opl_value: int, extra: int = 0) -> int:
        return min(127, (opl_value & 0x3F) * 2 + extra)

    def _operator(self, channel: int, slot: int, opl: bytes,
                  total_level: int | None = None) -> None:
        reg = YM_CHANNEL_REG[channel] + YM_SLOT_OFFSET[slot]
        am_vib_egt_ksr_mul, tl, ar_dr, sl_rr, _wave = opl
        # OPL vibrato has no direct static equivalent.  AM is retained and
        # the multiplier/envelope are expanded to OPN2 resolution.
        self.ym(channel, 0x30 + reg, am_vib_egt_ksr_mul & 0x0F)
        self.ym(channel, 0x40 + reg,
                self._total_level(tl) if total_level is None else total_level)
        rate_scale = 2 if (am_vib_egt_ksr_mul & 0x10) else 0
        self.ym(channel, 0x50 + reg,
                (rate_scale << 6) | self._rate(ar_dr >> 4))
        self.ym(channel, 0x60 + reg,
                (0x80 if am_vib_egt_ksr_mul & 0x80 else 0) |
                self._rate(ar_dr & 0x0F))
        # OPL's EGT bit holds at sustain; otherwise continue decaying.
        self.ym(channel, 0x70 + reg,
                0 if (am_vib_egt_ksr_mul & 0x20) else self._rate(sl_rr & 0x0F))
        self.ym(channel, 0x80 + reg, sl_rr)
        self.ym(channel, 0x90 + reg, 0)

    def key_off(self, channel: int) -> None:
        if channel < 6:
            self.ym_global(0x28, YM_CHANNEL_CODE[channel])
        elif channel == 6:
            self._cancel_psg(0)
        elif channel == 9:
            self._cancel_psg(1)
        else:
            self._cancel_psg(3)

    def program_instrument(self, channel: int, number: int) -> None:
        if channel >= 11 or number >= self.ninst:
            return
        self.channel_instrument[channel] = number
        if channel >= 6:
            return
        self.key_off(channel)
        rec = self.instruments[number * 16:number * 16 + 11]
        op1 = rec[0:5]
        op2 = rec[5:10]
        additive = bool(rec[10] & 1)

        # OPL2 is a two-operator chip.  Algorithms 4 and 7 let OP1/OP2 keep
        # their FM/additive relationship while unused OP3/OP4 are silenced.
        self._operator(
            channel, 0, op1,
            self._total_level(op1[1], YM_MUSIC_TL_BIAS) if additive else None)
        self._operator(channel, 1, op2,
                       self._total_level(op2[1], YM_MUSIC_TL_BIAS))
        silent = bytes((1, 0x3F, 0x00, 0x0F, 0))
        self._operator(channel, 2, silent, 127)
        self._operator(channel, 3, silent, 127)
        feedback = (rec[10] >> 1) & 7
        algorithm = 7 if additive else 4
        chreg = YM_CHANNEL_REG[channel]
        self.ym(channel, 0xB0 + chreg, (feedback << 3) | algorithm)
        self.ym(channel, 0xB4 + chreg, 0xC0)  # centered stereo

    def set_volume(self, channel: int, volume: int) -> None:
        if channel >= 11:
            return
        volume = max(0, min(30, volume))
        self.channel_volume[channel] = volume
        if channel >= 6:
            return
        number = self.channel_instrument[channel]
        if number >= self.ninst:
            return
        rec = self.instruments[number * 16:number * 16 + 11]
        additive = bool(rec[10] & 1)
        extra = VOL_TAB[volume] * 2 + YM_MUSIC_TL_BIAS
        reg = YM_CHANNEL_REG[channel]
        # OP2 is always audible; OP1 is also a carrier in additive mode.
        self.ym(channel, 0x40 + reg + YM_SLOT_OFFSET[1],
                self._total_level(rec[6], extra))
        if additive:
            self.ym(channel, 0x40 + reg + YM_SLOT_OFFSET[0],
                    self._total_level(rec[1], extra))

    def _note_frequency(self, note: int) -> float:
        return 130.81278265 * (2.0 ** (note / 12.0))

    def _start_envelope(self, voice: int, start_attenuation: int,
                        shape: Iterable[tuple[int, int]]) -> None:
        env = self.psg[voice]
        env.generation += 1
        generation = env.generation
        env.changes.clear()
        self.psg_volume(voice, start_attenuation)
        for relative_tick, attenuation in shape:
            env.changes.append((self.tick + relative_tick,
                                min(15, start_attenuation + attenuation),
                                generation))

    def _cancel_psg(self, voice: int) -> None:
        env = self.psg[voice]
        env.generation += 1
        env.changes.clear()
        self.psg_volume(voice, 15)

    def note_on(self, channel: int, note: int) -> None:
        if channel < 6:
            self.key_off(channel)
            semitone = note % 12
            block = max(0, min(7, note // 12 + 3))
            fnum = YM_FNUM[semitone]
            reg = YM_CHANNEL_REG[channel]
            self.ym(channel, 0xA4 + reg, (block << 3) | (fnum >> 8))
            self.ym(channel, 0xA0 + reg, fnum & 0xFF)
            self.ym_global(0x28, 0xF0 | YM_CHANNEL_CODE[channel])
            return

        # Genesis PSG augmentation for the OPL rhythm channels.  All melody
        # remains on the YM2612; this avoids dropping the sixth FM voice.
        start = max(0, min(12, (30 - self.channel_volume[channel]) // 2))
        if channel == 6:                    # bass drum
            self.psg_tone(0, max(45.0, self._note_frequency(note) * 0.5))
            self._start_envelope(0, start, PSG_ENVELOPES[0])
        elif channel == 9:                  # tom
            self.psg_tone(1, max(70.0, self._note_frequency(note)))
            self._start_envelope(1, start + 1, PSG_ENVELOPES[1])
        else:                               # hat/snare/cymbal share noise
            noise = 0xE4 if channel == 7 else (0xE5 if channel == 8 else 0xE6)
            self.psg_write(noise)
            self._start_envelope(3, start + 1, PSG_ENVELOPES[3])

    def _next_envelope_tick(self, end_tick: int) -> int:
        result = end_tick
        for env in self.psg:
            if env.changes:
                result = min(result, env.changes[0][0])
        return result

    def _apply_envelopes(self) -> None:
        for voice, env in enumerate(self.psg):
            while env.changes and env.changes[0][0] <= self.tick:
                _, attenuation, generation = env.changes.pop(0)
                if generation == env.generation:
                    self.psg_volume(voice, attenuation)

    def _emit_sample_wait(self, samples: int) -> None:
        while samples:
            part = min(samples, 0xFFFF)
            self.stream.extend((0x61, part & 0xFF, part >> 8))
            samples -= part

    def wait_ticks(self, ticks: int) -> None:
        end_tick = self.tick + ticks
        while self.tick < end_tick:
            next_tick = self._next_envelope_tick(end_tick)
            if next_tick <= self.tick:
                self._apply_envelopes()
                continue
            previous_samples = self.sample_position
            self.tick = next_tick
            # Round absolute time so long waits do not accumulate drift.
            self.sample_position = round(
                self.tick * VGM_RATE * OPL_TICK_DEN / OPL_TICK_NUM)
            self._emit_sample_wait(self.sample_position - previous_samples)
            self._apply_envelopes()

    def initialize_stream(self) -> None:
        self.ym_global(0x22, 0x00)  # LFO off
        self.ym_global(0x2B, 0x00)  # DAC off; channel 6 remains FM
        for channel in range(6):
            self.key_off(channel)
            self.ym(channel, 0xB4 + YM_CHANNEL_REG[channel], 0xC0)
            for slot in range(4):
                self.ym(channel, 0x40 + YM_CHANNEL_REG[channel] +
                        YM_SLOT_OFFSET[slot], 127)
        for voice in range(4):
            self.psg_volume(voice, 15)

    def convert(self) -> bytes:
        self.initialize_stream()
        pos = 0
        while pos + 1 < len(self.events):
            event_offset = pos
            word = self.events[pos] | (self.events[pos + 1] << 8)
            pos += 2
            self.command_count += 1
            operation = word & 7
            channel = (word >> 4) & 0x0F
            parameter = word >> 8

            if operation == 0:
                # The original player stores parameter then spends that many
                # additional IRQs decrementing it: delay N means N+1 ticks.
                self.wait_ticks(parameter + 1)
            elif operation == 1:
                self.program_instrument(channel, parameter)
            elif operation == 2:
                self.note_on(channel, parameter)
            elif operation == 3:
                self.key_off(channel)
            elif operation == 4:
                self.set_volume(channel, parameter)
            elif operation == 5:
                break
            elif operation == 6:
                self.loop_stream_offset = len(self.stream)
                self.loop_sample_position = self.sample_position
            # Operation 7 is an unused synchronization marker.
        else:
            raise ValueError(f"song {self.number}: event stream has no loop/end")

        self.stream.append(0x66)
        return self._with_header()

    def _with_header(self) -> bytes:
        header = bytearray(0x40)
        header[0:4] = b"Vgm "
        struct.pack_into("<I", header, 0x08, 0x00000150)
        struct.pack_into("<I", header, 0x0C, PSG_CLOCK)
        struct.pack_into("<I", header, 0x18, self.sample_position)
        struct.pack_into("<I", header, 0x2C, YM2612_CLOCK)
        struct.pack_into("<I", header, 0x34, 0x0C)  # data starts at 0x40
        if self.loop_stream_offset is not None:
            absolute_loop = 0x40 + self.loop_stream_offset
            struct.pack_into("<I", header, 0x1C, absolute_loop - 0x1C)
            struct.pack_into("<I", header, 0x20,
                             self.sample_position - self.loop_sample_position)
        output = header + self.stream
        struct.pack_into("<I", output, 0x04, len(output) - 4)
        return bytes(output)


def convert_songs(songs: Iterable[tuple[int, bytes]]) -> list[bytes]:
    return [VgmBuilder(number, ninst, song).convert()
            for number, (ninst, song) in enumerate(songs)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("muzax", type=Path, help="path to MUZAX.LZS")
    parser.add_argument("output", type=Path, help="output directory")
    args = parser.parse_args(argv)

    args.output.mkdir(parents=True, exist_ok=True)
    vgms = convert_songs(load_muzax(args.muzax))
    for number, vgm in enumerate(vgms):
        (args.output / f"skyroads-{number:02d}.vgm").write_bytes(vgm)
    print(f"Converted {len(vgms)} tracks to {sum(map(len, vgms)):,} VGM bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
