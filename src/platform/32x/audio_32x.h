#ifndef SR_AUDIO_32X_H
#define SR_AUDIO_32X_H

#include <stdbool.h>
#include <stdint.h>

#define SR32_PWM_RATE 22050
#define SR32_AUDIO_READY 0xa55a

#define SR32_ACMD_MUSIC_BASE 0x0100
#define SR32_ACMD_MUSIC_STOP 0x01ff
#define SR32_ACMD_SFX_BASE   0x0200
#define SR32_ACMD_INTRO_PCM  0x0300
#define SR32_ACMD_DISABLE    0x0400
#define SR32_ACMD_ENABLE     0x0401

/* Master SH-2 side: route music to the 68000 VGM/YM2612 player and PCM
 * effects to the slave SH-2 PWM mixer without blocking game startup. */
bool sr32_audio_wait_ready(void);
bool sr32_audio_send(uint16_t command);

/* Slave SH-2 entry; initializes both PWM channels and never returns. */
void sr32_audio_slave_main(void);

#endif
