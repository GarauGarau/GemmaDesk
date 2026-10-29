/* AGPL-3.0-or-later. Adapter to the unmodified Paperdesk algorithmic core. */
#include "internal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

static void picture(Document *d, int number, PD_Rect area, const char *path) {
    fz_context *c=d->ctx; fz_page *page=NULL; fz_pixmap *pix=NULL; fz_device *dev=NULL;
    fz_var(page);fz_var(pix);fz_var(dev);
    fz_try(c) {
        page=fz_load_page(c,d->pdf,number);
        float longest=fmaxf(area.x1-area.x0,area.y1-area.y0);
        if(longest<=0)fz_throw(c,FZ_ERROR_ARGUMENT,"Empty image area");
        float scale=fminf(2.0f,1344.0f/longest);
        fz_matrix m=fz_scale(scale,scale);
        fz_irect bounds=fz_round_rect(fz_transform_rect(to_fz(area),m));
        pix=fz_new_pixmap_with_bbox(c,fz_device_rgb(c),bounds,NULL,0);
        fz_clear_pixmap_with_value(c,pix,255);
        dev=fz_new_draw_device(c,m,pix);fz_run_page(c,page,dev,fz_identity,NULL);
        fz_close_device(c,dev);fz_save_pixmap_as_png(c,pix,path);
    }
    fz_always(c){fz_drop_device(c,dev);fz_drop_pixmap(c,pix);fz_drop_page(c,page);}
    fz_catch(c){fz_rethrow(c);}
}
int main(int argc,char **argv) {
    if(argc!=3 && argc!=6){fprintf(stderr,"extract input info | extract input first last output-dir json\n");return 2;}
    Document d={0};PD_Options opt={0};d.opt=&opt;
    d.ctx=fz_new_context(NULL,NULL,96<<20);if(!d.ctx)return 1;
    fz_context *c=d.ctx;fz_register_document_handlers(c);int result=0;fz_var(result);
    fz_buffer *out=NULL;fz_var(out);
    fz_try(c) {
        d.pdf=fz_open_document(c,argv[1]);
        if(fz_needs_password(c,d.pdf))fz_throw(c,FZ_ERROR_ARGUMENT,"PDF protetto da password");
        d.physical_pages=fz_count_pages(c,d.pdf);
        fz_lookup_metadata(c,d.pdf,FZ_META_INFO_TITLE,d.title,sizeof(d.title));
        out=fz_new_buffer(c,4096);
        fz_append_printf(c,out,"{\"pages\":%d,\"title\":",d.physical_pages);pd_json(c,out,d.title);
        if(argc==6) {
            opt.first_page=atoi(argv[2]);opt.last_page=atoi(argv[3]);
            if(opt.last_page-opt.first_page>7)fz_throw(c,FZ_ERROR_ARGUMENT,"Maximum 8 pages per batch");
            /* Zero-initialized model option: no Paperdesk model is loaded. */
            pd_extract(&d);pd_layout(&d);
            fz_append_string(c,out,",\"items\":[");
            for(int i=0;i<d.npage;i++) {
                Page *p=&d.pages[i];int page=p->number+1;char filename[4096];
                snprintf(filename,sizeof(filename),"%s/page-%d.png",argv[4],page);picture(&d,p->number,p->box,filename);
                fz_append_printf(c,out,"%s{\"page\":%d,\"blocks\":[",i?",":"",page);
                for(int j=0;j<p->nblock;j++) {
                    Block *b=&p->blocks[j];
                    fz_append_printf(c,out,"%s{\"kind\":%d,\"reason\":",j?",":"",b->pub.kind);pd_json(c,out,b->pub.reason);
                    fz_append_string(c,out,",\"text\":");
                    fz_buffer *text=fz_new_buffer(c,1024);
                    for(int k=0;k<b->n;k++){Line *l=&p->lines[b->lines[k]];if(k)fz_append_byte(c,text,'\n');fz_append_string(c,text,l->text?l->text:"");}
                    pd_json(c,out,fz_string_from_buffer(c,text));fz_drop_buffer(c,text);
                    fz_append_string(c,out,",\"image\":");
                    if((b->pub.kind==PD_FIGURE || b->pub.kind==PD_TABLE || b->pub.kind==PD_EQUATION) && rect_area(b->pub.box)>100) {
                        char base[100];snprintf(base,sizeof(base),"crop-%d-%d.png",page,j);
                        snprintf(filename,sizeof(filename),"%s/%s",argv[4],base);picture(&d,p->number,b->pub.box,filename);pd_json(c,out,base);
                    }else fz_append_string(c,out,"null");
                    fz_append_byte(c,out,'}');
                }
                fz_append_string(c,out,"]}");
            }
            fz_append_byte(c,out,']');
        }
        fz_append_byte(c,out,'}');
        puts(fz_string_from_buffer(c,out));
    }
    fz_catch(c){fprintf(stderr,"%s\n",fz_caught_message(c));result=1;}
    fz_drop_buffer(c,out);pd_drop_document(&d);fz_drop_context(c);return result;
}
