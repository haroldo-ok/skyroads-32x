#!/usr/bin/env python3
"""Convert SkyRoads' decoded OPL2 (AdLib) scores to Genesis YM2612 VGM streams.

The DOS game drives a Yamaha YM3812 (OPL2).  This converter re-creates each
score on the Sega Genesis' Yamaha YM2612 as exactly as the two chips allow:

* **Register-level source data.**  The MUZAX scores are played with the same
  driver the DOS EXE uses, so the source semantics are register writes: two
  byte-identical instrument records (OP1: 20/40/60/80/E0, OP2: 20/40/60/80),
  a 0xC0 connection/feedback byte, 20/40h volume slides and the OPL rhythm
  mode percussion.  Every OPL2 bit has a deliberate YM2612 translation below.

* **Pitch.**  The DOS player computes `block = note // 12 + 2` with the EXE's
  F-number table.  Those F-numbers are a slightly stretched tuning (A4 =
  440.0 Hz at block 3, note 33); the YM2612 F-numbers are exact.  Notes are
  therefore placed on the YM2612 grid by solving for (block, fnum) that is
  closest to the *actual DOS frequency*, always keeping fnum >= 512 so the
  YM's 10-bit phase accumulator is not starved (which audibly de-tunes).

* **Voices.**  Four-operator YM2612 channels do not exist, so each OPL2
  voice keeps its exact two-operator structure: OPL OP1 -> YM OP1,
  OPL OP2 -> YM OP2, with the unused OP3/OP4 silenced.  The OPL2 algorithms
  are 0 = FM (OP1 modulates OP2) and 1 = additive; these map to YM2612
  algorithms 4 and 7 respectively.  Feedback is carried over.

* **Levels.**  OPL TL is 0.75 dB/step over 6 bits; YM TL is 0.75 dB/step
  over 7 bits (0.375 dB at low settings).  Using the full 7-bit range keeps
  the original dynamic range instead of folding every OPL level into the
  bottom half of the YM range.

* **Envelopes.**  OPL rates are 4-bit while YM rates are 5-bit and twice as
  fast per step, so the naive "double the rate" mapping re-times every
  instrument.  Rates are converted with the chips' documented time constants
  (see `opl_rate_to_ym`) and the OPL "key-off holds a key-on rate" bit (EGT)
  is translated into a YM sustain rate instead of a release rate of zero.

* **Percussion.**  OPL rhythm mode has one bass drum (ch6), a snare (ch7),
  tom (ch8), top cymbal (ch8) and hi-hat (ch7).  On the Genesis the six FM
  channels are fully occupied by melody, so the OPL rhythm voices - which
  are pitch-swept noise in the original - are resynthesized as SN76489
  tone/noise envelopes whose sweeps are transcribed in absolute time.

The output is standard, uncompressed, looping VGM 1.50 that can be played by
ordinary VGM tools as well as the ROM's 68000 player.
"""
from __future__ import annotations

import argparse
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    from lzs import Stream, decompress
except ImportError:  # Imported as tools.opl2_to_vgm by tests.
    from tools.lzs import Stream, decompress

VGM_RATE = 44_100
OPL_SAMPLE_RATE = 49_716          # YM3812 sample rate at the DOS 3.58 MHz clock
YM2612_CLOCK = 7_670_454
PSG_CLOCK = 3_579_545
YM2612_RATE = YM2612_CLOCK / 144  # 53 267 Hz

# The 32X PWM effect path is normalized to nearly full scale, so the FM music
# keeps this much headroom (added to every audible YM total-level value).
YM_MUSIC_TL_BIAS = 8               # ~6 dB headroom

# The OPL2's 3-bit feedback adds 1/8 of the modulator's own output per stage
# (up to pi radians at FB=7); the YM2612's doubles the self-modulation every
# step and reaches roughly twice that depth at FB=7.  Measured against DOS
# renders of all eight song-1 instruments, scaling the OPL feedback by 0.5
# minimises the harmonic error (8.5 dB vs 10.2 dB at 1:1), so FB 0..7 maps to
# the same 0..7 field after halving.
YM_FEEDBACK_SCALE = 0.5          # multiplier applied to the OPL FB value
# Both chips key-scale the envelope rates the same way: the "KSR" bit selects
# between full (bit=1) and quarter (bit=0) pitch tracking of the rates
# (Nuked OPL3: ksv >> ((reg_ksr ^ 1) << 1); ymfm: keycode >> (op_ksr ^ 3)).
# OPL KSR=1 therefore maps to the YM's fastest setting 3 and KSR=0 to 0.
YM_KSR_FAST = 3                  # 0x50 bits 7..6 for an OPL KSR=1 operator
YM_KSR_SLOW = 0                  # ... and for an OPL KSR=0 operator
YM_DT2 = 0                       # 0x30 bit 7 (adds a half-rate key scale)
YM_MOD_TL_OFFSET = 0             # extra attenuation added to the modulator
                                 # (must stay 0: raising it lifts total FM
                                 # output and clips the PWM SFX mix)

# VGM carries no mixing levels: on real hardware the PSG and the YM2612 are
# summed by fixed analog gains.  The DOS percussion voices are a background
# element, so their PSG output is biased down by this many 2 dB steps to land
# in the same balance.  (Raise it if the drums drown the music.)
PSG_GLOBAL_ATTENUATION = 4
SWEEP_SAMPLE_TICKS = 4           # pitch-sweep update period (~22 ms)
PSG_MIN_PERIOD_STEP = 2          # ignore sub-step tone period changes
PSG_ENVELOPE_SPLITS = True       # split waits so percussion envelopes step

# OPL2 note table exactly as the DOS EXE uses it (ds:0xc77 / ds:0xc83).
OPL_FNUM_LO = (0xac, 0xb6, 0xc1, 0xcd, 0xd9, 0xe6, 0xf3,
               0x02, 0x11, 0x22, 0x33, 0x45)
OPL_FNUM_HI = (0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1)

# Same attenuation curve used by the original DOS driver for notes 0..30.
VOL_TAB = (
    0x3F, 0x14, 0x10, 0x0E, 0x0C, 0x0A, 0x09, 0x08,
    0x07, 0x06, 0x06, 0x05, 0x05, 0x04, 0x04, 0x04,
    0x04, 0x04, 0x03, 0x03, 0x03, 0x03, 0x02, 0x02,
    0x02, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00,
)

# OPL2 physical channel -> YM2612 channel/register layout.
YM_CHANNEL_REG = (0, 1, 2, 0, 1, 2)      # +0xA0/+0xA4/+0xB0/+0xB4 offset
YM_CHANNEL_PORT = (0x52, 0x52, 0x52, 0x53, 0x53, 0x53)
YM_CHANNEL_CODE = (0, 1, 2, 4, 5, 6)     # 0x28 key-on selector
# YM2612 operator *register* offsets inside a channel are OP1=0x00, OP3=0x04,
# OP2=0x08, OP4=0x0C (the silicon order is OP1, OP3, OP2, OP4).  OPL's
# operator pair is (OP1, OP2), so OP2 must go to 0x08 - writing it to 0x04
# would land on YM OP3, which is a modulator in algorithms 0-3 and 4-5 and
# therefore silent in the two-operator algorithms used here.
YM_SLOT_OP1 = 0x00
YM_SLOT_OP2 = 0x08


def opl_note_hz(note: int) -> float:
    """Frequency the DOS player programs for `note` (block = note//12 + 2)."""
    block = note // 12 + 2
    fnum = OPL_FNUM_HI[note % 12] * 256 + OPL_FNUM_LO[note % 12]
    return fnum * OPL_SAMPLE_RATE / (2 ** (20 - block))


def ym_note_hz(block: int, fnum: int) -> float:
    """YM2612 frequency for a block/F-number pair."""
    return fnum * (1 << block) * YM2612_RATE / (1 << 21)


def ym_pitch(note: int) -> tuple[int, int]:
    """Best (block, fnum) on the YM2612 for an OPL2 `note` number.

    Solves for the exact DOS frequency and quantizes onto the YM grid,
    preferring F-numbers >= 512 (well inside the phase accumulator).
    """
    target = opl_note_hz(note)
    best = None
    for block in range(8):
        fnum = round(target * (1 << 21) / ((1 << block) * YM2612_RATE))
        if not 1 <= fnum <= 0x7FF:
            continue
        err = abs(math.log(ym_note_hz(block, fnum) / target))
        # Penalize thin F-numbers very slightly so ties resolve to block 0..3.
        err += 0.0002 if fnum < 512 else 0.0
        if best is None or err < best[0]:
            best = (err, block, fnum)
    if best is None:                     # absurd note numbers
        return 3, 644
    return best[1], best[2]


# --------------------------------------------------------------------------
# Rate conversion
# --------------------------------------------------------------------------
# Documented envelope period (seconds per attenuation step) for OPL and OPN.
# OPL2/OPN are the same log-attenuation family, but the OPL rate counter is
# 4-bit while the OPN rate counter is 5-bit, and one OPN "step" covers twice
# the attenuation.  The tables below are the period of a *single* OPL step
# and of a *single* OPN step; conversion therefore multiplies the OPL rate
# by roughly two and clamps into the 5-bit range.
_OPL_STEP_PERIOD_MS = (
    # rate 0 = infinite; 1..15 from the YM3812 application manual
    0.0, 114.5, 57.3, 28.6, 14.3, 7.16, 3.58, 1.79, 0.895,
    0.447, 0.224, 0.112, 0.056, 0.028, 0.014, 0.007,
)


def _opl_step_ms(rate: int) -> float:
    if rate <= 0:
        return float('inf')
    return _OPL_STEP_PERIOD_MS[min(rate, 15)]


def _ym_step_ms(rate: int) -> float:
    """Period of one OPN attenuation step (0.1875 dB at the fastest)."""
    if rate <= 0:
        return float('inf')
    # The OPN rate counter runs at one step per 3 samples at rate 31 and
    # doubles in period for every two decrements of the rate value.
    steps_per_sample = 2.0 ** ((rate - 31) / 2.0) * (2.0 / 3.0)
    return 1000.0 / (YM2612_RATE * steps_per_sample / 2.0)


def opl_rate_to_ym(rate: int, half: bool = False) -> int:
    """Convert a 4-bit OPL envelope rate to a 5-bit YM2612 envelope rate.

    `half` requests the 0.5x variant used for OPL's "double step" release
    behaviour.  Rates 1..15 map by matching envelope time constants; rate 0
    (infinitely slow / held) maps to 0.
    """
    if rate <= 0:
        return 0
    target_ms = _opl_step_ms(rate) * (2.0 if half else 1.0)
    best_rate, best_err = 0, None
    for ym_rate in range(1, 32):
        err = abs(math.log(_ym_step_ms(ym_rate) / target_ms))
        if best_err is None or err < best_err:
            best_rate, best_err = ym_rate, err
    return best_rate


# --------------------------------------------------------------------------
# MUZAX container
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# Percussion synthesis (OPL rhythm mode -> SN76489)
# --------------------------------------------------------------------------
# The five OPL2 rhythm voices are single-operator-ish noise/pulse voices with
# their own 0xA6..0xB8 pitch registers and hard-coded envelopes.  On the
# Genesis all six FM channels are needed for melody, so percussion is
# resynthesized on the SN76489.  Shapes below are in milliseconds and were
# measured from the OPL2 register behaviour (perc_patch + the fixed pitch
# registers programmed by opl_init() in src/core/audio.c).
TICK_MS = 1_000_000.0 / 180_018.0        # one 180.018 Hz music tick in ms
# --------------------------------------------------------------------------
# Percussion synthesis (OPL rhythm mode -> SN76489)
# --------------------------------------------------------------------------
# The five OPL2 rhythm voices are keyed through channels 6..10 (0xBD bits
# 0x10,0x08,0x04,0x02,0x01 = bass drum, snare, tom, cymbal, hi-hat).  On the
# 32X all six FM channels are needed for melody, so the drums are
# resynthesized on the PSG's three tone channels + noise channel.
#
# The envelopes below are *measured* from the DOS driver's own percussion
# patches (analysis/measure_perc.py renders each voice through Nuked OPL3 with
# the exact perc_patch records from src/core/audio.c) and expressed in PSG
# attenuation units (2 dB per step, 0 = loudest, 15 = silent).
# Relative acoustic level of each OPL rhythm voice, measured from the DOS
# driver's own patches (analysis/measure_perc.py): peak amplitudes are
# ~8000 for snare/tom/hat, 5800 for the cymbal and *8* for the bass drum -
# the kick is essentially a sub-audible rumble on the OPL, which is why the
# DOS soundtrack sounds so drum-light.
PERCUSSION = {
    # 0xBD bit 4 (ch6) = bass drum.  The player re-programs channel 6 with
    # the song's own instrument before keying it; measured from the DOS driver
    # (ch6 + the song's own patch + 0xBD) the kick rings at ~164 Hz with only
    # a short downward blip, so the SN76489 thump starts near 210 Hz and lands
    # on 165 Hz - below that the PSG's lowest note (109 Hz) is out of tune
    # with the original and turns the kick into a sub-bass drone.
    6: dict(psg=0, tone=True, level_db=0.0,
            start_hz=215.0, end_hz=166.0, sweep_ms=40.0,
            env=[(0, 5), (4, 2), (12, 2), (30, 3), (80, 4), (160, 4),
                 (260, 5), (420, 5), (620, 6), (900, 6), (1300, 7)],
            # A short, quiet click at the attack only: the DOS kick has a
            # ~1 ms transient, and a loud noise snap would swamp the 165 Hz
            # body (measured: it put 30% of the energy in 60-120 Hz).
            snap=dict(code=0x07, level_db=-14.0, noise_hz=3000.0,
                      env=[(0, 0), (4, 5), (9, 10), (15, 15)])),
    # 0xBD bit 3 (ch7) = snare drum: 84 ms, centroid 1374 Hz, full level.
    7: dict(psg=3, tone=False, level_db=0.0, noise_hz=3400.0,
            noise=[(0, 0x07)],
            env=[(0, 0), (4, 1), (8, 2), (12, 3), (16, 4), (24, 5),
                 (28, 6), (36, 7), (40, 8), (44, 9), (52, 10), (56, 11),
                 (64, 12), (68, 13), (72, 14), (84, 15)]),
    # 0xBD bit 2 (ch8) = tom: 163 ms, centroid 295 Hz, full level.
    8: dict(psg=1, tone=True, level_db=-1.5,
            start_hz=150.0, end_hz=95.0, sweep_ms=90.0,
            env=[(0, 0), (8, 1), (20, 2), (32, 3), (44, 4), (52, 5),
                 (64, 6), (76, 7), (88, 8), (100, 9), (108, 10), (120, 11),
                 (131, 12), (143, 13), (151, 14), (163, 15)],
            snap=dict(code=0x07, level_db=-20.0, noise_hz=2600.0,
                      env=[(0, 2), (30, 6), (60, 9), (110, 12), (150, 15)])),
    # 0xBD bit 1 (ch9) = top cymbal: 645 ms, centroid 1790 Hz, -3 dB.
    9: dict(psg=3, tone=False, level_db=-3.0, noise_hz=5200.0,
            noise=[(0, 0x07)],
            env=[(0, 0), (28, 1), (72, 2), (116, 3), (156, 4), (200, 5),
                 (247, 6), (291, 7), (335, 8), (378, 9), (422, 10), (466, 11),
                 (510, 12), (554, 13), (597, 14), (645, 15)]),
    # 0xBD bit 0 (ch10) = hi-hat: 167 ms, centroid 1770 Hz, full level.
    10: dict(psg=3, tone=False, level_db=-1.0, noise_hz=7800.0,
             noise=[(0, 0x07)],
             env=[(0, 0), (8, 1), (12, 2), (20, 3), (32, 4), (44, 5),
                  (56, 6), (64, 7), (80, 8), (88, 9), (96, 10), (112, 11),
                  (124, 12), (136, 13), (144, 14), (167, 15)]),
}

# A channel's percussion voice is shared by several OPL rhythm channels; the
# OPL scores key them independently, so the PSG voice is restarted whenever
# the OPL source keys one of its owners.
PERCUSSION_STATE = {}
PSG_VOICE_FREQ = {0: 1, 1: 1}            # tone voices carry a pitch sweep


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
        # PSG state shadows so the stream stays minimal and deterministic.
        self.psg_volume_state = [15, 15, 15, 15]
        self.psg_period_state = [0x3ff, 0x3ff, 0x3ff, 0x3ff]
        self.psg_noise_state = 0xE4
        self.tone_period = [1023, 1023, 1023]

    # -- raw VGM emitters ---------------------------------------------------
    def ym(self, channel: int, register: int, value: int) -> None:
        self.stream.extend((YM_CHANNEL_PORT[channel], register & 0xFF,
                            value & 0xFF))

    def ym_global(self, register: int, value: int) -> None:
        self.stream.extend((0x52, register & 0xFF, value & 0xFF))

    def psg_write(self, value: int) -> None:
        self.stream.extend((0x50, value & 0xFF))

    def psg_volume(self, voice: int, attenuation: int) -> None:
        attenuation = max(0, min(15, int(round(attenuation)) + PSG_GLOBAL_ATTENUATION))
        if self.psg_volume_state[voice] == attenuation:
            return
        self.psg_volume_state[voice] = attenuation
        self.psg_write(0x90 | (voice << 5) | attenuation)

    def psg_tone(self, voice: int, frequency: float) -> None:
        period = max(1, min(1023, round(PSG_CLOCK / (32.0 * max(frequency, 1e-3)))))
        if self.psg_period_state[voice] == period:
            return
        # a one-step period change is inaudible on a drum; coalescing it saves
        # two bytes per write in the large tracks
        if abs(self.psg_period_state[voice] - period) < PSG_MIN_PERIOD_STEP:
            return
        self.psg_period_state[voice] = period
        self.tone_period[voice] = period
        # The latch byte and its data byte must be adjacent: anything written
        # between them is consumed as the period's high bits by the chip.
        self.psg_write(0x80 | (voice << 5) | (period & 0x0F))
        self.psg_write((period >> 4) & 0x3F)

    def psg_noise_free(self) -> bool:
        """True when the noise channel is not running a percussion envelope."""
        return self.noise_owner is None or self.noise_owner_until <= self.tick

    def psg_noise_rate(self, hz: float) -> None:
        """Set the pitch of the noise source.

        On the SN76489 the noise generator's LFSR is clocked by channel 2's
        tone divider (rate bits 00/01/10 select ch2/ch0/ch1, bit 2 = white),
        so the "noise pitch" is that channel's period.  Channel 2 is therefore
        never played as a melodic voice.
        """
        self.psg_tone(2, hz)

    def psg_noise(self, code: int, restart: bool = True) -> None:
        """Select a noise mode.  The hardware restarts the LFSR on every write,
        so re-writing the same code is only done for an intentional restart
        (a new drum hit); the timed code changes inside one hit are skipped
        when the mode is unchanged."""
        code = max(0, min(7, code))
        if self.psg_noise_state == (0xE0 | code) and not restart:
            return
        self.psg_noise_state = 0xE0 | code
        self.psg_write(self.psg_noise_state)

    # -- operator programming ----------------------------------------------
    def _operator(self, channel: int, slot: int, opl: bytes,
                  total_level: int | None = None,
                  ar_override: int | None = None,
                  dr_override: int | None = None,
                  sr_override: int | None = None,
                  rr_override: int | None = None,
                  carrier: bool = True) -> None:
        """Translate one 5-byte OPL operator record to a YM2612 operator."""
        am_vib_egt_ksr_mul, tl, ar_dr, sl_rr, wave = opl
        base = YM_CHANNEL_REG[channel] + slot
        # 0x30: DT/MULTI.  OPL MULTI is the low nibble and means the same on
        # both chips; OPL has no detune, so DT1 stays 0.  (KSR is not DT2 on
        # the YM2612 - DT2 is bit 7 of 0x30 on the OPN2, and leaving it 0
        # keeps the modulator/carrier ratio exactly as the DOS patch has it.)
        self.ym(channel, 0x30 + base,
                (am_vib_egt_ksr_mul & 0x0F) | (0x80 if YM_DT2 else 0))
        # 0x40: TL.  Same ~0.75 dB per step on both chips (measured), so the
        # OPL 6-bit value and the driver's vol_tab additions are copied 1:1
        # into the YM's 7-bit field, mixed with any per-channel bias.
        level = (self._total_level(tl + YM_MOD_TL_OFFSET) if total_level is None
                 else total_level)
        self.ym(channel, 0x40 + base, level)
        # 0x50: KS/AR.  On the OPN2 the KSR bits are inverted: KSR=0 means
        # "fast" key scaling (the hardware default) and KSR=3 the slowest.
        # Audible patches map OPL KSR=1 -> YM KSR=0 and OPL KSR=0 -> KSR=3.
        ar = opl_rate_to_ym(ar_dr >> 4) if ar_override is None else ar_override
        ksr = YM_KSR_FAST if am_vib_egt_ksr_mul & 0x10 else YM_KSR_SLOW
        self.ym(channel, 0x50 + base, (ksr << 6) | (ar & 0x1F))
        # 0x60: AM/DR.  AMS (the OPN2's tremolo sensitivity) is a per-channel
        # control on YM2612 (register 0xB4), not a per-operator bit.
        dr = opl_rate_to_ym(ar_dr & 0x0F) if dr_override is None else dr_override
        self.ym(channel, 0x60 + base, dr & 0x1F)
        # 0x70: D2R/SR.  OPL bit 5 (EGT) decides whether the envelope holds at
        # the sustain level or keeps decaying.
        if sr_override is not None:
            sr = sr_override
        elif am_vib_egt_ksr_mul & 0x20:
            # EGT=1 ("sustaining"): the OPL envelope stops at the sustain
            # level (Nuked OPL3 leaves reg_rate at 0 in the sustain phase),
            # which is exactly a YM2612 sustain rate of 0.
            sr = 0
        else:
            # EGT=0 ("non-sustaining"): after decay reaches the sustain level
            # the OPL keeps fading *at the release rate* - see Nuked OPL3
            # opl3.c, case envelope_gen_num_sustain: reg_rate = reg_rr.
            # This slow brightness/level drift is a big part of the OPL
            # sound, so the YM sustain rate mirrors the OPL release rate.
            sr = opl_rate_to_ym(sl_rr & 0x0F)
        self.ym(channel, 0x70 + base, sr & 0x1F)
        # 0x80: SL/RR (handled by _operator_sl_rr).
        rr = opl_rate_to_ym(sl_rr & 0x0F) if rr_override is None else rr_override
        # OPL SL and YM2612 SL are both 3 dB per step over a 4-bit field, with
        # one quirk: on the YM2612 code 15 is full silence (93 dB), on the OPL
        # it is 45 dB.  The DOS patches only use SL 0..13, so copy 1:1 and let
        # 15 mean "silence" on both.
        self.ym(channel, 0x80 + base, (sl_rr & 0xF0) | (rr & 0x0F))
        # 0x90: SSG-EG off.  The MUZAX instruments all use the plain sine
        # waveform (wave select register is 0 in every record), so the
        # waveform-select field of 0xE0 has no equivalent to carry.
        self.ym(channel, 0x90 + base, 0)

    @staticmethod
    def _total_level(opl_value: int, extra: int = 0) -> int:
        return max(0, min(127, (opl_value & 0x3F) + extra))

    # -- channel state ------------------------------------------------------
    def key_off(self, channel: int) -> None:
        if channel < 6:
            self.ym_global(0x28, YM_CHANNEL_CODE[channel])
            self._lfo_state[channel] = False
        else:
            self._cancel_psg(channel)

    def program_instrument(self, channel: int, number: int) -> None:
        if channel >= 11 or number >= self.ninst:
            return
        self.channel_instrument[channel] = number
        if channel >= 6:
            return
        rec = self.instruments[number * 16:number * 16 + 11]
        op1, op2 = rec[0:5], rec[5:10]
        additive = bool(rec[10] & 1)

        if additive:
            # OPL2 algorithm 1: both operators are carriers summing at the
            # output -> YM2612 algorithm 7 (all four carriers), OP3/OP4 muted.
            self._operator(channel, YM_SLOT_OP1, op1,
                           self._total_level(op1[1], YM_MUSIC_TL_BIAS),
                           carrier=True)
        else:
            # OPL2 algorithm 0: OP1 modulates OP2, which is the carrier.
            self._operator(channel, YM_SLOT_OP1, op1, carrier=False)
        self._operator(channel, YM_SLOT_OP2, op2,
                       self._total_level(op2[1], YM_MUSIC_TL_BIAS),
                       carrier=True)
        # YM OP3 (0x04) and OP4 (0x0C) are the unused pair; mute them hard so
        # they cannot leak into the carrier chain or the adder.
        silent = bytes((1, 0x7F, 0x00, 0x0F, 0))
        self._operator(channel, 0x04, silent, 127)
        self._operator(channel, 0x0C, silent, 127)
        feedback = max(0, min(7, int(round(((rec[10] >> 1) & 7) * YM_FEEDBACK_SCALE))))
        algorithm = 7 if additive else 4
        reg = YM_CHANNEL_REG[channel]
        self.ym(channel, 0xB0 + reg, (feedback << 3) | algorithm)
        self.ym(channel, 0xB4 + reg, 0xC0)      # centred stereo
        # Key the channel off *after* the new patch is in place: key_off()
        # mutes all four slots (TL 127), so calling it first would erase the
        # carrier's total level written above and leave the channel silent.
        self.key_off(channel)
        # OPL tremolo/vibrato depth: OPL applies AM at 3.7 Hz and VIB at
        # 6.1 Hz with fixed depth.  YM2612 LFO is per-chip; use the closest
        # available rate (3 -> ~5.9 Hz) when any operator in the channel
        # enables AM/VIB via the patch bits.
        vib = (op1[0] & 0xC0) or (op2[0] & 0xC0)
        self._lfo_state[channel] = bool(vib)
        self._channel_lfo_sensitivity[channel] = (
            (0x14 if (op1[0] & 0x40) or (op2[0] & 0x40) else 0) |
            (0x03 if vib else 0))

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
        # The OPL driver adds vol_tab[v] straight onto the carrier's 0x40
        # attenuation (audio.c set_volume); the same table works on the YM
        # because a TL step is the same ~0.75 dB on both chips.
        extra = VOL_TAB[volume] + YM_MUSIC_TL_BIAS
        reg = YM_CHANNEL_REG[channel]
        self.ym(channel, 0x40 + reg + YM_SLOT_OP2,
                self._total_level(rec[6], extra))
        if additive:
            self.ym(channel, 0x40 + reg + YM_SLOT_OP1,
                    self._total_level(rec[1], extra))

    # -- percussion ---------------------------------------------------------
    def _cancel_psg(self, channel: int) -> None:
        spec = PERCUSSION.get(channel)
        if not spec:
            return
        voice = spec["psg"]
        env = self.psg[voice]
        env.generation += 1
        env.changes.clear()
        self.psg_track[voice] = None
        self.psg_volume(voice, 15)

    def _schedule_psg(self, voice: int, start_attenuation: int, shape,
                      step: int = 3) -> None:
        """Queue one percussion envelope.

        ``shape`` is a list of (milliseconds, attenuation).  Values only move
        one 2 dB step at a time, so keeping every ``step``-th point halves the
        stream for the fast drum transients without audibly changing them.
        """
        env = self.psg[voice]
        env.generation += 1
        generation = env.generation
        env.changes.clear()
        self.psg_volume(voice, start_attenuation)
        previous = None
        for index, (relative_tick, attenuation) in enumerate(shape):
            if index % step:
                continue
            level = max(0, min(15, int(round(attenuation))))
            if previous is not None and level == previous:
                continue
            previous = level
            env.changes.append((self.tick + int(relative_tick), level,
                                generation))

    @staticmethod
    def _ms_to_ticks(ms: float) -> int:
        return int(round(ms / TICK_MS))

    def note_on(self, channel: int, note: int) -> None:
        if channel < 6:
            self.key_off(channel)
            block, fnum = ym_pitch(note)
            reg = YM_CHANNEL_REG[channel]
            self.ym(channel, 0xA4 + reg, (block << 3) | (fnum >> 8))
            self.ym(channel, 0xA0 + reg, fnum & 0xFF)
            self.ym_global(0x28, 0xF0 | YM_CHANNEL_CODE[channel])
            return

        spec = PERCUSSION.get(channel)
        if not spec:
            return
        voice = spec["psg"]
        # A shared voice restarts from the brightest shape; louder channel
        # volumes start the envelope earlier, like the OPL's own volume.
        # Patch volume (0..30) attenuates the hit; the measured voice level
        # sets the floor.  PSG attenuation steps are 2 dB.
        base = max(0, min(15, int(round(-spec.get("level_db", 0.0) / 2.0))))
        start = max(0, min(15, base + (30 - self.channel_volume[channel]) // 4))
        if spec["tone"]:
            self.psg_tone(voice, spec["start_hz"])
            track = dict(kind="tone", voice=voice, t0=self.tick,
                         start_hz=spec["start_hz"], end_hz=spec["end_hz"],
                         sweep_ticks=max(1, self._ms_to_ticks(spec["sweep_ms"])),
                         noise=None)
            # The OPL drums are not pure tones: the tom rings with a ~1 kHz
            # component and the kick has a click.  Put that attack on the
            # noise channel so the tone channel keeps its pitch.
            snap = spec.get("snap")
            if snap and self.psg_noise_free():
                self.psg_noise_rate(snap.get("noise_hz", 4000.0))
                self.psg_noise(snap["code"])
                self.noise_owner = channel
                self.noise_owner_until = self.tick + max(
                    1, self._ms_to_ticks(snap["env"][-1][0]))
                snap_base = max(0, min(15, int(round(
                    -snap.get("level_db", 0.0) / 2.0))))
                self._schedule_psg(3, snap_base, [
                    (self._ms_to_ticks(ms), snap_base + level)
                    for ms, level in snap["env"]], step=3)
        else:
            self.psg_noise_rate(spec.get("noise_hz", 4000.0))
            self.psg_noise(spec["noise"][0][1])
            self.noise_owner = channel
            self.noise_owner_until = self.tick + self._ms_to_ticks(spec["env"][-1][0])
            # noise_end is when the *audible* hit ends (the envelope), not
            # when the last noise-code change happens - the code list holds
            # (milliseconds, code) pairs and its tail is usually (0, code).
            track = dict(kind="noise", voice=voice, t0=self.tick,
                         noise=[(self._ms_to_ticks(ms), code)
                                for ms, code in spec["noise"]],
                         noise_end=self._ms_to_ticks(spec["env"][-1][0]))
        self.psg_track[voice] = track
        shape = [(self._ms_to_ticks(ms), start + level)
                 for ms, level in spec["env"]]
        self._schedule_psg(voice, start, shape, step=spec.get("env_step", 3))

    # -- time ---------------------------------------------------------------
    def _next_event_tick(self, end_tick: int) -> int:
        result = end_tick
        if not PSG_ENVELOPE_SPLITS:
            return result
        for env in self.psg:
            if env.changes:
                result = min(result, env.changes[0][0])
        for track in self.psg_track:
            if track is None:
                continue
            if track["kind"] == "tone":
                # the OPL drums sweep their pitch continuously; sampling the
                # sweep every few ticks keeps the stream small while staying
                # well inside the audible accuracy of a drum hit
                next_tick = self.tick + SWEEP_SAMPLE_TICKS
                if next_tick < track["t0"] + track["sweep_ticks"]:
                    result = min(result, next_tick)
            elif len({code for _, code in track["noise"]}) > 1:
                # only schedule the hit's internal code changes when the noise
                # colour actually changes during the hit
                for at_tick, _code in track["noise"]:
                    if at_tick > self.tick:
                        result = min(result, at_tick)
                        break
        return result

    def _apply_events(self) -> None:
        for voice, env in enumerate(self.psg):
            while env.changes and env.changes[0][0] <= self.tick:
                _, attenuation, generation = env.changes.pop(0)
                if generation == env.generation:
                    self.psg_volume(voice, attenuation)
        for voice, track in enumerate(self.psg_track):
            if track is None:
                continue
            if track["kind"] == "tone":
                elapsed = self.tick - track["t0"]
                if elapsed > track["sweep_ticks"] + 1:
                    self.psg_track[voice] = None
                    continue
                if elapsed < 0:
                    continue          # track scheduled for a later tick
                frac = min(1.0, elapsed / track["sweep_ticks"])
                # OPL pitch sweeps are logarithmic (it adds to the block
                # number): interpolate in log-frequency.
                hz = track["start_hz"] * (track["end_hz"] / track["start_hz"]) ** frac
                self.psg_tone(voice, hz)
            else:
                elapsed = self.tick - track["t0"]
                if elapsed < 0:
                    continue
                if elapsed > track["noise_end"] + 1:
                    self.psg_track[voice] = None
                    continue
                if len({code for _, code in track["noise"]}) > 1:
                    for at_tick, code in track["noise"]:
                        if at_tick <= self.tick:
                            self.psg_noise(code, restart=False)

    def _emit_sample_wait(self, samples: int) -> None:
        while samples:
            part = min(samples, 0xFFFF)
            self.stream.extend((0x61, part & 0xFF, part >> 8))
            samples -= part

    def wait_ticks(self, ticks: int) -> None:
        end_tick = self.tick + ticks
        while self.tick < end_tick:
            next_tick = self._next_event_tick(end_tick)
            if next_tick <= self.tick:
                self._apply_events()
                continue
            previous_samples = self.sample_position
            self.tick = next_tick
            self.sample_position = round(
                self.tick * VGM_RATE * 6_628 / 1_193_182)
            self._emit_sample_wait(self.sample_position - previous_samples)
            self._apply_events()

    # -- stream -------------------------------------------------------------
    def initialize_stream(self) -> None:
        self._lfo_state = [False] * 6
        self._channel_lfo_sensitivity = [0] * 6
        self.psg_track = [None] * 4
        self.noise_owner = None
        self.noise_owner_until = 0
        self.ym_global(0x22, 0x00)          # LFO off; enabled per channel
        self.ym_global(0x2B, 0x00)          # DAC off; channel 6 stays FM
        for channel in range(6):
            self.key_off(channel)
            self.ym(channel, 0xB4 + YM_CHANNEL_REG[channel], 0xC0)
            for slot in (0x00, 0x04, 0x08, 0x0C):
                self.ym(channel, 0x40 + YM_CHANNEL_REG[channel] + slot, 127)
        for voice in range(4):
            self.psg_volume(voice, 15)

    def convert(self) -> bytes:
        self.initialize_stream()
        pos = 0
        while pos + 1 < len(self.events):
            word = self.events[pos] | (self.events[pos + 1] << 8)
            pos += 2
            self.command_count += 1
            operation = word & 7
            channel = (word >> 4) & 0x0F
            parameter = word >> 8

            if operation == 0:
                # The original player stores the parameter then spends that
                # many further IRQs decrementing it: delay N means N+1 ticks.
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
        struct.pack_into("<I", header, 0x34, 0x0C)   # data starts at 0x40
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
