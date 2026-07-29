#include "common.h"
#include "../generated/32x/vgm_data_68k.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define RAMCODE __attribute__((section(".data")))

#define YM_PORT0_ADDR 0xA04000
#define PSG_PORT_ADDR 0xC00011

#define SR32_ACMD_MUSIC_BASE   0x0100
#define SR32_ACMD_MUSIC_STOP   0x01ff
#define SR32_ACMD_DISABLE      0x0400
#define SR32_ACMD_ENABLE       0x0401

static volatile uint8_t *const ym_port = (volatile uint8_t *)YM_PORT0_ADDR;
static volatile uint8_t *const psg_port = (volatile uint8_t *)PSG_PORT_ADDR;
static volatile uint16_t *const z80_bus_req = (volatile uint16_t *)Z80_BUS_REQ;
static volatile uint16_t *const z80_reset = (volatile uint16_t *)Z80_RESET;
static volatile uint16_t *const mars_comm10 = (volatile uint16_t *)MARS_COMM10;

typedef struct {
    const uint8_t *position;
    const uint8_t *loop;
    const uint8_t *end;
    uint16_t wait_ticks;
    bool enabled;
    bool playing;
} vgm_state;

static vgm_state vgm;

RAMCODE static uint32_t read_le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* YM2612 accesses are deliberately bounded: a faulty sound device must never
 * be able to deadlock the 68000 controller/VBlank service and black-screen the
 * game.  Normal hardware clears BUSY in a tiny fraction of these loops. */
RAMCODE static void ym_write(unsigned port, uint8_t reg, uint8_t value)
{
    unsigned timeout = 1024;
    while ((ym_port[0] & 0x80) && --timeout) {}
    ym_port[port ? 2 : 0] = reg;
    __asm__ volatile ("nop\n\tnop\n\tnop");
    timeout = 1024;
    while ((ym_port[0] & 0x80) && --timeout) {}
    ym_port[port ? 3 : 1] = value;
    __asm__ volatile ("nop\n\tnop\n\tnop");
}

RAMCODE static void silence_music(void)
{
    static const uint8_t key_channels[6] = {0, 1, 2, 4, 5, 6};
    for (unsigned channel = 0; channel < 6; channel++)
        ym_write(0, 0x28, key_channels[channel]);
    for (unsigned port = 0; port < 2; port++) {
        for (unsigned channel = 0; channel < 3; channel++) {
            for (unsigned slot = 0; slot < 4; slot++) {
                static const uint8_t slot_offset[4] = {0, 8, 4, 12};
                ym_write(port, (uint8_t)(0x40 + channel + slot_offset[slot]),
                         127);
            }
        }
    }
    *psg_port = 0x9f;
    *psg_port = 0xbf;
    *psg_port = 0xdf;
    *psg_port = 0xff;
}

RAMCODE static void music_stop(void)
{
    vgm.playing = false;
    vgm.wait_ticks = 0;
    silence_music();
}

RAMCODE static bool vgm_bounds(const uint8_t *position, size_t count)
{
    return position <= vgm.end && count <= (size_t)(vgm.end - position);
}

RAMCODE static void vgm_process_commands(void)
{
    /* A valid stream always encounters a wait.  The limit protects gameplay
     * and controller service from malformed generated or corrupted ROM data. */
    for (unsigned commands = 0; commands < 2048 && vgm.playing; commands++) {
        if (!vgm_bounds(vgm.position, 1)) {
            music_stop();
            return;
        }
        uint8_t command = *vgm.position++;
        switch (command) {
        case 0x50:
            if (!vgm_bounds(vgm.position, 1)) { music_stop(); return; }
            *psg_port = *vgm.position++;
            break;
        case 0x52:
        case 0x53:
            if (!vgm_bounds(vgm.position, 2)) { music_stop(); return; }
            ym_write(command == 0x53, vgm.position[0], vgm.position[1]);
            vgm.position += 2;
            break;
        case 0x61: {
            if (!vgm_bounds(vgm.position, 2)) { music_stop(); return; }
            uint16_t samples = (uint16_t)(vgm.position[0] |
                                         (vgm.position[1] << 8));
            vgm.position += 2;
            /* Generated waits are 44.1 kHz VGM time rounded from the DOS
             * 180.018 Hz music IRQ.  Convert them back to source ticks. */
            vgm.wait_ticks = (uint16_t)((samples + 122u) / 245u);
            if (!vgm.wait_ticks)
                vgm.wait_ticks = 1;
            return;
        }
        case 0x62:
            vgm.wait_ticks = 3; /* 735 / 245 */
            return;
        case 0x63:
            vgm.wait_ticks = 4; /* nearest source-tick duration */
            return;
        case 0x66:
            if (vgm.loop) {
                vgm.position = vgm.loop;
            } else {
                music_stop();
                return;
            }
            break;
        default:
            if (command >= 0x70 && command <= 0x7f) {
                unsigned samples = (command & 0x0f) + 1;
                vgm.wait_ticks = (uint16_t)((samples + 122u) / 245u);
                if (!vgm.wait_ticks)
                    vgm.wait_ticks = 1;
                return;
            }
            music_stop();
            return;
        }
    }
    if (vgm.playing)
        music_stop();
}

RAMCODE static void music_start(unsigned number)
{
    music_stop();
    if (!vgm.enabled || number >= SR32_VGM_SONG_COUNT)
        return;

    const sr32_vgm_asset *asset = &sr32_vgm_songs[number];
    const uint8_t *data = asset->data;
    if (asset->size < 0x41 || data[0] != 'V' || data[1] != 'g' ||
            data[2] != 'm' || data[3] != ' ')
        return;

    uint32_t data_relative = read_le32(data + 0x34);
    uint32_t data_offset = data_relative ? 0x34u + data_relative : 0x40u;
    uint32_t loop_relative = read_le32(data + 0x1c);
    if (data_offset >= asset->size)
        return;

    vgm.position = data + data_offset;
    vgm.end = data + asset->size;
    vgm.loop = NULL;
    if (loop_relative) {
        uint32_t loop_offset = 0x1cu + loop_relative;
        if (loop_offset < asset->size)
            vgm.loop = data + loop_offset;
    }
    vgm.wait_ticks = 0;
    vgm.playing = true;
    vgm_process_commands();
}

RAMCODE static void handle_command(uint16_t command)
{
    switch (command & 0xff00) {
    case SR32_ACMD_MUSIC_BASE:
        if (command == SR32_ACMD_MUSIC_STOP)
            music_stop();
        else
            music_start(command & 0xff);
        break;
    case SR32_ACMD_DISABLE:
        vgm.enabled = command == SR32_ACMD_ENABLE;
        if (!vgm.enabled)
            music_stop();
        break;
    default:
        break;
    }
}

RAMCODE void sr32_vgm_init(void)
{
    /* The Z80 is unused by this port.  Keep its bus requested so the 68000 can
     * safely drive the real YM2612 without racing another sound CPU. */
    *z80_bus_req = 0x0100;
    *z80_reset = 0x0100; /* release reset so the YM2612 clock is running */
    for (volatile unsigned timeout = 0; timeout < 65535; timeout++) {
        if ((*z80_bus_req & 0x0100) == 0)
            break;
    }

    vgm.position = NULL;
    vgm.loop = NULL;
    vgm.end = NULL;
    vgm.wait_ticks = 0;
    vgm.enabled = true;
    vgm.playing = false;

    ym_write(0, 0x27, 0x30); /* timers off, clear both flags */
    ym_write(0, 0x22, 0x00); /* LFO off */
    ym_write(0, 0x2b, 0x00); /* DAC off */
    silence_music();

    /* Timer A = 728: about 180 Hz at the NTSC Genesis YM2612 clock, matching
     * SkyRoads' original PIT divisor 6628 (180.018 Hz). */
    ym_write(0, 0x24, 0xb6);
    ym_write(0, 0x25, 0x00);
    ym_write(0, 0x27, 0x15); /* load/enable A and clear overflow A */
    *mars_comm10 = 0;
}

RAMCODE void sr32_vgm_service(void)
{
    uint16_t command = *mars_comm10;
    if (command) {
        handle_command(command);
        *mars_comm10 = 0;
    }

    /* Only status bit 0 is Timer A.  Other status bits differ among discrete
     * YM2612/YM3438 revisions and are intentionally ignored here. */
    if (ym_port[0] & 0x01) {
        ym_write(0, 0x27, 0x15);
        if (vgm.enabled && vgm.playing) {
            if (vgm.wait_ticks)
                vgm.wait_ticks--;
            if (!vgm.wait_ticks)
                vgm_process_commands();
        }
    }
}
