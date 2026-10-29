#include "internal.h"
#include "reader.h"
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

static int math_font(const char *name) {
    char s[160];size_t i;
    for(i=0;name[i] && i<sizeof(s)-1;i++) s[i]=(char)tolower((unsigned char)name[i]);
    s[i]=0;
    const char *tokens[]={"cmmi","cmsy","cmex","msam","msbm","symbol","math","mtmi","mtsy","rsfs","cmr10+","stixnonuni"};
    for(i=0;i<sizeof(tokens)/sizeof(tokens[0]);i++) if(strstr(s,tokens[i])) return 1;
    return 0;
}
static int math_code(int c) {
    return (c>=0x2200 && c<=0x22ff) || (c>=0x1d400 && c<=0x1d7ff) ||
           (c>=0x370 && c<=0x3ff) || c==0x00b1 || c==0x00d7 || c==0x2212;
}
static void add_mark(Document *d,Page *p,fz_stext_block *b) {
    /* Publishers sometimes leave prepress drawings far outside the CropBox.
     * They have no visible ink on this page and are not extractable objects. */
    PD_Rect r=from_fz(fz_intersect_rect(b->bbox,to_fz(p->box)));
    if(rect_area(r)<0.01f) return;
    if(b->type==FZ_STEXT_BLOCK_VECTOR) {
        unsigned int c=b->u.v.argb;
        if((c&0xffffff)==0xffffff || (c>>24)==0) return;
    }
    if(p->nmark>=12000) { p->complex_graphics=1;return; }
    p->marks=pd_grow(d->ctx,p->marks,&p->cmark,p->nmark+1,sizeof(Mark));
    p->marks[p->nmark++]=(Mark){r,b->type==FZ_STEXT_BLOCK_IMAGE,-1};
}
static void add_line(Document *d,Page *p,fz_stext_line *src,int source_block) {
    if(!src->first_char) return;
    if(p->nline>=20000) fz_throw(d->ctx,FZ_ERROR_LIMIT,"Too many text lines on page %d",p->number+1);
    p->lines=pd_grow(d->ctx,p->lines,&p->cline,p->nline+1,sizeof(Line));
    Line *l=&p->lines[p->nline++];memset(l,0,sizeof(*l));
    l->owner=-1;l->source_block=source_block;l->box=from_fz(src->bbox);
    l->dx=src->dir.x;l->dy=src->dir.y;
    float sizes[512]={0};float baseline_sum=0;int base_count=0;
    fz_buffer *text=fz_new_buffer(d->ctx,128);
    fz_try(d->ctx) {
        int inword=0;
        fz_font *font=NULL;int font_math=0,font_flags=0,font_variant=0;
        for(fz_stext_char *c=src->first_char;c;c=c->next) {
            /* Font properties are constant across a run of glyphs. */
            if(c==src->first_char || c->font!=font) {
                font=c->font;font_math=math_font(fz_font_name(d->ctx,font));
                font_variant=pd_math_font_variant(fz_font_name(d->ctx,font));
                font_flags=(fz_font_is_bold(d->ctx,font)?G_BOLD:0) |
                           (fz_font_is_italic(d->ctx,font)?G_ITALIC:0);
            }
            l->glyph=pd_grow(d->ctx,l->glyph,&l->cap,l->n+1,sizeof(Glyph));
            Glyph *g=&l->glyph[l->n++];
            *g=(Glyph){c->c,0,from_fz(fz_rect_from_quad(c->quad)),c->size,c->origin.y,0,0};
            if(c->flags&FZ_STEXT_SYNTHETIC)g->flags|=G_SYNTHETIC;
            int punctuation=c->c>0 && c->c<128 && strchr("()[]{}.,;:!?'\" ",c->c);
            if((!punctuation && font_math) || math_code(c->c)) { g->flags|=G_MATH;l->math++; }
            g->flags|=font_flags;
            if(c->c>='A' && c->c<='Z')g->flags|=font_variant;
            if(font_flags&G_BOLD)l->bold++;
            if(!pd_xml_scalar(c->c) || c->c==0xfffd || c->c<32 ||
               (c->c>=0xe000 && c->c<=0xf8ff)) { g->flags|=G_BAD;l->bad++; }
            if(c->c>32) {
                int bin=(int)lroundf(c->size*4);
                if(bin>0 && bin<512) sizes[bin]++;
                if(c->c<128 && isalpha(c->c)) l->letters++;
                if(c->c>='0' && c->c<='9') l->digits++;
                if(!inword) l->words++;
                inword=1;baseline_sum+=c->origin.y;base_count++;
            } else inword=0;
            pd_utf8(d->ctx,text,c->c);
        }
        int best=40;for(int i=1;i<512;i++) if(sizes[i]>sizes[best]) best=i;
        l->size=best/4.0f;
        l->baseline=base_count?baseline_sum/base_count:src->first_char->origin.y;
        l->text=fz_strdup(d->ctx,fz_string_from_buffer(d->ctx,text));
    }
    fz_always(d->ctx) fz_drop_buffer(d->ctx,text);
    fz_catch(d->ctx) fz_rethrow(d->ctx);
    /* Some Word PDFs contain empty text objects made solely of spaces.
     * They have no visible bounds and must not become empty EPUB blocks. */
    if(!l->words && !l->bad) {
        fz_free(d->ctx,l->glyph);fz_free(d->ctx,l->text);memset(l,0,sizeof(*l));p->nline--;
    }
}
void pd_extract(Document *d) {
    fz_context *ctx=d->ctx;
    int first=d->opt && d->opt->first_page>0?d->opt->first_page-1:0;
    int last=d->opt && d->opt->last_page>0?d->opt->last_page:d->physical_pages;
    if(first>=d->physical_pages || last> d->physical_pages || last<=first)
        fz_throw(ctx,FZ_ERROR_ARGUMENT,"Invalid page range");
    d->npage=last-first;
    d->pages=fz_calloc(ctx,d->npage,sizeof(Page));
    float size_hist[512]={0};
    for(int i=0;i<d->npage;i++) {
        pd_check(d,"Reading pages",i,d->npage);
        Page *p=&d->pages[i];p->number=first+i;
        fz_page *page=NULL;fz_stext_page *text=NULL;
        fz_var(page);fz_var(text);
        fz_try(ctx) {
            page=fz_load_page(ctx,d->pdf,p->number);
            p->box=from_fz(fz_bound_page(ctx,page));
            fz_stext_options opts={0};
            opts.flags=FZ_STEXT_PRESERVE_IMAGES|FZ_STEXT_COLLECT_VECTORS|FZ_STEXT_ACCURATE_BBOXES|FZ_STEXT_CLIP;
            text=fz_new_stext_page_from_page(ctx,page,&opts);
            int source=0;
            for(fz_stext_block *b=text->first_block;b;b=b->next,source++) {
                if(b->type==FZ_STEXT_BLOCK_TEXT)
                    for(fz_stext_line *l=b->u.t.first_line;l;l=l->next) add_line(d,p,l,source);
                else if(b->type==FZ_STEXT_BLOCK_IMAGE || b->type==FZ_STEXT_BLOCK_VECTOR) add_mark(d,p,b);
            }
            for(int j=0;j<p->nline;j++) {
                Line *l=&p->lines[j];int bin=(int)lroundf(l->size*4);
                if(bin>0 && bin<512 && fabsf(l->dy)<0.1f && l->letters>8) size_hist[bin]+=l->letters;
            }
        }
        fz_always(ctx) { fz_drop_stext_page(ctx,text);fz_drop_page(ctx,page); }
        fz_catch(ctx) fz_rethrow(ctx);
        if(p->nmark) {
            p->source_marks=fz_malloc_array(ctx,p->nmark,Mark);p->nsource_mark=p->nmark;
            memcpy(p->source_marks,p->marks,(size_t)p->nmark*sizeof(Mark));
        }
    }
    int best=44;for(int i=20;i<160;i++) if(size_hist[i]>size_hist[best]) best=i;
    d->body=best/4.0f;
}
void pd_drop_document(Document *d) {
    if(!d || !d->ctx) return;
    pd_model_close(d);
    for(int i=0;d->pages && i<d->npage;i++) {
        Page *p=&d->pages[i];
        for(int j=0;j<p->nline;j++) { fz_free(d->ctx,p->lines[j].glyph);fz_free(d->ctx,p->lines[j].text); }
        for(int j=0;j<p->nblock;j++) fz_free(d->ctx,p->blocks[j].lines);
        fz_free(d->ctx,p->lines);fz_free(d->ctx,p->marks);fz_free(d->ctx,p->source_marks);fz_free(d->ctx,p->blocks);
        fz_free(d->ctx,p->hints);fz_free(d->ctx,p->model_output);fz_free(d->ctx,p->baseline);
        for(int j=0;j<p->nrecon;j++){fz_free(d->ctx,p->recon[j].raw);fz_free(d->ctx,p->recon[j].markup);}
        fz_free(d->ctx,p->recon);
    }
    fz_free(d->ctx,d->pages);
    fz_free(d->ctx,d->refs);
    fz_drop_document(d->ctx,d->pdf);
}

struct PD_View { fz_context *ctx;fz_document *doc;fz_buffer *asset;int pages,reflow;void *reader; };
static PD_View *view_new(char *err,size_t len) {
    PD_View *v=calloc(1,sizeof(*v));
    if(v) v->ctx=fz_new_context(NULL,NULL,96<<20);
    if(!v || !v->ctx) { free(v);snprintf(err,len,"Not enough memory");return NULL; }
    fz_register_document_handlers(v->ctx);
    return v;
}
PD_View *pd_view_open(const char *path,char *err,size_t len) {
    PD_View *v=view_new(err,len);if(!v)return NULL;
    fz_try(v->ctx) {
        v->doc=fz_open_document(v->ctx,path);
        if(fz_needs_password(v->ctx,v->doc)) fz_throw(v->ctx,FZ_ERROR_ARGUMENT,"This PDF requires a password");
        v->reflow=fz_is_document_reflowable(v->ctx,v->doc);
        if(v->reflow) {
            fz_archive *zip=NULL;fz_buffer *opf=NULL;fz_var(zip);fz_var(opf);
            fz_try(v->ctx) {
                zip=fz_open_archive(v->ctx,path);
                if(fz_has_archive_entry(v->ctx,zip,"OEBPS/package.opf")) {
                    opf=fz_read_archive_entry(v->ctx,zip,"OEBPS/package.opf");
                    const char *metadata=fz_string_from_buffer(v->ctx,opf);
                    if(strstr(metadata,"name=\"paperdesk-reader\" content=\"koreader\"") || strstr(metadata,"name=\"paperdesk-reader\" content=\"kobo\"")) {
                        v->reader=pd_reader_open(path,err,len);
                        if(!v->reader)fz_throw(v->ctx,FZ_ERROR_GENERIC,"%s",err);
                        v->pages=pd_reader_pages(v->reader);
                    }
                }
            }
            fz_always(v->ctx){fz_drop_buffer(v->ctx,opf);fz_drop_archive(v->ctx,zip);}
            fz_catch(v->ctx)fz_rethrow(v->ctx);
        }
        if(!v->reader) {
            if(v->reflow)fz_layout_document(v->ctx,v->doc,440,640,14);
            v->pages=fz_count_pages(v->ctx,v->doc);
        }
    }
    fz_catch(v->ctx) { snprintf(err,len,"%s",fz_caught_message(v->ctx));pd_view_close(v);return NULL; }
    return v;
}
PD_View *pd_view_asset(const char *epub,const char *entry,char *err,size_t len) {
    PD_View *v=view_new(err,len);if(!v)return NULL;
    fz_archive *zip=NULL;fz_stream *stream=NULL;fz_var(zip);fz_var(stream);
    fz_try(v->ctx) {
        zip=fz_open_archive(v->ctx,epub);
        v->asset=fz_read_archive_entry(v->ctx,zip,entry);
        stream=fz_open_buffer(v->ctx,v->asset);
        v->doc=fz_open_document_with_stream(v->ctx,entry,stream);
        v->pages=fz_count_pages(v->ctx,v->doc);
    }
    fz_always(v->ctx) { fz_drop_stream(v->ctx,stream);fz_drop_archive(v->ctx,zip); }
    fz_catch(v->ctx) { snprintf(err,len,"%s",fz_caught_message(v->ctx));pd_view_close(v);return NULL; }
    return v;
}
void pd_view_close(PD_View *v) {
    if(v) { pd_reader_close(v->reader);fz_drop_document(v->ctx,v->doc);fz_drop_buffer(v->ctx,v->asset);fz_drop_context(v->ctx);free(v); }
}
int pd_view_pages(PD_View *v) { return v?v->pages:0; }
int pd_view_is_reflowable(PD_View *v) { return v?v->reflow:0; }
int pd_view_reflow(PD_View *v,float w,float h,float em) {
    if(!v || !v->reflow)return 0;
    if(v->reader){int ok=pd_reader_layout(v->reader,(int)w,(int)h,(int)em);v->pages=pd_reader_pages(v->reader);return ok;}
    fz_try(v->ctx) { fz_layout_document(v->ctx,v->doc,w,h,em);v->pages=fz_count_pages(v->ctx,v->doc); }
    fz_catch(v->ctx) { return 0; }
    return 1;
}
PD_Rect pd_view_bounds(PD_View *v,int number) {
    if(v && v->reader)return pd_reader_bounds(v->reader);
    PD_Rect r={0,0,612,792};fz_var(r);fz_page *p=NULL;fz_var(p);
    if(!v || number<0 || number>=v->pages)return r;
    fz_try(v->ctx) { p=fz_load_page(v->ctx,v->doc,number);r=from_fz(fz_bound_page(v->ctx,p)); }
    fz_always(v->ctx) fz_drop_page(v->ctx,p);
    fz_catch(v->ctx) {}
    return r;
}
int pd_view_resolve(PD_View *v,const char *anchor) {
    if(v && v->reader)return pd_reader_resolve(v->reader,anchor);
    int page=-1;fz_var(page);
    fz_try(v->ctx) { fz_location loc=fz_resolve_link(v->ctx,v->doc,anchor,NULL,NULL);page=fz_page_number_from_location(v->ctx,v->doc,loc); }
    fz_catch(v->ctx) {}
    return page;
}
int pd_view_link_at(PD_View *v,int number,float x,float y) {
    if(!v || number<0 || number>=v->pages || !isfinite(x) || !isfinite(y))return -1;
    if(v->reader)return pd_reader_link(v->reader,number,x,y);
    fz_page *page=NULL;fz_link *links=NULL;int target=-1;
    fz_var(page);fz_var(links);fz_var(target);
    fz_try(v->ctx) {
        page=fz_load_page(v->ctx,v->doc,number);links=fz_load_links(v->ctx,page);
        for(fz_link *l=links;l;l=l->next) {
            if(!l->uri || fz_is_external_link(v->ctx,l->uri))continue;
            fz_rect r=fz_expand_rect(l->rect,2);
            if(x>=r.x0 && x<=r.x1 && y>=r.y0 && y<=r.y1){target=pd_view_resolve(v,l->uri);break;}
        }
    }
    fz_always(v->ctx){fz_drop_link(v->ctx,links);fz_drop_page(v->ctx,page);}
    fz_catch(v->ctx){}
    return target;
}
static fz_pixmap *view_pixmap(PD_View *v,int n,float scale,int rotation) {
    if(n<0 || n>=v->pages || !isfinite(scale) || scale<=0)
        fz_throw(v->ctx,FZ_ERROR_ARGUMENT,"Invalid page or zoom");
    fz_matrix m=fz_pre_rotate(fz_scale(scale,scale),(float)rotation);
    PD_Rect r=pd_view_bounds(v,n);
    if(rect_area(r)*scale*scale>80000000.0) fz_throw(v->ctx,FZ_ERROR_LIMIT,"Zoom is too large");
    return fz_new_pixmap_from_page_number(v->ctx,v->doc,n,m,fz_device_rgb(v->ctx),0);
}
int pd_view_render(PD_View *v,int page,float scale,int rotation,PD_Bitmap *out,char *err,size_t len) {
    fz_pixmap *pix=NULL;fz_var(pix);
    if(!v || page<0 || page>=v->pages) {snprintf(err,len,"Page unavailable");return 0;}
    memset(out,0,sizeof(*out));
    if(v->reader)return pd_reader_render(v->reader,page,scale,rotation,out,err,len);
    int ok=0;fz_var(ok);
    fz_try(v->ctx) {
        pix=view_pixmap(v,page,scale,rotation);
        out->w=fz_pixmap_width(v->ctx,pix);out->h=fz_pixmap_height(v->ctx,pix);
        out->stride=out->w*3;out->scale=scale;
        out->samples=malloc((size_t)out->stride*out->h);
        if(!out->samples) fz_throw(v->ctx,FZ_ERROR_SYSTEM,"Not enough memory for preview");
        unsigned char *src=fz_pixmap_samples(v->ctx,pix);int stride=fz_pixmap_stride(v->ctx,pix);
        for(int y=0;y<out->h;y++)memcpy(out->samples+(size_t)y*out->stride,src+(size_t)y*stride,out->stride);
        ok=1;
    }
    fz_always(v->ctx) fz_drop_pixmap(v->ctx,pix);
    fz_catch(v->ctx) {snprintf(err,len,"%s",fz_caught_message(v->ctx));pd_bitmap_free(out);}
    return ok;
}
void pd_bitmap_free(PD_Bitmap *b) {free(b->samples);memset(b,0,sizeof(*b));}
int pd_bitmap_save(const PD_Bitmap *b,const char *path,char *err,size_t len) {
    fz_context *ctx=fz_new_context(NULL,NULL,0);if(!ctx)return 0;
    fz_pixmap *pix=NULL;fz_var(pix);int ok=0;fz_var(ok);
    fz_try(ctx) {
        pix=fz_new_pixmap_with_data(ctx,fz_device_rgb(ctx),b->w,b->h,NULL,0,b->stride,b->samples);
        fz_save_pixmap_as_png(ctx,pix,path);ok=1;
    }
    fz_always(ctx) fz_drop_pixmap(ctx,pix);
    fz_catch(ctx) {snprintf(err,len,"%s",fz_caught_message(ctx));}
    fz_drop_context(ctx);return ok;
}
int pd_view_snapshot(PD_View *v,int n,const char *path,float scale,char *err,size_t len) {
    if(v->reader){PD_Bitmap b={0};int ok=pd_view_render(v,n,scale,0,&b,err,len);if(ok)ok=pd_bitmap_save(&b,path,err,len);pd_bitmap_free(&b);return ok;}
    fz_pixmap *pix=NULL;fz_var(pix);int ok=0;fz_var(ok);
    fz_try(v->ctx) {pix=view_pixmap(v,n,scale,0);fz_save_pixmap_as_png(v->ctx,pix,path);ok=1;}
    fz_always(v->ctx) fz_drop_pixmap(v->ctx,pix);
    fz_catch(v->ctx) {snprintf(err,len,"%s",fz_caught_message(v->ctx));}
    return ok;
}
int pd_view_text(PD_View *v,int n,const char *path,char *err,size_t len) {
    if(v->reader)return pd_reader_text(v->reader,n,path);
    fz_buffer *b=NULL;fz_var(b);int ok=0;fz_var(ok);
    fz_try(v->ctx) {b=fz_new_buffer_from_page_number(v->ctx,v->doc,n,NULL);fz_save_buffer(v->ctx,b,path);ok=1;}
    fz_always(v->ctx) fz_drop_buffer(v->ctx,b);
    fz_catch(v->ctx) {snprintf(err,len,"%s",fz_caught_message(v->ctx));}
    return ok;
}
