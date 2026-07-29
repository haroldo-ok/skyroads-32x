#!/usr/bin/env python3
"""Headless PicoDrive/libretro end-to-end tests for the SkyRoads 32X ROM.

The test boots the real cartridge image, drives controller inputs through the
emulator, and checks rendered frames at boot, main menu, road menu and active
gameplay. It intentionally tests visible pixels rather than internal C state,
so a startup deadlock or VDP regression cannot pass as a black screen.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import math
import struct
import sys
import zlib
from pathlib import Path

# libretro constants used by this minimal headless frontend.
RETRO_ENVIRONMENT_GET_CAN_DUPE = 3
RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY = 9
RETRO_ENVIRONMENT_SET_PIXEL_FORMAT = 10
RETRO_ENVIRONMENT_GET_VARIABLE = 15
RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE = 17
RETRO_ENVIRONMENT_SET_SUPPORT_NO_GAME = 18
RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY = 31
RETRO_ENVIRONMENT_SET_GEOMETRY = 37
RETRO_ENVIRONMENT_GET_LANGUAGE = 39
RETRO_ENVIRONMENT_SET_SERIALIZATION_QUIRKS = 44
RETRO_ENVIRONMENT_GET_INPUT_BITMASKS = 51
RETRO_ENVIRONMENT_GET_CORE_OPTIONS_VERSION = 52
RETRO_ENVIRONMENT_GET_MESSAGE_INTERFACE_VERSION = 59
RETRO_ENVIRONMENT_GET_JIT_CAPABLE = 74
RETRO_DEVICE_JOYPAD = 1
RETRO_DEVICE_ID_JOYPAD_START = 3
RETRO_DEVICE_ID_JOYPAD_UP = 4
RETRO_DEVICE_ID_JOYPAD_DOWN = 5
RETRO_DEVICE_ID_JOYPAD_LEFT = 6
RETRO_DEVICE_ID_JOYPAD_RIGHT = 7
RETRO_DEVICE_ID_JOYPAD_A = 8
RETRO_DEVICE_ID_JOYPAD_X = 9
RETRO_DEVICE_ID_JOYPAD_Y = 1


class GameInfo(C.Structure):
    _fields_ = [("path", C.c_char_p), ("data", C.c_void_p),
                ("size", C.c_size_t), ("meta", C.c_char_p)]


class Geometry(C.Structure):
    _fields_ = [("base_width", C.c_uint), ("base_height", C.c_uint),
                ("max_width", C.c_uint), ("max_height", C.c_uint),
                ("aspect_ratio", C.c_float)]


class Timing(C.Structure):
    _fields_ = [("fps", C.c_double), ("sample_rate", C.c_double)]


class AVInfo(C.Structure):
    _fields_ = [("geometry", Geometry), ("timing", Timing)]


class HeadlessFrontend:
    ENV_CB = C.CFUNCTYPE(C.c_bool, C.c_uint, C.c_void_p)
    VIDEO_CB = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_size_t)
    AUDIO_CB = C.CFUNCTYPE(None, C.c_int16, C.c_int16)
    AUDIO_BATCH_CB = C.CFUNCTYPE(C.c_size_t, C.POINTER(C.c_int16), C.c_size_t)
    INPUT_POLL_CB = C.CFUNCTYPE(None)
    INPUT_STATE_CB = C.CFUNCTYPE(C.c_int16, C.c_uint, C.c_uint, C.c_uint, C.c_uint)

    def __init__(self, core: Path, system_dir: Path):
        self.lib = C.CDLL(str(core.resolve()))
        self.system_dir_buf = C.create_string_buffer(str(system_dir.resolve()).encode())
        self.pixel_format = 0  # libretro default: 0RGB1555
        self.pad_mask = 0
        self.frame = None
        self.video_calls = 0
        self.audio_frames = 0
        self.audio_nonzero = 0
        self.audio_peak = 0
        self.audio_energy = 0
        self.audio_samples = []
        self.last_audio_samples = []

        self.env_cb = self.ENV_CB(self._environment)
        self.video_cb = self.VIDEO_CB(self._video)
        self.audio_cb = self.AUDIO_CB(self._audio_sample)
        self.audio_batch_cb = self.AUDIO_BATCH_CB(self._audio_batch)
        self.input_poll_cb = self.INPUT_POLL_CB(lambda: None)
        self.input_state_cb = self.INPUT_STATE_CB(self._input_state)

        self.lib.retro_set_environment.argtypes = [self.ENV_CB]
        self.lib.retro_set_video_refresh.argtypes = [self.VIDEO_CB]
        self.lib.retro_set_audio_sample.argtypes = [self.AUDIO_CB]
        self.lib.retro_set_audio_sample_batch.argtypes = [self.AUDIO_BATCH_CB]
        self.lib.retro_set_input_poll.argtypes = [self.INPUT_POLL_CB]
        self.lib.retro_set_input_state.argtypes = [self.INPUT_STATE_CB]
        self.lib.retro_load_game.argtypes = [C.POINTER(GameInfo)]
        self.lib.retro_load_game.restype = C.c_bool
        self.lib.retro_get_system_av_info.argtypes = [C.POINTER(AVInfo)]

        self.lib.retro_set_environment(self.env_cb)
        self.lib.retro_set_video_refresh(self.video_cb)
        self.lib.retro_set_audio_sample(self.audio_cb)
        self.lib.retro_set_audio_sample_batch(self.audio_batch_cb)
        self.lib.retro_set_input_poll(self.input_poll_cb)
        self.lib.retro_set_input_state(self.input_state_cb)
        self.lib.retro_init()

    def _set_bool(self, ptr: int, value: bool) -> bool:
        if not ptr:
            return False
        C.cast(ptr, C.POINTER(C.c_bool))[0] = value
        return True

    def _set_uint(self, ptr: int, value: int) -> bool:
        C.cast(ptr, C.POINTER(C.c_uint))[0] = value
        return True

    def _set_string(self, ptr: int, buf) -> bool:
        C.cast(ptr, C.POINTER(C.c_char_p))[0] = C.cast(buf, C.c_char_p)
        return True

    def _environment(self, cmd: int, data: int) -> bool:
        # Strip the experimental/private command marker if present.
        cmd &= 0xFFFF
        if cmd == RETRO_ENVIRONMENT_SET_PIXEL_FORMAT:
            self.pixel_format = C.cast(data, C.POINTER(C.c_int))[0]
            return self.pixel_format in (0, 1, 2)
        if cmd == RETRO_ENVIRONMENT_GET_CAN_DUPE:
            return self._set_bool(data, True)
        if cmd in (RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY,
                   RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY):
            return self._set_string(data, self.system_dir_buf)
        if cmd == RETRO_ENVIRONMENT_GET_VARIABLE:
            return False  # use every core option's declared default
        if cmd == RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE:
            return self._set_bool(data, False)
        if cmd == RETRO_ENVIRONMENT_GET_INPUT_BITMASKS:
            return self._set_bool(data, False)
        if cmd in (RETRO_ENVIRONMENT_GET_CORE_OPTIONS_VERSION,
                   RETRO_ENVIRONMENT_GET_MESSAGE_INTERFACE_VERSION):
            return self._set_uint(data, 0)
        if cmd == RETRO_ENVIRONMENT_GET_LANGUAGE:
            return self._set_uint(data, 0)  # English
        if cmd == RETRO_ENVIRONMENT_GET_JIT_CAPABLE:
            return self._set_bool(data, True)
        # SET_* notifications that don't require frontend work.
        if cmd in (RETRO_ENVIRONMENT_SET_SUPPORT_NO_GAME,
                   RETRO_ENVIRONMENT_SET_GEOMETRY,
                   RETRO_ENVIRONMENT_SET_SERIALIZATION_QUIRKS,
                   11, 16, 35, 36, 55, 67):
            return True
        return False

    def _video(self, data: int, width: int, height: int, pitch: int) -> None:
        self.video_calls += 1
        if not data or data == C.c_void_p(-1).value:
            return
        # Preserve rows without any frontend padding.
        bpp = 4 if self.pixel_format == 1 else 2
        src = C.cast(data, C.POINTER(C.c_ubyte))
        packed = bytearray(width * height * bpp)
        row_bytes = width * bpp
        for y in range(height):
            packed[y * row_bytes:(y + 1) * row_bytes] = bytes(src[y * pitch:y * pitch + row_bytes])
        self.frame = (width, height, bpp, bytes(packed))

    def _record_audio(self, left: int, right: int) -> None:
        self.audio_frames += 1
        if left or right:
            self.audio_nonzero += 1
        peak = max(abs(left), abs(right))
        if peak > self.audio_peak:
            self.audio_peak = peak
        self.audio_energy += abs(left) + abs(right)
        self.audio_samples.append((left + right) // 2)

    def _audio_sample(self, left: int, right: int) -> None:
        self._record_audio(left, right)

    def _audio_batch(self, data, frames: int) -> int:
        for i in range(frames):
            self._record_audio(int(data[i * 2]), int(data[i * 2 + 1]))
        return frames

    def take_audio_stats(self) -> dict:
        frames = self.audio_frames
        stats = {
            "frames": frames,
            "nonzero_ratio": round(self.audio_nonzero / frames, 5) if frames else 0,
            "peak": self.audio_peak,
            "mean_abs": round(self.audio_energy / (frames * 2), 2) if frames else 0,
        }
        self.audio_frames = 0
        self.audio_nonzero = 0
        self.audio_peak = 0
        self.audio_energy = 0
        self.last_audio_samples = self.audio_samples
        self.audio_samples = []
        return stats

    def _input_state(self, port: int, device: int, index: int, ident: int) -> int:
        if port != 0 or device != RETRO_DEVICE_JOYPAD or index != 0:
            return 0
        return 1 if (self.pad_mask & (1 << ident)) else 0

    def load(self, rom: Path) -> AVInfo:
        raw = rom.read_bytes()
        self.rom_buf = C.create_string_buffer(raw)
        path_buf = C.create_string_buffer(str(rom.resolve()).encode())
        self.rom_path_buf = path_buf
        info = GameInfo(C.cast(path_buf, C.c_char_p), C.cast(self.rom_buf, C.c_void_p),
                        len(raw), None)
        if not self.lib.retro_load_game(C.byref(info)):
            raise RuntimeError("PicoDrive rejected the 32X ROM")
        av = AVInfo()
        self.lib.retro_get_system_av_info(C.byref(av))
        return av

    def run(self) -> None:
        self.lib.retro_run()

    def close(self) -> None:
        self.lib.retro_unload_game()
        self.lib.retro_deinit()


def rgb_pixels(frame):
    w, h, bpp, raw = frame
    rgb = bytearray(w * h * 3)
    if bpp == 4:  # XRGB8888 in host endian
        for i, (v,) in enumerate(struct.iter_unpack("=I", raw)):
            rgb[i * 3:i * 3 + 3] = bytes(((v >> 16) & 255, (v >> 8) & 255, v & 255))
    else:
        # RGB565 or 0RGB1555, both host-endian libretro formats.
        for i, (v,) in enumerate(struct.iter_unpack("=H", raw)):
            if FRONTEND.pixel_format == 2:
                r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
                rgb[i * 3:i * 3 + 3] = bytes((r * 255 // 31, g * 255 // 63, b * 255 // 31))
            else:
                r, g, b = (v >> 10) & 31, (v >> 5) & 31, v & 31
                rgb[i * 3:i * 3 + 3] = bytes((r * 255 // 31, g * 255 // 31, b * 255 // 31))
    return w, h, bytes(rgb)


def frame_stats(frame) -> dict:
    w, h, rgb = rgb_pixels(frame)
    pixels = [rgb[i:i + 3] for i in range(0, len(rgb), 3)]
    counts = {}
    for pixel in pixels:
        counts[pixel] = counts.get(pixel, 0) + 1
    dominant = max(counts.values()) / len(pixels)
    return {"width": w, "height": h, "crc32": f"{zlib.crc32(rgb):08x}",
            "visible_ratio": round(1.0 - dominant, 4), "colors": len(counts)}


def frame_difference(a, b) -> float:
    wa, ha, ra = rgb_pixels(a)
    wb, hb, rb = rgb_pixels(b)
    if (wa, ha) != (wb, hb):
        return 1.0
    changed = sum(ra[i:i + 3] != rb[i:i + 3] for i in range(0, len(ra), 3))
    return changed / (wa * ha)


def crop_stats(frame, x0: int, y0: int, x1: int, y1: int) -> dict:
    width, height, rgb = rgb_pixels(frame)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("crop is outside the video frame")
    counts = {}
    for y in range(y0, y1):
        for x in range(x0, x1):
            pixel = rgb[(y * width + x) * 3:(y * width + x + 1) * 3]
            counts[pixel] = counts.get(pixel, 0) + 1
    count = (x1 - x0) * (y1 - y0)
    dominant = max(counts.values()) / count
    return {"colors": len(counts), "visible_ratio": round(1.0 - dominant, 4)}


def write_ppm(path: Path, frame) -> None:
    w, h, rgb = rgb_pixels(frame)
    path.write_bytes(f"P6\n{w} {h}\n255\n".encode() + rgb)


def tap(frontend: HeadlessFrontend, button: int, frames: int = 3) -> None:
    frontend.pad_mask = 1 << button
    for _ in range(frames):
        frontend.run()
    frontend.pad_mask = 0


def check_visible(name: str, stats: dict) -> None:
    if stats["visible_ratio"] < 0.08:
        raise AssertionError(f"{name} is black/blank: {stats}")
    if stats["colors"] < 12:
        raise AssertionError(f"{name} has too few colors: {stats}")


def pcm_correlation(samples: list[int], pcm: bytes, source_rate: int,
                    output_rate: int = 44100) -> float:
    """Find a resampled unsigned-PCM signature inside mixed Genesis audio.

    A sparse normalized correlation is enough to distinguish the original
    landing sample from changing YM2612 music without adding NumPy as a test
    dependency.
    """
    if not samples or not pcm or source_rate <= 0:
        return 0.0
    stride = 32
    duration = len(pcm) * output_rate // source_rate
    reference = []
    for output_pos in range(0, duration, stride):
        source_pos = min(len(pcm) - 1, output_pos * source_rate // output_rate)
        reference.append(pcm[source_pos] - 128)
    ref_mean = sum(reference) / len(reference)
    reference = [value - ref_mean for value in reference]
    ref_energy = sum(value * value for value in reference)
    if ref_energy == 0:
        return 0.0

    best = 0.0
    last_start = len(samples) - duration
    for start in range(0, max(0, last_start) + 1, 16):
        dot = total = energy = 0.0
        for index, ref in enumerate(reference):
            value = samples[start + index * stride]
            dot += value * ref
            total += value
            energy += value * value
        count = len(reference)
        energy -= total * total / count
        if energy > 0:
            correlation = abs(dot) / math.sqrt(ref_energy * energy)
            if correlation > best:
                best = correlation
    return round(best, 4)


def main() -> int:
    global FRONTEND
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", required=True, type=Path,
                    help="PicoDrive libretro shared library")
    ap.add_argument("--rom", required=True, type=Path)
    ap.add_argument("--artifacts", type=Path, default=Path("test-artifacts/emulator"))
    ns = ap.parse_args()
    ns.artifacts.mkdir(parents=True, exist_ok=True)

    FRONTEND = HeadlessFrontend(ns.core, ns.artifacts)
    av = FRONTEND.load(ns.rom)
    captures = {}
    capture_frames = {}
    audio_captures = {}
    sample_captures = {}

    def run_to(target: int, current: int) -> int:
        while current < target:
            FRONTEND.run()
            current += 1
        return current

    def capture(name: str):
        if FRONTEND.frame is None:
            raise AssertionError(f"{name}: emulator produced no video callback")
        captures[name] = frame_stats(FRONTEND.frame)
        capture_frames[name] = FRONTEND.frame
        audio_captures[name] = FRONTEND.take_audio_stats()
        if name in ("gameplay_accelerating", "gameplay_sfx_tail"):
            sample_captures[name] = FRONTEND.last_audio_samples
        write_ppm(ns.artifacts / f"{name}.ppm", FRONTEND.frame)

    frame = 0
    frame = run_to(100, frame)
    capture("boot_intro")

    # Intro -> main menu. START is accepted as Enter/Pause by the 32X shell.
    tap(FRONTEND, RETRO_DEVICE_ID_JOYPAD_START); frame += 3
    frame = run_to(245, frame)
    capture("main_menu")

    # Main menu Start -> road selection.
    tap(FRONTEND, RETRO_DEVICE_ID_JOYPAD_START); frame += 3
    frame = run_to(390, frame)
    capture("road_menu")

    # Regression for the 6-button-pad bug: extended button bits used to
    # mirror U/D/L/R, so every d-pad press was also interpreted as Jump/Enter.
    # DOWN must only move the highlight, not launch the selected road.
    tap(FRONTEND, RETRO_DEVICE_ID_JOYPAD_DOWN); frame += 3
    frame = run_to(480, frame)
    capture("road_menu_level2")
    menu_delta = frame_difference(capture_frames["road_menu"],
                                  capture_frames["road_menu_level2"])
    if not (0.0001 < menu_delta < 0.08):
        raise AssertionError(f"DOWN left the road menu or did not select level 2: delta={menu_delta:.4f}")

    # UP must return to level 1 and reproduce the original static menu.
    tap(FRONTEND, RETRO_DEVICE_ID_JOYPAD_UP); frame += 3
    frame = run_to(500, frame)
    capture("road_menu_level1_again")
    if captures["road_menu_level1_again"]["crc32"] != captures["road_menu"]["crc32"]:
        raise AssertionError("UP did not return the road selector to level 1")

    # RIGHT must move to the right column (road 16), and LEFT must return.
    tap(FRONTEND, RETRO_DEVICE_ID_JOYPAD_RIGHT); frame += 3
    frame = run_to(590, frame)
    capture("road_menu_right_column")
    right_delta = frame_difference(capture_frames["road_menu"],
                                   capture_frames["road_menu_right_column"])
    if not (0.0001 < right_delta < 0.08):
        raise AssertionError(f"RIGHT left the road menu or did not change columns: delta={right_delta:.4f}")
    tap(FRONTEND, RETRO_DEVICE_ID_JOYPAD_LEFT); frame += 3
    frame = run_to(610, frame)
    capture("road_menu_left_column_again")
    if captures["road_menu_left_column_again"]["crc32"] != captures["road_menu"]["crc32"]:
        raise AssertionError("LEFT did not return the road selector to level 1")

    # START now launches road 1; accelerate and jump with A.
    tap(FRONTEND, RETRO_DEVICE_ID_JOYPAD_START); frame += 3
    frame = run_to(720, frame)
    FRONTEND.pad_mask = 1 << RETRO_DEVICE_ID_JOYPAD_UP
    frame = run_to(780, frame)
    capture("gameplay_accelerating")
    FRONTEND.pad_mask |= 1 << RETRO_DEVICE_ID_JOYPAD_A
    frame = run_to(820, frame)
    capture("gameplay_jump")
    FRONTEND.pad_mask = 1 << RETRO_DEVICE_ID_JOYPAD_UP
    frame = run_to(900, frame)
    capture("gameplay_later")
    FRONTEND.pad_mask = 0
    # The landing command is posted near the end of the previous interval.
    # Continue long enough for the slave SH-2 to stream the complete PWM sample
    # instead of mistaking a changing FM passage for the landing effect.
    frame = run_to(1200, frame)
    capture("gameplay_sfx_tail")

    # Console palette regression: without explicitly loading CARS.LZS and
    # DASHBRD.LZS CMAP sections, their nonzero pixel indices all display as
    # palette color zero, making the ship and complete HUD black silhouettes.
    hud_stats = crop_stats(capture_frames["gameplay_accelerating"],
                           0, 150, 320, 212)
    ship_stats = crop_stats(capture_frames["gameplay_accelerating"],
                            140, 84, 181, 120)
    if hud_stats["colors"] < 20 or hud_stats["visible_ratio"] < 0.5:
        raise AssertionError(f"HUD palette is missing/black: {hud_stats}")
    if ship_stats["colors"] < 8:
        raise AssertionError(f"ship palette is missing/black: {ship_stats}")

    # Genesis audio integration checks. The intro interval contains the PWM
    # INTRO.SND sample. gameplay_accelerating occurs after that sample ends,
    # so its continuous output verifies native YM2612 VGM music playback.
    intro_audio = audio_captures["boot_intro"]
    music_audio = audio_captures["gameplay_accelerating"]
    sfx_audio = audio_captures["gameplay_sfx_tail"]
    if (intro_audio["frames"] == 0 or intro_audio["peak"] < 128 or
            intro_audio["nonzero_ratio"] < 0.02):
        raise AssertionError(f"intro PCM output is silent: {intro_audio}")
    if (music_audio["frames"] == 0 or music_audio["peak"] < 64 or
            music_audio["nonzero_ratio"] < 0.02):
        raise AssertionError(f"gameplay YM2612 VGM music is silent: {music_audio}")
    # Releasing A after the scripted jump causes the ship to land in the final
    # interval. Verify both level and the actual SFX.SND landing waveform; a
    # changing FM passage alone must not satisfy this regression.
    sfx_data = (Path(__file__).resolve().parents[1] / "SFX.SND").read_bytes()
    landing_off, landing_end = struct.unpack_from("<HH", sfx_data, 2)
    landing_rate = 1_000_000 // (256 - sfx_data[landing_off])
    landing_correlation = pcm_correlation(
        sample_captures["gameplay_sfx_tail"],
        sfx_data[landing_off + 1:landing_end], landing_rate)
    if sfx_audio["peak"] < music_audio["peak"] + 4000:
        raise AssertionError(f"landing PWM effect is too quiet: {sfx_audio}")
    if landing_correlation < 0.12:
        raise AssertionError(
            f"landing PWM waveform is missing: correlation={landing_correlation}")

    for name, stats in captures.items():
        check_visible(name, stats)
    state_crcs = {captures[n]["crc32"] for n in ("boot_intro", "main_menu", "road_menu")}
    if len(state_crcs) != 3:
        raise AssertionError(f"UI checkpoints did not transition: {captures}")
    gameplay_crcs = {captures[n]["crc32"] for n in
                     ("gameplay_accelerating", "gameplay_jump", "gameplay_later")}
    if len(gameplay_crcs) < 2:
        raise AssertionError(f"gameplay image never changed: {captures}")

    report = {
        "rom": str(ns.rom), "core": str(ns.core),
        "reported_fps": av.timing.fps, "video_callbacks": FRONTEND.video_calls,
        "dpad_regression": {"down_pixel_delta": round(menu_delta, 6),
                            "right_pixel_delta": round(right_delta, 6)},
        "palette_regression": {"hud_crop": hud_stats,
                               "ship_crop": ship_stats},
        "audio": {"intro_pwm_pcm": intro_audio,
                  "gameplay_ym2612_vgm": music_audio,
                  "landing_pwm_sfx": sfx_audio,
                  "landing_pcm_correlation": landing_correlation,
                  "checkpoint_audio": audio_captures},
        "checkpoints": captures, "result": "PASS",
    }
    (ns.artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    FRONTEND.close()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"EMULATOR TEST FAILED: {exc}", file=sys.stderr)
        raise
