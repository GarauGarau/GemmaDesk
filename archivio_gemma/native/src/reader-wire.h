/* SPDX-License-Identifier: 0BSD
 * Value types shared by the optional standalone EPUB renderer. */
#ifndef PD_READER_WIRE_H
#define PD_READER_WIRE_H
#include <stddef.h>
typedef struct {float x0,y0,x1,y1;} PD_Rect;
typedef struct {unsigned char *samples;int w,h,stride;float scale;} PD_Bitmap;
#endif
