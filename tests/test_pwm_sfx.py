#!/usr/bin/env python3
"""Regression checks for audible 32X PWM effect normalization."""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_32x_assets import load_sfx  # noqa: E402


def main() -> int:
    effects = load_sfx(ROOT)
    assert len(effects) == 5
    expected_gains = []
    for number, (time_constant, gain_q8, pcm) in enumerate(effects):
        centered = [value - 128 for value in pcm]
        peak = max(map(abs, centered))
        rms = math.sqrt(sum(value * value for value in centered) / len(centered))
        pwm_peak = peak * gain_q8 / 128.0
        pwm_rms = rms * gain_q8 / 128.0
        assert 256 <= gain_q8 <= 1024, (number, gain_q8)
        assert 470 <= pwm_peak <= 481, (number, pwm_peak)
        assert pwm_rms >= 80, (number, pwm_rms)
        expected_gains.append(gain_q8)

    generated = ROOT / "src/platform/32x/generated/32x/assets_data_32x.c"
    if generated.exists():
        text = generated.read_text()
        block = text.split("const sr32_pcm_asset sr32_sfx[5] = {", 1)[1].split("};", 1)[0]
        actual = [int(match) for match in re.findall(r",\s*(\d+)\s*},", block)]
        assert actual == expected_gains, (actual, expected_gains)
        assert re.search(r"const sr32_pcm_asset sr32_intro_pcm\s*=.*0x5a, 256}",
                         text, re.S), "intro PCM must remain at unity gain"

    print("PWM SFX normalization OK: gains " +
          ", ".join(f"{gain}/256x" for gain in expected_gains))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
