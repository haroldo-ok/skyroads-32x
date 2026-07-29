#include "hw_32x.h"
#include "../../core/sr.h"
static uint16_t current_fb;
static int refresh_hz = 60;
static uint8_t staging_fb[SR_SCREEN_W * SR_SCREEN_H] __attribute__((aligned(4)));
uint8_t *sr32_backbuffer_pixels(void) { return staging_fb; }
static void init_writable_image(void) {
    volatile uint16_t *fb = (volatile uint16_t *)&MARS_FRAMEBUFFER;
    const uint16_t blank = 0x100u + (SR_SCREEN_W * SR_SCREEN_H) / 2u;
    const int top = (224 - SR_SCREEN_H) / 2;
    for (int y=0;y<224;y++) fb[y]=(y>=top&&y<top+SR_SCREEN_H)?(uint16_t)(0x100u+(y-top)*(SR_SCREEN_W/2)):blank;
    for (int y=224;y<256;y++) fb[y]=blank;
    volatile uint32_t *pixels=(volatile uint32_t *)(0x24000000u+0x200u);
    for(int i=0;i<(SR_SCREEN_W*SR_SCREEN_H)/4+SR_SCREEN_W/4;i++) pixels[i]=0;
}
void sr32_video_init(void) {
    while ((MARS_SYS_INTMSK & MARS_SH2_ACCESS_VDP)==0) {}
    MARS_VDP_DISPMODE=MARS_224_LINES|MARS_VDP_MODE_256;
    refresh_hz=(MARS_VDP_DISPMODE&MARS_NTSC_FORMAT)?60:50;
    current_fb=MARS_VDP_FBCTL&MARS_VDP_FS;
    for(int i=0;i<2;i++) { MARS_VDP_FBCTL=current_fb^1u; while((MARS_VDP_FBCTL&MARS_VDP_FS)==current_fb){} current_fb^=1u; init_writable_image(); }
}
void sr32_video_present(const uint8_t *pal) {
    volatile uint16_t *cram=(volatile uint16_t *)&MARS_CRAM;
    while((MARS_SYS_INTMSK&MARS_SH2_ACCESS_VDP)==0){}
    volatile uint32_t *dst=(volatile uint32_t *)(0x24000000u+0x200u); const uint32_t *src=(const uint32_t *)staging_fb;
    for(int i=0;i<(SR_SCREEN_W*SR_SCREEN_H)/4;i++)dst[i]=src[i];
    for(int i=0;i<256;i++){uint16_t r=pal[i*3]>>1,g=pal[i*3+1]>>1,b=pal[i*3+2]>>1;cram[i]=(uint16_t)(0x8000u|r|(g<<5)|(b<<10));}
    MARS_VDP_FBCTL=current_fb^1u;while((MARS_VDP_FBCTL&MARS_VDP_FS)==current_fb){}current_fb^=1u;
}
uint16_t sr32_read_pad(void)
{
    while (MARS_SYS_COMM0 != 0) {}
    MARS_SYS_COMM0 = 0x0300;
    while (MARS_SYS_COMM0 != 0) {}
    /*
     * Some emulators expose a 6-button pad whose extended nibble mirrors
     * U/D/L/R during the 68000 handshake (U also looks like Z, D like Y,
     * L like X and R like Mode).  The port only needs the reliable 3-button
     * set.  Masking here prevents every d-pad direction from also becoming
     * Jump/Back while preserving U/D/L/R, A/B/C and Start.
     */
    return MARS_SYS_COMM8 & 0x00ffu;
}
int sr32_refresh_hz(void){return refresh_hz;}
