#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "hle_internal.h"
#include "memory.h"
#include "ucodes.h"
#define DRAM_SIZE (1<<20)
static uint32_t be32(const uint8_t*p){return (uint32_t)p[0]<<24|p[1]<<16|p[2]<<8|p[3];}
static void put_be32(uint8_t*p,uint32_t v){p[0]=v>>24;p[1]=v>>16;p[2]=v>>8;p[3]=v;}
int main(int argc,char**argv){
  /* abitest variant in out */
  static unsigned char dram[DRAM_SIZE], dmem[0x2000], imem[0x1000];
  static struct hle_t hle;
  unsigned int status=0,mi=0,dummy=0;
  FILE*f=fopen(argv[2],"rb"); uint8_t hdr[8]; fread(hdr,1,8,f);
  uint32_t aptr=be32(hdr), asize=be32(hdr+4);
  static uint8_t img[DRAM_SIZE], ab[0x1000];
  fread(img,1,DRAM_SIZE,f); fread(ab,1,0x1000,f); fclose(f);
  for(int i=0;i<DRAM_SIZE;i+=4) *(uint32_t*)(dram+i)=be32(img+i);
  memset(&hle,0,sizeof hle);
  hle.dram=dram;hle.dmem=dmem;hle.imem=imem;hle.sp_status=&status;hle.mi_intr=&mi;
  hle.sp_mem_addr=hle.sp_dram_addr=hle.sp_rd_length=hle.sp_wr_length=hle.sp_dma_full=hle.sp_dma_busy=hle.sp_pc=hle.sp_semaphore=&dummy;
  for(int i=0;i<0x1000;i+=4) *(uint32_t*)(hle.alist_buffer+i)=be32(ab+i);
  /* состояние между задачами: второй запуск повторяет те же команды (проверка накопленного состояния) */
  *dmem_u32(&hle,TASK_DATA_PTR)=aptr; *dmem_u32(&hle,TASK_DATA_SIZE)=asize;
  const char*v=argv[1];
  void (*fn)(struct hle_t*)=0;
  if(!strcmp(v,"naudio"))fn=alist_process_naudio; else if(!strcmp(v,"bk"))fn=alist_process_naudio_bk;
  else if(!strcmp(v,"dk"))fn=alist_process_naudio_dk; else if(!strcmp(v,"mp3"))fn=alist_process_naudio_mp3;
  else if(!strcmp(v,"cbfd"))fn=alist_process_naudio_cbfd;
  else if(!strcmp(v,"mk"))fn=alist_process_nead_mk; else if(!strcmp(v,"sf"))fn=alist_process_nead_sf;
  else if(!strcmp(v,"sfj"))fn=alist_process_nead_sfj; else if(!strcmp(v,"fz"))fn=alist_process_nead_fz;
  else if(!strcmp(v,"wrjb"))fn=alist_process_nead_wrjb; else if(!strcmp(v,"ys"))fn=alist_process_nead_ys;
  else if(!strcmp(v,"1080"))fn=alist_process_nead_1080; else if(!strcmp(v,"oot"))fn=alist_process_nead_oot;
  else if(!strcmp(v,"mm"))fn=alist_process_nead_mm; else if(!strcmp(v,"mmb"))fn=alist_process_nead_mmb;
  else if(!strcmp(v,"ac"))fn=alist_process_nead_ac;
  else {fprintf(stderr,"bad variant\n");return 2;}
  int reps = argc>4?atoi(argv[4]):1;
  for(int r=0;r<reps;r++) fn(&hle);
  f=fopen(argv[3],"wb");
  uint8_t o[4];
  for(int i=0;i<DRAM_SIZE;i+=4){put_be32(o,*(uint32_t*)(dram+i));fwrite(o,1,4,f);}
  for(int i=0;i<0x1000;i+=4){put_be32(o,*(uint32_t*)(hle.alist_buffer+i));fwrite(o,1,4,f);}
  fclose(f); return 0;
}
