#include "../../core/cfg.h"
#include <string.h>
void sr_cfg_load(sr_cfg *c,const sr_io *io){(void)io;memset(c,0,sizeof *c);}
void sr_cfg_save(const sr_cfg *c,const sr_io *io){if(io->write_file)io->write_file("skyroads.cfg",c,sizeof *c);}
