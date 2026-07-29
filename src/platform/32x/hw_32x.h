#ifndef SR_HW_32X_H
#define SR_HW_32X_H
#include <stdint.h>
#define MARS_CRAM          (*(volatile uint16_t *)0x20004200)
#define MARS_FRAMEBUFFER   (*(volatile uint16_t *)0x24000000)
#define MARS_SYS_INTMSK    (*(volatile uint16_t *)0x20004000)
#define MARS_SYS_COMM0     (*(volatile uint16_t *)0x20004020)
#define MARS_SYS_COMM4     (*(volatile uint16_t *)0x20004024)
#define MARS_SYS_COMM6     (*(volatile uint16_t *)0x20004026)
#define MARS_SYS_COMM8     (*(volatile uint16_t *)0x20004028)
#define MARS_SYS_COMM10    (*(volatile uint16_t *)0x2000402A)
#define MARS_SYS_COMM12    (*(volatile uint32_t *)0x2000402C)
#define MARS_PWM_CTRL      (*(volatile uint16_t *)0x20004030)
#define MARS_PWM_CYCLE     (*(volatile uint16_t *)0x20004032)
#define MARS_PWM_LEFT      (*(volatile uint16_t *)0x20004034)
#define MARS_PWM_RIGHT     (*(volatile uint16_t *)0x20004036)
#define MARS_PWM_MONO      (*(volatile uint16_t *)0x20004038)
#define MARS_VDP_DISPMODE  (*(volatile uint16_t *)0x20004100)
#define MARS_VDP_FBCTL     (*(volatile uint16_t *)0x2000410A)
#define MARS_SH2_ACCESS_VDP 0x8000
#define MARS_NTSC_FORMAT    0x8000
#define MARS_224_LINES      0x0000
#define MARS_VDP_MODE_256   0x0001
#define MARS_VDP_FS         0x0001
#define SEGA_CTRL_UP       0x0001
#define SEGA_CTRL_DOWN     0x0002
#define SEGA_CTRL_LEFT     0x0004
#define SEGA_CTRL_RIGHT    0x0008
#define SEGA_CTRL_B        0x0010
#define SEGA_CTRL_C        0x0020
#define SEGA_CTRL_A        0x0040
#define SEGA_CTRL_START    0x0080
#define SEGA_CTRL_Z        0x0100
#define SEGA_CTRL_Y        0x0200
#define SEGA_CTRL_X        0x0400
#define SEGA_CTRL_MODE     0x0800
void sr32_video_init(void);
void sr32_video_present(const uint8_t *pal_rgb6);
uint16_t sr32_read_pad(void);
uint8_t *sr32_backbuffer_pixels(void);
int sr32_refresh_hz(void);
#endif
