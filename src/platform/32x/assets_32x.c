#include "../../core/assets.h"
#include "generated/32x/assets_data_32x.h"
#include <string.h>
bool sr_assets_load(sr_assets *a,sr_io io,char *err,size_t errlen){(void)err;(void)errlen;memset(a,0,sizeof *a);a->io=io;sr32_populate_assets(a);return true;}
bool sr_assets_load_road(sr_assets *a,int entry,sr_road *road){return sr32_load_road(a,entry,road);}
bool sr_assets_load_world(sr_assets *a,int world){return sr32_load_world(a,world);}
void sr_assets_free_gfx(sr_gfxfile *g){(void)g;}
void sr_gfx_apply_section(const sr_gfxfile *g,int idx,sr_rgb6 *pal){if(idx<0||idx>=g->n_sections)return;const sr_pal_section *s=&g->sections[idx];for(int i=0;i<s->count&&s->base+i<256;i++)pal[s->base+i]=s->colors[i];}
void sr_gfx_apply_pal(const sr_gfxfile *g,sr_rgb6 *pal){for(int i=0;i<g->n_sections;i++)sr_gfx_apply_section(g,i,pal);}
