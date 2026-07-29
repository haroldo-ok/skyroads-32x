#include "../../core/game.h"
#include "audio_32x.h"
#include "hw_32x.h"
#include <stddef.h>
#include <stdint.h>

static sr_game game;

static void *rom_no_file(const char *name, size_t *size)
{
    (void)name;
    if (size)
        *size = 0;
    return NULL;
}

static bool session_save(const char *name, const void *data, size_t size)
{
    (void)name;
    (void)data;
    (void)size;
    return true;
}

static void latch_input(sr_input *in, uint16_t pad, uint16_t *previous)
{
    uint16_t changed = (uint16_t)(pad & ~*previous);
    *previous = pad;

    in->held[SR_KEY_UP] = !!(pad & SEGA_CTRL_UP);
    in->held[SR_KEY_DOWN] = !!(pad & SEGA_CTRL_DOWN);
    in->held[SR_KEY_LEFT] = !!(pad & SEGA_CTRL_LEFT);
    in->held[SR_KEY_RIGHT] = !!(pad & SEGA_CTRL_RIGHT);
    in->held[SR_KEY_JUMP] = !!(pad & (SEGA_CTRL_A | SEGA_CTRL_C));

    if (changed & SEGA_CTRL_UP)
        in->pressed[SR_KEY_UP] = 1;
    if (changed & SEGA_CTRL_DOWN)
        in->pressed[SR_KEY_DOWN] = 1;
    if (changed & SEGA_CTRL_LEFT)
        in->pressed[SR_KEY_LEFT] = 1;
    if (changed & SEGA_CTRL_RIGHT)
        in->pressed[SR_KEY_RIGHT] = 1;
    if (changed & (SEGA_CTRL_A | SEGA_CTRL_C)) {
        in->pressed[SR_KEY_JUMP] = 1;
        in->pressed[SR_KEY_ENTER] = 1;
    }
    if (changed & SEGA_CTRL_B)
        in->pressed[SR_KEY_ESC] = 1;
    if (changed & SEGA_CTRL_START) {
        in->pressed[SR_KEY_ENTER] = 1;
        in->pressed[SR_KEY_PAUSE] = 1;
    }
}

static void service_audio(int *enabled_state, int *current_song)
{
    int enabled = !game.cfg.sound_off;
    if (enabled != *enabled_state) {
        if (!sr32_audio_send(enabled ? SR32_ACMD_ENABLE :
                                      SR32_ACMD_DISABLE))
            return;
        *enabled_state = enabled;
        *current_song = -2;
    }

    if (enabled && game.want_song != *current_song) {
        uint16_t command = game.want_song < 0
                           ? SR32_ACMD_MUSIC_STOP
                           : (uint16_t)(SR32_ACMD_MUSIC_BASE |
                                      game.want_song);
        if (!sr32_audio_send(command))
            return;
        *current_song = game.want_song;
    }
    if (game.sfx_request) {
        if (enabled && !sr32_audio_send((uint16_t)(SR32_ACMD_SFX_BASE |
                                                   (game.sfx_request - 1))))
            return;
        game.sfx_request = 0;
    }
    if (game.want_intro_snd) {
        if (enabled && !sr32_audio_send(SR32_ACMD_INTRO_PCM))
            return;
        game.want_intro_snd = 0;
    }
}

int m_main(void)
{
    sr_input input = {0};
    uint16_t previous = 0;
    uint32_t tick_acc = 0;
    int audio_enabled = -1;
    int current_song = -2;
    char err[64];

    sr32_video_init();
    if (!sr_game_init(&game, (sr_io){rom_no_file, session_save},
                      err, sizeof err)) {
        volatile uint16_t *cram = (volatile uint16_t *)&MARS_CRAM;
        cram[0] = 0x801f;
        for (;;) {}
    }

    sr32_video_present((const uint8_t *)game.out_pal);
    uint32_t last_vblank = MARS_SYS_COMM12;

    for (;;) {
        while (MARS_SYS_COMM12 == last_vblank) {}
        last_vblank = MARS_SYS_COMM12;

        latch_input(&input, sr32_read_pad(), &previous);
        tick_acc += SR_TICK_NUM;
        uint32_t threshold = SR_TICK_DEN * (uint32_t)sr32_refresh_hz();
        if (tick_acc >= threshold) {
            tick_acc -= threshold;
            if (sr_game_running(&game))
                sr_game_tick(&game, &input);
            service_audio(&audio_enabled, &current_song);
            for (int i = 0; i < SR_KEY_COUNT; i++)
                input.pressed[i] = 0;
            sr32_video_present((const uint8_t *)game.out_pal);
        }
    }
}

void s_main(void)
{
    sr32_audio_slave_main();
}
