#ifndef PD_READER_H
#define PD_READER_H
#include "paper.h"
#ifdef __cplusplus
extern "C" {
#endif
void *pd_reader_open(const char *path,char *error,size_t size);
void pd_reader_close(void *reader);
int pd_reader_layout(void *reader,int width,int height,int font);
int pd_reader_pages(void *reader);
PD_Rect pd_reader_bounds(void *reader);
int pd_reader_render(void *reader,int page,float scale,int rotation,PD_Bitmap *bitmap,char *error,size_t size);
int pd_reader_resolve(void *reader,const char *anchor);
int pd_reader_link(void *reader,int page,float x,float y);
int pd_reader_text(void *reader,int page,const char *path);
#ifdef __cplusplus
}
#endif
#endif
