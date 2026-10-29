#include "internal.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdio.h>

PD_Rect from_fz(fz_rect r) { return (PD_Rect){r.x0,r.y0,r.x1,r.y1}; }
fz_rect to_fz(PD_Rect r) { return fz_make_rect(r.x0,r.y0,r.x1,r.y1); }
PD_Rect rect_union(PD_Rect a, PD_Rect b) {
    return (PD_Rect){fminf(a.x0,b.x0), fminf(a.y0,b.y0), fmaxf(a.x1,b.x1), fmaxf(a.y1,b.y1)};
}
PD_Rect rect_pad(PD_Rect a, float p) { return (PD_Rect){a.x0-p,a.y0-p,a.x1+p,a.y1+p}; }
int rect_overlap(PD_Rect a, PD_Rect b) {
    return a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
}
float rect_area(PD_Rect a) { return fmaxf(0,a.x1-a.x0)*fmaxf(0,a.y1-a.y0); }
float rect_x_overlap(PD_Rect a, PD_Rect b) { return fmaxf(0,fminf(a.x1,b.x1)-fmaxf(a.x0,b.x0)); }
void *pd_grow(fz_context *ctx, void *p, int *cap, int need, size_t item) {
    if (need<=*cap) return p;
    int next=*cap ? *cap : 16;
    while(next<need) {
        if(next > 10000000) fz_throw(ctx,FZ_ERROR_LIMIT,"Document structure is too large");
        next*=2;
    }
    p=fz_realloc(ctx,p,(size_t)next*item);
    *cap=next;
    return p;
}
int pd_xml_scalar(int c) {
    return c==9 || c==10 || c==13 || (c>=0x20 && c<=0xd7ff) ||
           (c>=0xe000 && c<=0xfffd) || (c>=0x10000 && c<=0x10ffff);
}
void pd_utf8(fz_context *ctx, fz_buffer *b, int c) {
    char s[8];
    if(!pd_xml_scalar(c)) c=0xfffd;
    int n=fz_runetochar(s,c);
    fz_append_data(ctx,b,s,(size_t)n);
}
void pd_xml(fz_context *ctx, fz_buffer *b, const char *s) {
    while(*s) {
        int c;s+=fz_chartorune(&c,s);
        switch(c) {
        case '&': fz_append_string(ctx,b,"&amp;");break;
        case '<': fz_append_string(ctx,b,"&lt;");break;
        case '>': fz_append_string(ctx,b,"&gt;");break;
        case '"': fz_append_string(ctx,b,"&quot;");break;
        default: pd_utf8(ctx,b,c);
        }
    }
}
void pd_json(fz_context *ctx, fz_buffer *b, const char *s) {
    fz_append_byte(ctx,b,'"');
    while(*s) {
        int c;s+=fz_chartorune(&c,s);
        if(c=='"' || c=='\\') { fz_append_byte(ctx,b,'\\');fz_append_byte(ctx,b,c); }
        else if(c<32) fz_append_printf(ctx,b,"\\u%04x",c);
        else pd_utf8(ctx,b,c);
    }
    fz_append_byte(ctx,b,'"');
}
const char *pd_kind_name(PD_Kind k) {
    static const char *names[]={"Text","Heading","Table","Figure","Equation","Note","Margin","Page image","Rule","Publication"};
    return (k>=PD_TEXT && k<=PD_DETAIL)?names[k]:"Unknown";
}
void pd_check(Document *d,const char *s,int n,int total) {
    if(d->opt && d->opt->progress && d->opt->progress(d->opt->user,s,n,total))
        fz_throw(d->ctx,FZ_ERROR_ABORT,"Conversion cancelled");
}
void pd_free_result(PD_Result *r) {
    if(!r)return;
    for(int i=0;i<r->recognition_count;i++){free(r->recognitions[i].raw);free(r->recognitions[i].markup);}
    free(r->recognitions);free(r->blocks);free(r);
}
void pd_source_digest(Document *d,const char *path) {
    FILE *file=fopen(path,"rb");if(!file)fz_throw(d->ctx,FZ_ERROR_SYSTEM,"Cannot read PDF identity");
    fz_sha256 sha;unsigned char buffer[65536],digest[32];fz_sha256_init(&sha);size_t size;
    while((size=fread(buffer,1,sizeof(buffer),file)))fz_sha256_update(&sha,buffer,size);
    int failed=ferror(file);fclose(file);
    if(failed)fz_throw(d->ctx,FZ_ERROR_SYSTEM,"Cannot finish PDF identity");
    fz_sha256_final(&sha,digest);
    for(int i=0;i<32;i++)snprintf(d->source_digest+i*2,3,"%02x",digest[i]);
}
