#include "audio_32x.h"
#include "hw_32x.h"
#include "generated/32x/assets_data_32x.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define PWM_SAMPLE_MIN 2
#define PWM_SAMPLE_MAX 1032
#define PWM_SAMPLE_CENTER 515

typedef struct {
    bool enabled;
    const uint8_t *pcm;
    uint32_t pcm_size;
    uint32_t pcm_pos_fp;
    uint32_t pcm_step_fp;
    uint16_t pcm_gain_q8;
} sr32_audio_state;

static sr32_audio_state audio;

static void pcm_start(const sr32_pcm_asset *pcm)
{
    if (!audio.enabled || !pcm || !pcm->data || !pcm->size)
        return;
    audio.pcm = pcm->data;
    audio.pcm_size = pcm->size;
    audio.pcm_pos_fp = 0;
    uint32_t rate = 1000000u / (256u - pcm->time_constant);
    audio.pcm_step_fp = (rate * 65536u) / SR32_PWM_RATE;
    audio.pcm_gain_q8 = pcm->gain_q8;
}

static void set_enabled(bool enabled)
{
    audio.enabled = enabled;
    if (!enabled)
        audio.pcm = NULL;
}

static void handle_pwm_command(uint16_t command)
{
    switch (command & 0xff00) {
    case SR32_ACMD_SFX_BASE: {
        unsigned number = command & 0xff;
        if (number < SR32_SFX_COUNT)
            pcm_start(&sr32_sfx[number]);
        break;
    }
    case SR32_ACMD_INTRO_PCM:
        pcm_start(&sr32_intro_pcm);
        break;
    case SR32_ACMD_DISABLE:
        set_enabled(command == SR32_ACMD_ENABLE);
        break;
    default:
        break;
    }
}

static void poll_command(void)
{
    uint16_t command = MARS_SYS_COMM4;
    if (command != 0) {
        handle_pwm_command(command);
        MARS_SYS_COMM4 = 0;
    }
}

static int16_t render_sample(void)
{
    if (!audio.enabled || !audio.pcm)
        return 0;

    uint32_t position = audio.pcm_pos_fp >> 16;
    if (position >= audio.pcm_size) {
        audio.pcm = NULL;
        return 0;
    }
    /* Q8 normalization compensates for the very different recording levels
     * in SFX.SND. Unity (256) is the original signed 8-bit << 7 conversion. */
    int32_t sample = ((int32_t)audio.pcm[position] - 128) *
                     audio.pcm_gain_q8 / 2;
    if (sample > 32767) sample = 32767;
    if (sample < -32768) sample = -32768;
    audio.pcm_pos_fp += audio.pcm_step_fp;
    return (int16_t)sample;
}

static uint16_t to_pwm(int16_t sample)
{
    int32_t value = PWM_SAMPLE_CENTER + sample / 64;
    if (value < PWM_SAMPLE_MIN)
        value = PWM_SAMPLE_MIN;
    if (value > PWM_SAMPLE_MAX)
        value = PWM_SAMPLE_MAX;
    return (uint16_t)value;
}

static void pwm_initialize(void)
{
    MARS_PWM_MONO = 1;
    MARS_PWM_MONO = 1;
    MARS_PWM_MONO = 1;
    uint32_t clock = (MARS_VDP_DISPMODE & MARS_NTSC_FORMAT)
                     ? 23011361u : 22801467u;
    MARS_PWM_CYCLE = (uint16_t)(((clock << 1) / SR32_PWM_RATE + 1) / 2 + 1);
    MARS_PWM_CTRL = 0x0185;

    /* Short click-free ramp on real hardware. */
    for (uint16_t sample = PWM_SAMPLE_MIN;
         sample < PWM_SAMPLE_CENTER; sample++) {
        while (MARS_PWM_MONO & 0x8000) {}
        MARS_PWM_MONO = sample;
    }
}

bool sr32_audio_wait_ready(void)
{
    /* Audio must never be able to deadlock video/game startup. */
    for (volatile uint32_t timeout = 0; timeout < 1000000u; timeout++) {
        if (MARS_SYS_COMM6 == SR32_AUDIO_READY)
            return true;
    }
    return false;
}

bool sr32_audio_send(uint16_t command)
{
    uint16_t group = command & 0xff00;
    bool to_fm = group == SR32_ACMD_MUSIC_BASE ||
                 group == SR32_ACMD_DISABLE;
    bool to_pwm = group == SR32_ACMD_SFX_BASE ||
                  group == SR32_ACMD_INTRO_PCM ||
                  group == SR32_ACMD_DISABLE;

    /* COMM14 is the low half of the 68000's 32-bit VBlank counter and cannot
     * carry a persistent readiness marker.  VGM setup completes before that
     * counter starts, so an empty COMM10 is the safe non-blocking condition. */
    if (to_fm && MARS_SYS_COMM10 != 0)
        return false;
    if (to_pwm && (MARS_SYS_COMM6 != SR32_AUDIO_READY ||
                   MARS_SYS_COMM4 != 0))
        return false;
    if (to_fm)
        MARS_SYS_COMM10 = command;
    if (to_pwm)
        MARS_SYS_COMM4 = command;
    return true;
}

void sr32_audio_slave_main(void)
{
    /* Release the master immediately.  Music is now rendered by the native
     * Genesis YM2612; this slave only streams original PCM effects over PWM. */
    MARS_SYS_COMM4 = 0;
    MARS_SYS_COMM6 = SR32_AUDIO_READY;
    memset(&audio, 0, sizeof audio);
    audio.enabled = true;
    pwm_initialize();

    for (;;) {
        poll_command();
        while ((MARS_PWM_LEFT & 0x8000) ||
               (MARS_PWM_RIGHT & 0x8000)) {
            poll_command();
        }
        uint16_t sample = to_pwm(render_sample());
        MARS_PWM_LEFT = sample;
        MARS_PWM_RIGHT = sample;
    }
}
