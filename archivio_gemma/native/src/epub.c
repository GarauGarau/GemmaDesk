#include "internal.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

typedef struct {
    Document *doc;
    PD_Zip *zip;
    fz_buffer *manifest, *nav, *spine, *map;
    fz_display_list *artwork;
    int artwork_page;
    int asset_id,details;
    char current[64];
} Writer;

static const char *css =
"html{background:white;color:#111}body{font-family:serif;margin:5%;line-height:1.4}"
"p{margin:0 0 .5em;text-align:left;orphans:2;widows:2}"
"p.text{text-align:justify;text-align-last:left;hyphens:auto;-webkit-hyphens:auto}"
"p.indent{text-indent:1.2em}p.hanging{margin-left:1.2em;text-indent:-1.2em}"
"p.reference,p.caption{text-align:left;hyphens:manual}"
".book-title{text-align:center;font-size:1.6em}.author{text-align:center;text-indent:0}"
".publication{font-size:.85em;text-align:left}.publication p{overflow-wrap:anywhere}"
"h1,h2,h3,h4{line-height:1.25;text-align:left;break-after:avoid;page-break-after:avoid}"
"h1{font-size:1.5em}h2{font-size:1.25em}h3,h4{font-size:1.1em}"
".source{font-family:sans-serif;font-size:.65em;color:#666;margin:1em 0 .7em}"
".note{font-size:.85em}"
"sup,sub{font-size:.75em;line-height:0}.noteref{text-decoration:none}"
".footnote{margin:0 0 1em}.footnote p{font-size:1em;text-align:left}"
".note-label{font-weight:bold}.note-return{font-family:sans-serif;font-size:.8em}"
".object{margin:.9em 0;break-inside:avoid;page-break-inside:avoid;text-align:center}"
".object img{max-width:100%;height:auto}.object a{color:inherit;text-decoration:none}"
"math{font-family:'STIX Two Math',serif}.native-equation{margin:.8em 0;break-inside:avoid}.native-math{text-decoration:none}"
".native-detail{break-before:page;page-break-before:always}.native-table{border-collapse:collapse;margin:1em 0;font-size:1em;break-inside:avoid;page-break-inside:avoid;line-height:1.25}.native-table td,.native-table th{padding:.3em .1em;border:1px solid #888;white-space:nowrap;hyphens:none}.cell-content{display:inline-block;white-space:nowrap}"
".math{display:inline;max-width:100%}.object-note{font-family:sans-serif;font-size:.65em;color:#666}"
"html.object-document{height:100%;margin:0;padding:0}"
"body.object-page{height:100%;margin:0;padding:0;line-height:0}"
".object-page .object{height:95%;margin:0;break-inside:avoid;page-break-inside:avoid}"
".object-page.has-notes .object{height:85%}"
".object-note-links{font-family:sans-serif;font-size:.75em;line-height:1.3;text-align:center;margin:.25em 0}"
".object-page .object a{display:block;height:100%}"
/* Fit BOTH axes. CSS2 readers retain auto sizing with percentage maxima;
 * readers supporting min()/vh can also enlarge to fill the available area.
 * 69.666667vh = 95vh * 440/600, reserving room for the source-page footer. */
".object-page .object img{display:block;width:auto;width:min(100%,69.666667vh);height:auto;"
"max-width:100%;max-height:100%;object-fit:contain;margin:0 auto}"
".object-page.has-notes .object img{width:auto;width:min(100%,62.333333vh)}"
"a{color:inherit}nav ol{padding-left:1.3em}strong{font-weight:bold}em{font-style:italic}";

static void zip_buffer(Writer *w,const char *name,fz_buffer *b,int compress) {
    fz_context *ctx=w->doc->ctx;fz_buffer *kobo=NULL;fz_var(kobo);
    fz_try(ctx) {
        if(w->doc->opt && w->doc->opt->reader==PD_READER_KOBO && strstr(name,".xhtml"))kobo=pd_kepub_xhtml(ctx,b);
        pd_zip_entry(ctx,w->zip,name,kobo?kobo:b,compress);
    }
    fz_always(ctx)fz_drop_buffer(ctx,kobo);
    fz_catch(ctx)fz_rethrow(ctx);
}
static void zip_string(Writer *w,const char *name,const char *s,int compress) {
    fz_context *ctx=w->doc->ctx;
    fz_buffer *b=fz_new_buffer_from_copied_data(ctx,(const unsigned char*)s,strlen(s));
    fz_try(ctx) zip_buffer(w,name,b,compress);
    fz_always(ctx) fz_drop_buffer(ctx,b);
    fz_catch(ctx) fz_rethrow(ctx);
}
static void manifest(Writer *w,const char *id,const char *href,const char *type,const char *props) {
    fz_append_printf(w->doc->ctx,w->manifest,"<item id=\"%s\" href=\"%s\" media-type=\"%s\"%s/>",id,href,type,props?props:"");
}
static void xml_char(fz_context *ctx,fz_buffer *b,int c) {
    if(c=='&')fz_append_string(ctx,b,"&amp;");
    else if(c=='<')fz_append_string(ctx,b,"&lt;");
    else if(c=='>')fz_append_string(ctx,b,"&gt;");
    else if(c=='"')fz_append_string(ctx,b,"&quot;");
    else pd_utf8(ctx,b,c);
}
/* The pinned SVG writer emits numeric references in data-text attributes
 * even for invalid PDF Unicode mappings. Repair that metadata without
 * touching the glyph outlines. This handles its generated syntax only. */
static void svg_character_references(fz_context *ctx,fz_buffer **svg) {
    const char *start=fz_string_from_buffer(ctx,*svg),*copied=start;
    fz_buffer *clean=NULL;fz_var(clean);
    fz_try(ctx) {
        for(const char *p=start;(p=strstr(p,"&#x"));p+=3) {
            char *end;errno=0;unsigned long c=strtoul(p+3,&end,16);
            if(end==p+3 || *end!=';')continue;
            if(!errno && c<=0x10ffff && pd_xml_scalar((int)c))continue;
            if(!clean)clean=fz_new_buffer(ctx,fz_buffer_storage(ctx,*svg,NULL));
            fz_append_data(ctx,clean,copied,(size_t)(p-copied));
            fz_append_string(ctx,clean,"&#xfffd;");copied=end+1;
        }
        if(clean) {
            fz_append_string(ctx,clean,copied);fz_drop_buffer(ctx,*svg);
            *svg=clean;clean=NULL;
        }
    }
    fz_always(ctx)fz_drop_buffer(ctx,clean);
    fz_catch(ctx)fz_rethrow(ctx);
}

/* Render from the display list rather than copying an embedded bitmap.
 * The same crop and transformation drive PNG and outlined SVG. */
static void describe_artwork(Writer *w,Page *page,Block *block,fz_buffer **svg) {
    if(!block)return;
    fz_context *ctx=w->doc->ctx;fz_buffer *out=fz_new_buffer(ctx,4096);
    fz_try(ctx) {
        unsigned char *data;size_t length=fz_buffer_storage(ctx,*svg,&data);
        unsigned char *end=memchr(data,'>',length);
        if(!end)fz_throw(ctx,FZ_ERROR_FORMAT,"Invalid SVG output");
        size_t prefix=(size_t)(end-data)+1;fz_append_data(ctx,out,data,prefix);
        fz_append_string(ctx,out,"\n<title>");pd_xml(ctx,out,block->pub.label);
        fz_append_string(ctx,out,"</title><desc>");
        /* Source text, not an inferred cell structure. Keep signs, stars,
         * labels and notes available alongside the outlined artwork. */
        for(int i=0;i<block->n;i++) {
            if(i)fz_append_byte(ctx,out,'\n');
            pd_xml(ctx,out,page->lines[block->lines[i]].text);
        }
        fz_append_string(ctx,out,"</desc>");fz_append_data(ctx,out,data+prefix,length-prefix);
        fz_drop_buffer(ctx,*svg);*svg=out;out=NULL;
    }
    fz_always(ctx)fz_drop_buffer(ctx,out);
    fz_catch(ctx)fz_rethrow(ctx);
}
/* This checks the syntax emitted by our pinned SVG writer, not arbitrary SVG.
 * The bundled reader cannot reproduce masks, patterns or group compositing.
 * Keep the vector file, but show the PDF-rendered PNG for those objects. */
static int svg_preview_supported(fz_context *ctx,fz_buffer *svg) {
    const char *s=fz_string_from_buffer(ctx,svg);
    const char *unsupported[]={"<mask","<pattern","<linearGradient","<radialGradient","<filter","<foreignObject","mix-blend-mode:"};
    for(size_t i=0;i<sizeof(unsupported)/sizeof(*unsupported);i++)
        if(strstr(s,unsupported[i]))return 0;
    for(const char *g=s;(g=strstr(g,"<g"));g++) {
        const char *end=strchr(g,'>');if(!end)return 0;
        for(const char *p=g;end-p>=9;p++)if(!memcmp(p," opacity=",9))return 0;
        g=end;
    }
    return 1;
}
static int write_asset(Writer *w,Page *page,Block *block,PD_Rect crop,int angle,int portrait_frame,int raster_needed,char *base,size_t base_size) {
    fz_context *ctx=w->doc->ctx;
    /* Interpret graphics only when an asset needs them. Keep one source page
     * for all its crops, including successive notes from the same page. */
    if(!w->artwork || w->artwork_page!=page->number) {
        fz_drop_display_list(ctx,w->artwork);w->artwork=NULL;
        w->artwork=fz_new_display_list_from_page_number(ctx,w->doc->pdf,page->number);
        w->artwork_page=page->number;
    }
    if(crop.x1-crop.x0<.5f)crop.x1=crop.x0+.5f;
    if(crop.y1-crop.y0<.5f)crop.y1=crop.y0+.5f;
    fz_matrix m=fz_rotate((float)angle);
    fz_rect rotated=fz_transform_rect(to_fz(crop),m);
    m=fz_concat(m,fz_translate(-rotated.x0,-rotated.y0));
    float width=rotated.x1-rotated.x0,height=rotated.y1-rotated.y0;
    if(portrait_frame) {
        float fit=fminf(440.0f/width,600.0f/height);
        m=fz_concat(m,fz_scale(fit,fit));
        m=fz_concat(m,fz_translate((440-width*fit)/2,(600-height*fit)/2));
        width=440;height=600;
    }
    char png_path[120],svg_path[120],id[64];
    int aid=++w->asset_id;
    snprintf(base,base_size,"assets/r%06d",aid);
    snprintf(png_path,sizeof(png_path),"OEBPS/%s.png",base);
    snprintf(svg_path,sizeof(svg_path),"OEBPS/%s.svg",base);
    fz_buffer *svg=NULL,*png=NULL;fz_output *out=NULL;fz_device *dev=NULL;
    fz_pixmap *pix=NULL;fz_path *clip=NULL;
    int vector_preview=1;fz_var(vector_preview);
    fz_var(svg);fz_var(png);fz_var(out);fz_var(dev);fz_var(pix);fz_var(clip);
    fz_try(ctx) {
        svg=fz_new_buffer(ctx,4096);
        out=fz_new_output_with_buffer(ctx,svg);
        dev=fz_new_svg_device(ctx,out,width,height,FZ_SVG_TEXT_AS_PATH,1);
        /* Clip the SOURCE region, not only the destination canvas. Centering
         * an object in a portrait frame creates padding where adjacent PDF
         * paragraphs would otherwise reappear, often cut in half. */
        clip=fz_new_path(ctx);fz_rectto(ctx,clip,crop.x0,crop.y0,crop.x1,crop.y1);
        fz_clip_path(ctx,dev,clip,0,m,fz_make_rect(0,0,width,height));
        fz_run_display_list(ctx,w->artwork,dev,m,fz_make_rect(0,0,width,height),NULL);
        fz_pop_clip(ctx,dev);fz_close_device(ctx,dev);fz_drop_device(ctx,dev);dev=NULL;
        fz_close_output(ctx,out);fz_drop_output(ctx,out);out=NULL;
        svg_character_references(ctx,&svg);
        vector_preview=svg_preview_supported(ctx,svg);
        describe_artwork(w,page,block,&svg);
        zip_buffer(w,svg_path,svg,1);
        /* Native MathML needs only a vector source reference. Rasterize
         * when the reading flow or unsupported SVG features require it.
         * 216 dpi by default. Large objects retain an outlined SVG even
         * where a bounded preview raster is necessary. */
        if(raster_needed || !vector_preview) {
        float scale=fminf(3.0f,6000.0f/fmaxf(width,height));
        if(width*height*scale*scale>24000000.f)scale=sqrtf(24000000.f/(width*height));
        fz_irect box=fz_round_rect(fz_make_rect(0,0,width*scale,height*scale));
        pix=fz_new_pixmap_with_bbox(ctx,fz_device_rgb(ctx),box,NULL,0);
        fz_clear_pixmap_with_value(ctx,pix,255);
        dev=fz_new_draw_device(ctx,fz_identity,pix);
        fz_matrix raster=fz_concat(m,fz_scale(scale,scale));
        fz_clip_path(ctx,dev,clip,0,raster,fz_rect_from_irect(box));
        fz_run_display_list(ctx,w->artwork,dev,raster,fz_rect_from_irect(box),NULL);
        fz_pop_clip(ctx,dev);
        fz_close_device(ctx,dev);fz_drop_device(ctx,dev);dev=NULL;
        png=fz_new_buffer_from_pixmap_as_png(ctx,pix,fz_default_color_params);
        zip_buffer(w,png_path,png,0);
        }
        snprintf(id,sizeof(id),"svg%d",aid);char relative[112];snprintf(relative,sizeof(relative),"%s.svg",base);
        manifest(w,id,relative,"image/svg+xml",NULL);
        if(png) {
            snprintf(id,sizeof(id),"png%d",aid);snprintf(relative,sizeof(relative),"%s.png",base);
            manifest(w,id,relative,"image/png",NULL);
        }
    }
    fz_always(ctx) {
        fz_drop_path(ctx,clip);fz_drop_device(ctx,dev);fz_drop_output(ctx,out);
        fz_drop_pixmap(ctx,pix);fz_drop_buffer(ctx,png);fz_drop_buffer(ctx,svg);
    }
    fz_catch(ctx) fz_rethrow(ctx);
    return vector_preview;
}
static void native_math(Writer *w,fz_buffer *html,const char *markup) {
    fz_context *ctx=w->doc->ctx;
    char *math=pd_math_flow(ctx,markup,w->doc->opt->reader);
    fz_try(ctx)fz_append_string(ctx,html,math);
    fz_always(ctx)fz_free(ctx,math);
    fz_catch(ctx)fz_rethrow(ctx);
}
static void text_line(Writer *w,Page *p,Line *l,fz_buffer *html,int start) {
    fz_context *ctx=w->doc->ctx;
    for(int i=start;i<l->n;) {
        Glyph *g=&l->glyph[i];
        if(g->ref) {
            NoteRef *ref=&w->doc->refs[g->ref-1];
            snprintf(ref->anchor,sizeof(ref->anchor),"%s#ref%d",w->current,g->ref);
            if(g->flags&G_GAP)fz_append_byte(ctx,html,' ');
            fz_append_printf(ctx,html,"<a class=\"noteref\" epub:type=\"noteref\" role=\"doc-noteref\" id=\"ref%d\" href=\"notes.xhtml#fn%d\"><sup>",g->ref,ref->note);
            while(i<ref->end)xml_char(ctx,html,l->glyph[i++].c);
            fz_append_string(ctx,html,"</sup></a>");continue;
        }
        if(g->flags&G_INLINE) {
            if(g->flags&G_GAP)fz_append_byte(ctx,html,' ');
            int end=i+1;PD_Rect box=g->box;
            while(end<l->n && (l->glyph[end].flags&G_INLINE))box=rect_union(box,l->glyph[end++].box);
            box=rect_pad(box,.8f);
            for(int j=0;j<p->nmark;j++)
                if(rect_overlap(box,p->marks[j].box) && rect_area(p->marks[j].box)<rect_area(box)*3)
                    box=rect_union(box,p->marks[j].box);
            Reconstruction *r=g->native?&p->recon[g->native-1]:NULL;
            int native=w->doc->opt && w->doc->opt->reader && r && r->accepted && r->markup;
            if(native) {
                native_math(w,html,r->markup);
                r->visible=1;i=end;continue;
            }
            char asset[96];write_asset(w,p,NULL,box,0,0,1,asset,sizeof(asset));
            float h=(box.y1-box.y0)/l->size;
            float width=(box.x1-box.x0)/l->size;
            float descent=fmaxf(0,box.y1-l->baseline)/l->size;
            fz_append_printf(ctx,html,"<img class=\"math\" src=\"%s.png\" style=\"width:%.4fem;height:%.4fem;vertical-align:-%.4fem\" alt=\"",asset,width,h,descent);
            for(int k=i;k<end;k++)xml_char(ctx,html,l->glyph[k].c);
            fz_append_string(ctx,html,"\"/>");i=end;continue;
        }
        int style=g->flags&(G_BOLD|G_ITALIC|G_UNDERLINE),script=pd_glyph_script(l,g);
        if(script)fz_append_string(ctx,html,script>0?"<sup>":"<sub>");
        if(style&G_UNDERLINE)fz_append_string(ctx,html,"<span style=\"text-decoration:underline\">");
        if(style&G_BOLD)fz_append_string(ctx,html,"<strong>");
        if(style&G_ITALIC)fz_append_string(ctx,html,"<em>");
        do {
            if(l->glyph[i].flags&G_GAP)fz_append_byte(ctx,html,' ');
            xml_char(ctx,html,l->glyph[i].c);i++;
        }while(i<l->n && !l->glyph[i].ref && !(l->glyph[i].flags&G_INLINE) && pd_glyph_script(l,&l->glyph[i])==script &&
               (l->glyph[i].flags&(G_BOLD|G_ITALIC|G_UNDERLINE))==style);
        if(style&G_ITALIC)fz_append_string(ctx,html,"</em>");
        if(style&G_BOLD)fz_append_string(ctx,html,"</strong>");
        if(style&G_UNDERLINE)fz_append_string(ctx,html,"</span>");
        if(script)fz_append_string(ctx,html,script>0?"</sup>":"</sub>");
    }
}
static void map_block(Writer *w,PD_Block *s,int comma) {
    fz_context *ctx=w->doc->ctx;
    if(comma)fz_append_byte(ctx,w->map,',');
    fz_append_printf(ctx,w->map,"{\"id\":%d,\"page\":%d,\"kind\":",s->id,s->page+1);
    pd_json(ctx,w->map,pd_kind_name(s->kind));
    fz_append_printf(ctx,w->map,",\"bbox\":[%.4f,%.4f,%.4f,%.4f],\"rotation\":%d,\"page_rotation\":%d,\"glyphs\":%d,\"review\":%s,\"level\":%d,\"label\":",
                     s->box.x0,s->box.y0,s->box.x1,s->box.y1,s->rotation,s->page_rotation,s->glyphs,s->review?"true":"false",s->level);
    pd_json(ctx,w->map,s->omitted?"Excluded section":s->label);
    fz_append_printf(ctx,w->map,",\"section\":%d,\"omitted\":%s,\"continues\":%d,\"note\":%d,\"front\":%d,\"reason\":",s->section,s->omitted?"true":"false",s->continues,s->note,s->front);
    pd_json(ctx,w->map,s->omitted?"Excluded by conversion options":s->reason);
    fz_append_printf(ctx,w->map,",\"native\":%d",s->native);
    fz_append_string(ctx,w->map,",\"anchor\":");pd_json(ctx,w->map,s->anchor);
    fz_append_string(ctx,w->map,",\"asset\":");pd_json(ctx,w->map,s->asset);
    fz_append_string(ctx,w->map,",\"reader_asset\":");pd_json(ctx,w->map,s->reader_asset);
    fz_append_string(ctx,w->map,"}");
}
typedef struct {
    fz_buffer *html;
    int part,number,object,paragraph,last_char;
    char relative[64];
} PageOutput;
static void start_part(Writer *w,Page *p,PageOutput *out,int object,int notes) {
    fz_context *ctx=w->doc->ctx;
    out->part=++p->parts;out->number=p->number;out->object=object;
    if(out->part==1)snprintf(out->relative,sizeof(out->relative),"p%04d.xhtml",p->number+1);
    else snprintf(out->relative,sizeof(out->relative),"p%04d-s%d.xhtml",p->number+1,out->part);
    snprintf(w->current,sizeof(w->current),"%s",out->relative);
    out->html=fz_new_buffer(ctx,4096);
    fz_append_printf(ctx,out->html,"<?xml version=\"1.0\" encoding=\"utf-8\"?><html xmlns=\"http://www.w3.org/1999/xhtml\" xmlns:epub=\"http://www.idpf.org/2007/ops\" lang=\"en\" class=\"%s\"><head><title>",object?"object-document":"article-document");
    pd_xml(ctx,out->html,w->doc->title);
    fz_append_printf(ctx,out->html,"</title><link rel=\"stylesheet\" type=\"text/css\" href=\"style.css\"/></head><body class=\"%s%s\">",object?"object-page":"article",object && notes?" has-notes":"");
}
static void finish_part(Writer *w,PageOutput *out) {
    if(!out->html)return;
    fz_context *ctx=w->doc->ctx;char path[96],id[64];
    if(out->paragraph){fz_append_string(ctx,out->html,"</p>");out->paragraph=0;}
    fz_append_string(ctx,out->html,"</body></html>");
    snprintf(path,sizeof(path),"OEBPS/%s",out->relative);zip_buffer(w,path,out->html,1);
    snprintf(id,sizeof(id),"p%d-s%d",out->number+1,out->part);
    manifest(w,id,out->relative,"application/xhtml+xml",strstr(fz_string_from_buffer(ctx,out->html),"<math ")?" properties=\"mathml\"":NULL);
    fz_append_printf(ctx,w->spine,"<itemref idref=\"%s\"/>",id);
    fz_drop_buffer(ctx,out->html);out->html=NULL;
}
static int turn_for_reader(Writer *w,PD_Block *b) {
    if(!w->doc->opt || !w->doc->opt->rotate_wide)return 0;
    if(b->kind!=PD_TABLE && b->kind!=PD_FIGURE)return 0;
    if(strstr(b->reason,"Unmapped"))return 0;
    float width=b->box.x1-b->box.x0,height=b->box.y1-b->box.y0;
    if(b->rotation==90 || b->rotation==270){float t=width;width=height;height=t;}
    if(width<height || height<w->doc->body*3)return 0;
    float straight=fminf(440/width,600/height),turned=fminf(440/height,600/width);
    return turned>straight*1.12f;
}
static int block_ref(Document *d,Page *p,Block *b,int ri) {
    NoteRef *ref=&d->refs[ri];if(ref->omitted || ref->page!=(int)(p-d->pages))return 0;
    for(int i=0;i<b->n;i++)if(b->lines[i]==ref->line)return 1;
    return 0;
}
static int has_native_detail(Document *d,Page *p,int block);
static void write_page(Writer *w,Page *p,PageOutput *out,PD_Result *result,int *bi) {
    fz_context *ctx=w->doc->ctx;int begin=*bi;
    for(int i=0;i<p->nblock;i++) {
        pd_check(w->doc,"Writing EPUB",*bi,result->block_count);
        Block *b=&p->blocks[i];PD_Block *s=&b->pub;
        if(s->omitted) {
            s->anchor[0]=0;
            result->omitted_blocks++;result->omitted_glyphs+=s->glyphs;
            result->assigned_glyphs+=s->glyphs;
            result->blocks[(*bi)++]=*s;
            continue;
        }
        if(s->note || s->kind==PD_DETAIL) {
            result->assigned_glyphs+=s->glyphs;
            result->blocks[(*bi)++]=*s;
            continue;
        }
        if(s->kind==PD_MARGIN) {
            /* A confirmed printed folio is page metadata, not a final
             * paragraph that may occupy an otherwise empty EPUB page.
             * Navigation and the inspector retain its original label. */
            result->assigned_glyphs+=s->glyphs;
            result->blocks[(*bi)++]=*s;
            continue;
        }
        int sideways=turn_for_reader(w,s);s->page_rotation=sideways?90:0;
        int dedicated=s->kind==PD_PAGE_IMAGE || ((s->kind==PD_TABLE || s->kind==PD_FIGURE) &&
            s->box.y1-s->box.y0>w->doc->body*3 && s->box.x1-s->box.x0>w->doc->body*3 &&
            !strstr(s->reason,"Unmapped") && !strstr(s->reason,"Mathematical"));
        int note_links=0,native_detail=has_native_detail(w->doc,p,s->id);
        for(int ri=0;ri<w->doc->nref;ri++)note_links+=block_ref(w->doc,p,b,ri);
        if(out->html && (dedicated || out->object))finish_part(w,out);
        if(!out->html)start_part(w,p,out,dedicated,note_links || native_detail);
        fz_buffer *html=out->html;
        if(!p->exported) {
            snprintf(p->anchor,sizeof(p->anchor),"%s#page%d",out->relative,p->number+1);
            fz_append_printf(ctx,html,"<span epub:type=\"pagebreak\" role=\"doc-pagebreak\" id=\"page%d\" title=\"PDF page %d\"/>",p->number+1,p->number+1);
            p->exported=1;
        }
        snprintf(s->anchor,sizeof(s->anchor),"%s#b%d",out->relative,s->id);
        if(s->kind==PD_TABLE || s->kind==PD_FIGURE || s->kind==PD_EQUATION || s->kind==PD_PAGE_IMAGE) {
            float width=s->box.x1-s->box.x0;
            if(s->rotation==90 || s->rotation==270)width=s->box.y1-s->box.y0;
            float em=fmaxf(1,width/w->doc->body);
            Reconstruction *recon=s->native?&p->recon[s->native-1]:NULL;
            int native=w->doc->opt && w->doc->opt->reader && s->kind==PD_EQUATION && recon && recon->accepted && recon->markup && em<24;
            if(native) {
                fz_append_printf(ctx,html,"<div class=\"native-equation\" id=\"b%d\">",s->id);
                native_math(w,html,recon->markup);fz_append_string(ctx,html,"</div>");
                recon->visible=1;
            } else {
            char base[64],reader[64];int vector=write_asset(w,p,b,s->box,s->rotation,0,!native,base,sizeof(base));
            snprintf(reader,sizeof(reader),"%s",base);
            if(dedicated)vector&=write_asset(w,p,b,s->box,(s->rotation+(sideways?90:0))%360,1,1,reader,sizeof(reader));
            const char *extension=vector?"svg":"png";
            const char *preview_extension=w->doc->opt && w->doc->opt->reader && !native?"png":extension;
            snprintf(s->asset,sizeof(s->asset),"OEBPS/%s.%s",base,extension);
            snprintf(s->reader_asset,sizeof(s->reader_asset),"OEBPS/%s.%s",reader,preview_extension);
            if(!vector) {
                size_t used=strlen(s->reason);s->review=1;
                snprintf(s->reason+used,sizeof(s->reason)-used," PNG preview: vector uses masks, patterns or compositing.");
            }
            fz_append_printf(ctx,html,"<div class=\"object\" id=\"b%d\"><a href=\"%s.%s\"><img src=\"%s.%s\"",s->id,base,extension,reader,preview_extension);
            if(!dedicated)fz_append_printf(ctx,html," style=\"width:%.3fem\"",em);
            fz_append_string(ctx,html," alt=\"");
            pd_xml(ctx,html,s->label);
            fz_append_string(ctx,html,"\"/></a></div>");
            }
            if(native_detail)fz_append_printf(ctx,html,"<p class=\"object-note-links\"><a href=\"native-%d.xhtml#content%d\">Read recognized content</a></p>",s->id,s->id);
            if(note_links) {
                fz_append_string(ctx,html,"<p class=\"object-note-links\">");
                for(int ri=0;ri<w->doc->nref;ri++)if(block_ref(w->doc,p,b,ri)) {
                    NoteRef *ref=&w->doc->refs[ri];Line *l=&p->lines[ref->line];
                    snprintf(ref->anchor,sizeof(ref->anchor),"%s#ref%d",out->relative,ri+1);
                    fz_append_printf(ctx,html,"<a epub:type=\"noteref\" role=\"doc-noteref\" id=\"ref%d\" href=\"notes.xhtml#fn%d\">Note ",ri+1,ref->note);
                    for(int k=ref->first;k<ref->end;k++)xml_char(ctx,html,l->glyph[k].c);
                    fz_append_string(ctx,html,"</a> ");
                }
                fz_append_string(ctx,html,"</p>");
            }
            if(s->kind==PD_PAGE_IMAGE) {
                result->image_pages++;
            }
            if(s->kind==PD_TABLE)result->tables++;
            if(s->kind==PD_FIGURE)result->figures++;
            if(s->kind==PD_EQUATION)result->equations++;
        } else if(s->kind==PD_RULE) {
            float width=100*(s->box.x1-s->box.x0)/(p->box.x1-p->box.x0);
            fz_append_printf(ctx,html,"<hr id=\"b%d\" style=\"width:%.2f%%;margin:.5em 0;border:0;border-top:1px solid\"/>",s->id,width);
        } else {
            char tag[12]="p";
            if(s->kind==PD_HEADING)snprintf(tag,sizeof(tag),"h%d",s->level?s->level:1);
            if(s->continues && out->paragraph) {
                if(out->last_char!='-' && out->last_char!=0xad)fz_append_byte(ctx,html,' ');
                fz_append_printf(ctx,html,"<span id=\"b%d\"/>",s->id);
            } else {
                const char *cls=s->front==1?"book-title":s->front==2?"author":
                    s->kind==PD_NOTE?"note":s->kind==PD_HEADING?"heading":
                    s->section&PD_REFERENCES?"reference":p->lines[b->lines[0]].caption?"caption":"text";
                fz_append_printf(ctx,html,"<%s id=\"b%d\" class=\"%s%s\">",tag,s->id,cls,
                                 b->indent>0?" indent":b->indent<0?" hanging":"");
            }
            for(int j=0;j<b->n;j++) {
                if(j) {
                    Line *last=&p->lines[b->lines[j-1]];
                    if(!last->n || (last->glyph[last->n-1].c!='-' && last->glyph[last->n-1].c!=0xad))
                        fz_append_byte(ctx,html,' ');
                }
                text_line(w,p,&p->lines[b->lines[j]],html,0);
            }
            Line *last=&p->lines[b->lines[b->n-1]];
            out->last_char=last->n?last->glyph[last->n-1].c:0;
            out->paragraph=b->join_next;
            if(!b->join_next)fz_append_printf(ctx,html,"</%s>",tag);
            result->text_blocks++;
        }
        if(s->review)result->review_count++;
        result->assigned_glyphs+=s->glyphs;
        result->blocks[(*bi)++]=*s;
    }
    for(int i=begin;i<*bi;i++)if(result->blocks[i].kind==PD_MARGIN && !result->blocks[i].omitted)
        snprintf(result->blocks[i].anchor,sizeof(result->blocks[i].anchor),"%s",p->anchor);
    result->pages+=p->exported;
}
static int same_detail(Page *p,Block *a,Page *q,Block *b) {
    if(a->n!=b->n)return 0;
    for(int i=0;i<a->n;i++)if(strcmp(p->lines[a->lines[i]].text,q->lines[b->lines[i]].text))return 0;
    return 1;
}
static void write_details(Writer *w,PD_Result *r) {
    Document *d=w->doc;fz_context *ctx=d->ctx;fz_buffer *html=NULL;fz_var(html);
    fz_try(ctx) {
        for(int pi=0;pi<d->npage;pi++)for(int bi=0;bi<d->pages[pi].nblock;bi++) {
            pd_check(d,"Writing publication details",pi,d->npage);
            Page *p=&d->pages[pi];Block *b=&p->blocks[bi];PD_Block *s=&b->pub;
            if(s->kind!=PD_DETAIL || s->omitted || !strncmp(s->anchor,"publication.xhtml#",18))continue;
            if(!html) {
                html=fz_new_buffer(ctx,1024);snprintf(w->current,sizeof(w->current),"publication.xhtml");
                fz_append_string(ctx,html,"<?xml version=\"1.0\" encoding=\"utf-8\"?><html xmlns=\"http://www.w3.org/1999/xhtml\" lang=\"en\"><head><title>Publication details</title><link rel=\"stylesheet\" type=\"text/css\" href=\"style.css\"/></head><body class=\"publication\"><h1 id=\"publication\">Publication details</h1>");
            }
            fz_append_printf(ctx,html,"<p id=\"b%d\">",s->id);
            for(int i=0;i<b->n;i++) {
                if(i)fz_append_byte(ctx,html,' ');
                text_line(w,p,&p->lines[b->lines[i]],html,0);
            }
            fz_append_string(ctx,html,"</p><p class=\"source\">PDF pages: ");int count=0;
            for(int qi=pi;qi<d->npage;qi++)for(int j=0;j<d->pages[qi].nblock;j++) {
                Page *q=&d->pages[qi];Block *copy=&q->blocks[j];PD_Block *t=&copy->pub;
                if(t->kind!=PD_DETAIL || t->omitted || !same_detail(p,b,q,copy))continue;
                snprintf(t->anchor,sizeof(t->anchor),"publication.xhtml#b%d",s->id);
                if(!q->exported) {snprintf(q->anchor,sizeof(q->anchor),"%s",t->anchor);q->exported=1;r->pages++;}
                if(count++)fz_append_string(ctx,html,", ");
                fz_append_printf(ctx,html,"<a href=\"%s\">%d</a>",q->anchor,q->number+1);
                r->blocks[t->id-1]=*t;
            }
            fz_append_string(ctx,html,"</p>");w->details++;
        }
        if(html) {
            fz_append_string(ctx,html,"</body></html>");zip_buffer(w,"OEBPS/publication.xhtml",html,1);
            manifest(w,"publication","publication.xhtml","application/xhtml+xml",strstr(fz_string_from_buffer(ctx,html),"<math ")?" properties=\"mathml\"":NULL);
            fz_append_string(ctx,w->spine,"<itemref idref=\"publication\"/>");
        }
    }
    fz_always(ctx)fz_drop_buffer(ctx,html);
    fz_catch(ctx)fz_rethrow(ctx);
}
static void write_notes(Writer *w,PD_Result *r) {
    Document *d=w->doc;fz_context *ctx=d->ctx;
    fz_buffer *html=NULL;fz_var(html);
    fz_try(ctx) {
        for(int ri=0;ri<d->nref;ri++) {
            pd_check(d,"Writing notes",ri,d->nref);
            NoteRef *ref=&d->refs[ri];if(ref->omitted)continue;
            int seen=0;for(int k=0;k<ri;k++)if(!d->refs[k].omitted && d->refs[k].note==ref->note)seen=1;
            if(seen)continue;
            if(!html) {
                html=fz_new_buffer(ctx,2048);snprintf(w->current,sizeof(w->current),"notes.xhtml");
                fz_append_string(ctx,html,"<?xml version=\"1.0\" encoding=\"utf-8\"?><html xmlns=\"http://www.w3.org/1999/xhtml\" xmlns:epub=\"http://www.idpf.org/2007/ops\" lang=\"en\"><head><title>Notes</title><link rel=\"stylesheet\" type=\"text/css\" href=\"style.css\"/></head><body><h1 id=\"notes\">Notes</h1>");
            }
            fz_append_printf(ctx,html,"<aside class=\"footnote\" epub:type=\"footnote\" role=\"doc-footnote\" id=\"fn%d\">",ref->note);
            int paragraph=0,last_block=0,last_char=0;
            for(int pi=0;pi<d->npage;pi++) {
                Page *p=&d->pages[pi];
                for(int bi=0;bi<p->nblock;bi++) {
                    Block *b=&p->blocks[bi];PD_Block *s=&b->pub;
                    if(s->note!=ref->note || s->omitted)continue;
                    if(!p->exported) {
                        snprintf(p->anchor,sizeof(p->anchor),"notes.xhtml#page%d",p->number+1);
                        fz_append_printf(ctx,html,"<span epub:type=\"pagebreak\" role=\"doc-pagebreak\" id=\"page%d\" title=\"PDF page %d\"/>",p->number+1,p->number+1);
                        p->exported=1;r->pages++;
                    }
                    snprintf(s->anchor,sizeof(s->anchor),"notes.xhtml#b%d",s->id);
                    if(paragraph && (s->kind==PD_RULE || s->kind==PD_EQUATION || s->continues!=last_block)) {
                        fz_append_string(ctx,html,"</p>");paragraph=0;
                    }
                    if(s->kind==PD_RULE) {
                        fz_append_printf(ctx,html,"<hr id=\"b%d\" style=\"width:%.2f%%;margin:.5em 0;border:0;border-top:1px solid\"/>",s->id,100*(s->box.x1-s->box.x0)/(p->box.x1-p->box.x0));
                    } else {
                        if(s->kind==PD_EQUATION) {
                            char base[64];int vector=write_asset(w,p,b,s->box,s->rotation,0,1,base,sizeof(base));
                            const char *ext=vector?"svg":"png";
                            snprintf(s->asset,sizeof(s->asset),"OEBPS/%s.%s",base,ext);
                            snprintf(s->reader_asset,sizeof(s->reader_asset),"%s",s->asset);
                            fz_append_printf(ctx,html,"<div class=\"object\" id=\"b%d\"><img src=\"%s.%s\" style=\"width:%.3fem\" alt=\"",s->id,base,ext,(s->box.x1-s->box.x0)/d->body);
                            pd_xml(ctx,html,s->label);fz_append_string(ctx,html,"\"/></div>");r->equations++;
                        } else {
                            if(paragraph) {
                                if(last_char!='-' && last_char!=0xad)fz_append_byte(ctx,html,' ');
                                fz_append_printf(ctx,html,"<span id=\"b%d\"/>",s->id);
                            } else fz_append_printf(ctx,html,"<p id=\"b%d\">",s->id);
                            for(int li=0;li<b->n;li++) {
                                Line *l=&p->lines[b->lines[li]];int start=li?0:b->note_prefix;
                                if(start) {
                                    fz_append_string(ctx,html,"<span class=\"note-label\">");
                                    for(int g=0;g<start;g++)xml_char(ctx,html,l->glyph[g].c);
                                    fz_append_string(ctx,html,"</span> ");
                                }
                                if(li) {
                                    Line *last=&p->lines[b->lines[li-1]];
                                    if(!last->n || (last->glyph[last->n-1].c!='-' && last->glyph[last->n-1].c!=0xad))fz_append_byte(ctx,html,' ');
                                }
                                text_line(w,p,l,html,start);
                            }
                            Line *last=&p->lines[b->lines[b->n-1]];
                            last_char=last->n?last->glyph[last->n-1].c:0;paragraph=1;last_block=s->id;r->text_blocks++;
                        }
                    }
                    r->review_count+=!!s->review;r->blocks[s->id-1]=*s;
                }
            }
            if(paragraph)fz_append_string(ctx,html,"</p>");
            fz_append_string(ctx,html,"<p class=\"note-return\">");int count=0;
            for(int j=0;j<d->nref;j++) {
                NoteRef *back=&d->refs[j];if(back->omitted || back->note!=ref->note)continue;
                if(count++)fz_append_string(ctx,html," · ");
                fz_append_printf(ctx,html,"<a role=\"doc-backlink\" href=\"%s\">Back to text",back->anchor);
                if(count>1)fz_append_printf(ctx,html," %d",count);
                fz_append_string(ctx,html,"</a>");
            }
            fz_append_string(ctx,html,"</p></aside>");r->footnotes++;
        }
        if(html) {
            fz_append_string(ctx,html,"</body></html>");zip_buffer(w,"OEBPS/notes.xhtml",html,1);
            manifest(w,"notes","notes.xhtml","application/xhtml+xml",strstr(fz_string_from_buffer(ctx,html),"<math ")?" properties=\"mathml\"":NULL);
            fz_append_string(ctx,w->spine,"<itemref idref=\"notes\"/>");
        }
    }
    fz_always(ctx)fz_drop_buffer(ctx,html);
    fz_catch(ctx)fz_rethrow(ctx);
}
static int has_native_detail(Document *d,Page *p,int block) {
    if(!d->opt || !d->opt->reader)return 0;
    for(int i=0;i<p->nrecon;i++)if(p->recon[i].block==block && p->recon[i].accepted &&
        (p->recon[i].task==1 || p->recon[i].task==3))return 1;
    return 0;
}
static int cell_characters(fz_xml *x) {
    int n=0;const char *s=fz_xml_text(x);
    if(s)while(*s){int c;s+=fz_chartorune(&c,s);n++;}
    for(fz_xml *child=fz_xml_down(x);child;child=fz_xml_next(child))n+=cell_characters(child);
    return n;
}
static float table_detail_size(fz_context *ctx,const char *markup) {
    /* Fit the grid's complete strings, including signs and significance
     * markers, to the smallest supported preview width. This linked detail
     * is an object view; body text continues to follow the reader font size. */
    fz_buffer *data=fz_new_buffer_from_copied_data(ctx,(const unsigned char*)markup,strlen(markup));
    fz_xml *xml=NULL;float size=12;fz_var(xml);fz_var(size);
    fz_try(ctx) {
        xml=fz_parse_xml(ctx,data,1);float widths[512]={0};int occupied[512]={0},columns=0;
        fz_xml *body=fz_xml_down(fz_xml_root(xml));
        for(fz_xml *row=fz_xml_down(body);row;row=fz_xml_next(row)) {
            int column=0;
            for(fz_xml *cell=fz_xml_down(row);cell;cell=fz_xml_next(cell)) {
                if(!fz_xml_tag(cell))continue;
                while(column<512 && occupied[column])column++;
                const char *s=fz_xml_att(cell,"colspan"),*r=fz_xml_att(cell,"rowspan");
                int span=s?atoi(s):1,rows=r?atoi(r):1;if(span<1 || column+span>512)break;
                float width=(cell_characters(cell)*.68f+1.4f)/span;
                for(int k=0;k<span;k++){widths[column+k]=fmaxf(widths[column+k],width);occupied[column+k]=rows;}
                column+=span;if(column>columns)columns=column;
            }
            for(int i=0;i<512;i++)if(occupied[i])occupied[i]--;
        }
        float width=0;for(int i=0;i<columns;i++)width+=widths[i];
        if(width>0)size=fminf(size,312/width);
    }
    fz_always(ctx){fz_drop_xml(ctx,xml);fz_drop_buffer(ctx,data);}
    fz_catch(ctx)fz_rethrow(ctx);
    return size;
}
static void table_detail_markup(fz_context *ctx,fz_buffer *out,const char *markup) {
    const char *s=markup;
    while(*s) {
        if(!strncmp(s,"<td ",4) || !strncmp(s,"<th ",4)) {
            const char *end=strchr(s,'>');
            fz_append_data(ctx,out,s,(size_t)(end-s+1));fz_append_string(ctx,out,"<span class=\"cell-content\">");s=end+1;
        } else {
            if(!strncmp(s,"</td>",5) || !strncmp(s,"</th>",5))fz_append_string(ctx,out,"</span>");
            fz_append_byte(ctx,out,*s++);
        }
    }
}
static void write_native_details(Writer *w) {
    Document *d=w->doc;fz_context *ctx=d->ctx;
    if(!d->opt || !d->opt->reader)return;
    int pi;fz_var(pi);
    for(pi=0;pi<d->npage;pi++) {
        Page *p=&d->pages[pi];
        for(int bi=0;bi<p->nblock;bi++) {
            PD_Block *b=&p->blocks[bi].pub;if(b->omitted || !has_native_detail(d,p,b->id))continue;
            fz_buffer *html=fz_new_buffer(ctx,2048);
            fz_try(ctx) {
                fz_append_string(ctx,html,"<?xml version=\"1.0\" encoding=\"utf-8\"?><html xmlns=\"http://www.w3.org/1999/xhtml\" lang=\"en\"><head><title>Recognized content</title><link rel=\"stylesheet\" type=\"text/css\" href=\"style.css\"/></head><body class=\"native-detail\">");
                fz_append_printf(ctx,html,"<p id=\"content%d\"><a href=\"%s\">Back to original</a></p>",b->id,b->anchor);
                for(int ri=0;ri<p->nrecon;ri++) {
                    Reconstruction *r=&p->recon[ri];
                    if(r->block!=b->id || !r->accepted || (r->task!=1 && r->task!=3))continue;
                    if(r->task==3)fz_append_printf(ctx,html,"<div style=\"font-size:%.3fpx\">",table_detail_size(ctx,r->markup));
                    if(r->task==3)table_detail_markup(ctx,html,r->markup);else fz_append_string(ctx,html,r->markup);
                    r->visible=2;
                    if(r->task==3)fz_append_string(ctx,html,"</div>");
                }
                fz_append_string(ctx,html,"</body></html>");
                char relative[96],entry[128],id[64];snprintf(relative,sizeof(relative),"native-%d.xhtml",b->id);
                snprintf(entry,sizeof(entry),"OEBPS/%s",relative);snprintf(id,sizeof(id),"native-%d",b->id);
                zip_buffer(w,entry,html,1);manifest(w,id,relative,"application/xhtml+xml",strstr(fz_string_from_buffer(ctx,html),"<math ")?" properties=\"mathml\"":NULL);
                fz_append_printf(ctx,w->spine,"<itemref idref=\"%s\" linear=\"no\"/>",id);
            }
            fz_always(ctx)fz_drop_buffer(ctx,html);
            fz_catch(ctx)fz_rethrow(ctx);
        }
    }
}
static void native_font(Writer *w) {
    fz_context *ctx=w->doc->ctx;char path[4096];ssize_t n=readlink("/proc/self/exe",path,sizeof(path)-1);
    if(n<1)fz_throw(ctx,FZ_ERROR_SYSTEM,"Cannot locate bundled math font");
    path[n]=0;char *end=strrchr(path,'/');if(!end)fz_throw(ctx,FZ_ERROR_SYSTEM,"Cannot locate reader assets");*end=0;
    char base[4096];snprintf(base,sizeof(base),"%s",path);
    const char *names[]={"STIXTwoMath-Regular.ttf","STIXTwoMath-OFL.txt"};
    for(int i=0;i<2;i++) {
        snprintf(path,sizeof(path),"%.3800s/assets/%s",base,names[i]);
        if(access(path,R_OK))snprintf(path,sizeof(path),"%.3800s/../assets/%s",base,names[i]);
        fz_buffer *data=fz_read_file(ctx,path);
        fz_try(ctx){char entry[160];snprintf(entry,sizeof(entry),"OEBPS/%s",names[i]);zip_buffer(w,entry,data,1);}
        fz_always(ctx)fz_drop_buffer(ctx,data);
        fz_catch(ctx)fz_rethrow(ctx);
        manifest(w,i?"math-font-license":"math-font",names[i],i?"text/plain":"font/ttf",NULL);
    }
}
void pd_write_epub(Document *d,const char *out,PD_Result *r) {
    fz_context *ctx=d->ctx;Writer w={0};w.doc=d;fz_var(w);fz_buffer *opf=NULL;fz_var(opf);
    PageOutput flow={0};fz_var(flow);
    fz_try(ctx) {
        w.zip=pd_zip_open(ctx,out);
        w.manifest=fz_new_buffer(ctx,4096);w.spine=fz_new_buffer(ctx,512);
        w.nav=fz_new_buffer(ctx,1024);w.map=fz_new_buffer(ctx,4096);
        zip_string(&w,"mimetype","application/epub+zip",0);
        zip_string(&w,"META-INF/container.xml","<?xml version=\"1.0\"?><container version=\"1.0\" xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\"><rootfiles><rootfile full-path=\"OEBPS/package.opf\" media-type=\"application/oebps-package+xml\"/></rootfiles></container>",1);
        fz_buffer *stylesheet=fz_new_buffer(ctx,4096);
        fz_try(ctx) {
            if(d->opt && d->opt->reader)fz_append_string(ctx,stylesheet,"@font-face{font-family:'STIX Two Math';src:url('STIXTwoMath-Regular.ttf')}");
            fz_append_string(ctx,stylesheet,css);
            if(d->opt && d->opt->reader)fz_append_string(ctx,stylesheet,
                "math .math-flow{display:inline;white-space:normal}math .math-term{display:inline-block;white-space:nowrap}"
                ".native-equation math{text-align:left;hyphens:none}.native-equation{page-break-inside:avoid}");
            if(d->opt && d->opt->reader==PD_READER_KOBO)fz_append_string(ctx,stylesheet,
                "html.object-document #book-columns,html.object-document #book-inner{height:100%}"
                "span.math-flow{white-space:normal;line-height:1.8;text-indent:0}"
                "span.math-term{display:inline-block;white-space:nowrap;text-indent:0;vertical-align:baseline}"
                "math,math *{font-family:'STIX Two Math',serif!important;hyphens:none;-webkit-hyphens:none}"
                ".native-equation{text-align:left}");
            zip_buffer(&w,"OEBPS/style.css",stylesheet,1);
        }
        fz_always(ctx)fz_drop_buffer(ctx,stylesheet);
        fz_catch(ctx)fz_rethrow(ctx);
        if(d->opt && d->opt->reader)native_font(&w);
        manifest(&w,"style","style.css","text/css",NULL);
        fz_buffer *cover=pd_cover_png(d);
        fz_try(ctx)zip_buffer(&w,"OEBPS/cover.png",cover,0);
        fz_always(ctx)fz_drop_buffer(ctx,cover);
        fz_catch(ctx)fz_rethrow(ctx);
        manifest(&w,"cover-image","cover.png","image/png"," properties=\"cover-image\"");
        fz_buffer *cover_page=fz_new_buffer(ctx,1024);
        fz_try(ctx) {
            fz_append_string(ctx,cover_page,"<?xml version=\"1.0\" encoding=\"utf-8\"?><html xmlns=\"http://www.w3.org/1999/xhtml\" xmlns:epub=\"http://www.idpf.org/2007/ops\" lang=\"en\"><head><title>Cover</title><style>html,body{margin:0;padding:0;height:100%;background:white}body{line-height:0}img{display:block;width:100%;height:auto;max-width:100%;max-height:100%;object-fit:contain}</style></head><body epub:type=\"cover\" id=\"cover\"><img src=\"cover.png\" alt=\"");
            pd_xml(ctx,cover_page,d->title);fz_append_string(ctx,cover_page,"\"/></body></html>");
            zip_buffer(&w,"OEBPS/cover.xhtml",cover_page,1);
        }
        fz_always(ctx)fz_drop_buffer(ctx,cover_page);
        fz_catch(ctx)fz_rethrow(ctx);
        manifest(&w,"cover","cover.xhtml","application/xhtml+xml",NULL);
        fz_append_string(ctx,w.spine,"<itemref idref=\"cover\"/>");
        fz_append_string(ctx,w.nav,"<?xml version=\"1.0\" encoding=\"utf-8\"?><html xmlns=\"http://www.w3.org/1999/xhtml\" xmlns:epub=\"http://www.idpf.org/2007/ops\" lang=\"en\"><head><title>Contents</title></head><body><nav epub:type=\"toc\" id=\"toc\"><h1>Contents</h1><ol>");
        fz_append_printf(ctx,w.map,"{\"schema\":2,\"generator\":\"Paperdesk " PD_VERSION "\",\"method\":\"%s\",\"title\":",d->model_stats.applied?"geometry+local-model":"geometry");
        pd_json(ctx,w.map,d->title);
        fz_append_printf(ctx,w.map,",\"source_pages\":%d,\"include_references\":%s,\"include_appendices\":%s,\"blocks\":[",d->physical_pages,
            d->opt && d->opt->exclude_references?"false":"true",d->opt && d->opt->exclude_appendices?"false":"true");
        for(int i=0;i<d->npage;i++) {
            r->block_count+=d->pages[i].nblock;
            for(int j=0;j<d->pages[i].nline;j++)r->glyphs+=d->pages[i].lines[j].n;
        }
        r->blocks=calloc((size_t)r->block_count,sizeof(PD_Block));
        if(!r->blocks && r->block_count)fz_throw(ctx,FZ_ERROR_SYSTEM,"Not enough memory for the source map");
        int bi=0;
        for(int i=0;i<d->npage;i++)write_page(&w,&d->pages[i],&flow,r,&bi);
        finish_part(&w,&flow);
        write_notes(&w,r);
        write_details(&w,r);
        write_native_details(&w);
        for(int i=0;i<r->block_count;i++)if(r->blocks[i].kind==PD_MARGIN && !r->blocks[i].omitted)
            for(int j=0;j<d->npage;j++)if(d->pages[j].number==r->blocks[i].page)
                snprintf(r->blocks[i].anchor,sizeof(r->blocks[i].anchor),"%s",d->pages[j].anchor);
        for(int i=0;i<r->block_count;i++)map_block(&w,&r->blocks[i],i>0);
        if(!r->pages)fz_throw(ctx,FZ_ERROR_ARGUMENT,"No content remains after excluding the selected sections");
        int depth=0;
        for(int i=0;i<r->block_count;i++) {
            PD_Block *s=&r->blocks[i];if(s->omitted || s->kind!=PD_HEADING || !s->level)continue;
            int target=s->level;if(target>depth+1)target=depth+1;
            if(!depth){depth=1;fz_append_string(ctx,w.nav,"<li>");}
            else if(target>depth){depth++;fz_append_string(ctx,w.nav,"<ol><li>");}
            else {
                fz_append_string(ctx,w.nav,"</li>");
                while(depth>target){fz_append_string(ctx,w.nav,"</ol></li>");depth--;}
                fz_append_string(ctx,w.nav,"<li>");
            }
            fz_append_printf(ctx,w.nav,"<a href=\"%s\">",s->anchor);
            pd_xml(ctx,w.nav,s->label);fz_append_string(ctx,w.nav,"</a>");
        }
        if(depth){fz_append_string(ctx,w.nav,"</li>");while(depth>1){fz_append_string(ctx,w.nav,"</ol></li>");depth--;}}
        else for(int i=0;i<d->npage;i++) {
            if(!d->pages[i].exported)continue;
            int n=d->pages[i].number+1;
            fz_append_printf(ctx,w.nav,"<li><a href=\"%s\">Page %d</a></li>",d->pages[i].anchor,n);
        }
        if(r->footnotes)fz_append_string(ctx,w.nav,"<li><a href=\"notes.xhtml#notes\">Notes</a></li>");
        if(w.details)fz_append_string(ctx,w.nav,"<li><a href=\"publication.xhtml#publication\">Publication details</a></li>");
        fz_append_string(ctx,w.nav,"</ol></nav><nav epub:type=\"page-list\"><h2>PDF pages</h2><ol>");
        for(int i=0;i<d->npage;i++) {
            if(!d->pages[i].exported)continue;
            int n=d->pages[i].number+1;
            fz_append_printf(ctx,w.nav,"<li><a href=\"%s\">%d",d->pages[i].anchor,n);
            for(int j=0;j<d->pages[i].nblock;j++) {
                PD_Block *b=&d->pages[i].blocks[j].pub;
                if(b->kind!=PD_MARGIN || b->omitted)continue;
                fz_append_string(ctx,w.nav," (printed ");pd_xml(ctx,w.nav,b->label);fz_append_byte(ctx,w.nav,')');
            }
            fz_append_string(ctx,w.nav,"</a></li>");
        }
        fz_append_string(ctx,w.nav,"</ol></nav></body></html>");
        fz_append_printf(ctx,w.map,"],\"glyphs\":%d,\"assigned_glyphs\":%d,\"exported_glyphs\":%d,\"omitted_glyphs\":%d,\"omitted_blocks\":%d,\"exported_pages\":%d,\"review_blocks\":%d,\"image_pages\":%d,\"linked_notes\":%d}",
                         r->glyphs,r->assigned_glyphs,r->assigned_glyphs-r->omitted_glyphs,r->omitted_glyphs,r->omitted_blocks,r->pages,r->review_count,r->image_pages,r->footnotes);
        zip_buffer(&w,"OEBPS/nav.xhtml",w.nav,1);
        manifest(&w,"nav","nav.xhtml","application/xhtml+xml"," properties=\"nav\"");
        zip_buffer(&w,"OEBPS/source-map.json",w.map,1);
        manifest(&w,"map","source-map.json","application/json",NULL);
        if(d->model_stats.requested) {
            fz_buffer *report=pd_model_report(d);
            fz_try(ctx) {zip_buffer(&w,"OEBPS/model-report.json",report,1);manifest(&w,"model-report","model-report.json","application/json",NULL);}
            fz_always(ctx)fz_drop_buffer(ctx,report);
            fz_catch(ctx)fz_rethrow(ctx);
        }
        {
            fz_buffer *reconstruction=pd_reconstruction_report(d);
            zip_buffer(&w,"OEBPS/reconstruction.json",reconstruction,1);fz_drop_buffer(ctx,reconstruction);
            manifest(&w,"reconstruction","reconstruction.json","application/json",NULL);
            size_t count=0;
            for(int pi=0;pi<d->npage;pi++)count+=(size_t)d->pages[pi].nrecon;
            if(count>INT_MAX)fz_throw(ctx,FZ_ERROR_LIMIT,"Too many recognition results");
            r->recognitions=calloc(count,sizeof(PD_Recognition));
            if(count && !r->recognitions)fz_throw(ctx,FZ_ERROR_SYSTEM,"Not enough memory for recognition report");
            for(int pi=0;pi<d->npage;pi++)for(int i=0;i<d->pages[pi].nrecon;i++) {
                Reconstruction *item=&d->pages[pi].recon[i];
                if(item->task==2){r->detected_math++;r->native_math+=item->accepted;r->visible_math+=item->visible==1;}
                if(item->task==3){r->recognized_tables++;r->native_tables+=item->accepted;r->visible_tables+=item->visible==2;}
                if(item->task==1)r->recognized_text++;
                r->unresolved+=!item->accepted && !item->skipped;r->reconstruction_skips+=item->skipped;
                r->cache_hits+=item->cached;r->manual_count+=item->manual;
                PD_Recognition *v=&r->recognitions[r->recognition_count++];
                v->page=d->pages[pi].number+1;v->index=i+1;v->block=item->block;v->task=item->task;v->box=item->box;
                v->accepted=item->accepted;v->visible=item->visible;v->manual=item->manual;v->skipped=item->skipped;
                v->raw=strdup(item->raw?item->raw:"");v->markup=strdup(item->markup?item->markup:"");
                if(!v->raw || !v->markup)fz_throw(ctx,FZ_ERROR_SYSTEM,"Not enough memory for recognition content");
                snprintf(v->reason,sizeof(v->reason),"%s",item->reason);
            }
        }
        opf=fz_new_buffer(ctx,4096);
        time_t now=time(NULL);struct tm utc;gmtime_r(&now,&utc);char date[32];strftime(date,sizeof(date),"%Y-%m-%dT%H:%M:%SZ",&utc);
        fz_append_string(ctx,opf,"<?xml version=\"1.0\" encoding=\"utf-8\"?><package xmlns=\"http://www.idpf.org/2007/opf\" version=\"3.0\" unique-identifier=\"book-id\"><metadata xmlns:dc=\"http://purl.org/dc/elements/1.1/\"><dc:identifier id=\"book-id\">urn:paperdesk:");
        unsigned char digest[16];fz_md5 md5;fz_md5_init(&md5);
        fz_md5_update(&md5,(const unsigned char*)fz_string_from_buffer(ctx,w.map),fz_buffer_storage(ctx,w.map,NULL));
        fz_md5_final(&md5,digest);
        for(int i=0;i<16;i++)fz_append_printf(ctx,opf,"%02x",digest[i]);
        fz_append_string(ctx,opf,"</dc:identifier>");
        if(d->opt && d->opt->reader)fz_append_printf(ctx,opf,"<meta name=\"paperdesk-reader\" content=\"%s\"/>",d->opt->reader==PD_READER_KOBO?"kobo":"koreader");
        fz_append_string(ctx,opf,"<dc:title>");pd_xml(ctx,opf,d->title);
        fz_append_string(ctx,opf,"</dc:title>");
        if(d->author[0]){fz_append_string(ctx,opf,"<dc:creator>");pd_xml(ctx,opf,d->author);fz_append_string(ctx,opf,"</dc:creator>");}
        fz_append_printf(ctx,opf,"<dc:language>en</dc:language><meta name=\"cover\" content=\"cover-image\"/><meta property=\"dcterms:modified\">%s</meta><meta name=\"generator\" content=\"Paperdesk " PD_VERSION "\"/></metadata><manifest>",date);
        fz_append_buffer(ctx,opf,w.manifest);fz_append_string(ctx,opf,"</manifest><spine>");
        fz_append_buffer(ctx,opf,w.spine);fz_append_string(ctx,opf,"</spine><guide><reference type=\"cover\" title=\"Cover\" href=\"cover.xhtml\"/></guide></package>");
        zip_buffer(&w,"OEBPS/package.opf",opf,1);
        pd_zip_finish(ctx,w.zip);
    }
    fz_always(ctx) {
        pd_zip_drop(ctx,w.zip);
        fz_drop_display_list(ctx,w.artwork);
        fz_drop_buffer(ctx,flow.html);
        fz_drop_buffer(ctx,w.manifest);fz_drop_buffer(ctx,w.spine);
        fz_drop_buffer(ctx,w.nav);fz_drop_buffer(ctx,w.map);fz_drop_buffer(ctx,opf);
    }
    fz_catch(ctx) fz_rethrow(ctx);
}
int pd_convert(const char *input,const char *output,const PD_Options *opt,PD_Result **result,char *err,size_t len) {
    if(result)*result=NULL;
    struct stat a,b;
    if(!stat(input,&a) && !stat(output,&b) && a.st_ino==b.st_ino && a.st_dev==b.st_dev) {
        snprintf(err,len,"The output must not replace the source PDF");return 0;
    }
    if(strlen(input)>=4096 || strlen(output)>=4000) {snprintf(err,len,"Path is too long");return 0;}
    Document d={0};d.opt=opt;fz_var(d);
    d.ctx=fz_new_context(NULL,NULL,96<<20);
    PD_Result *r=calloc(1,sizeof(*r));
    if(!d.ctx || !r) {fz_drop_context(d.ctx);free(r);snprintf(err,len,"Not enough memory");return 0;}
    r->reader=opt?opt->reader:PD_READER_COMPATIBLE;
    char temporary[4096];snprintf(temporary,sizeof(temporary),"%s.part-XXXXXX",output);
    int fd=mkstemp(temporary);
    if(fd<0){snprintf(err,len,"Cannot create output: %s",strerror(errno));fz_drop_context(d.ctx);free(r);return 0;}
    close(fd);
    int ok=0;fz_var(ok);
    struct timespec start,end;clock_gettime(CLOCK_MONOTONIC,&start);
    fz_register_document_handlers(d.ctx);
    fz_try(d.ctx) {
        pd_source_digest(&d,input);snprintf(r->source_digest,sizeof(r->source_digest),"%s",d.source_digest);
        if(opt && opt->correction_count && (!opt->correction_digest || strcmp(opt->correction_digest,d.source_digest)))
            fz_throw(d.ctx,FZ_ERROR_ARGUMENT,"The PDF changed; inspect it again before applying corrections");
        d.pdf=fz_open_document(d.ctx,input);
        if(fz_needs_password(d.ctx,d.pdf))fz_throw(d.ctx,FZ_ERROR_ARGUMENT,"This PDF requires a password");
        d.physical_pages=fz_count_pages(d.ctx,d.pdf);
        if(d.physical_pages<1)fz_throw(d.ctx,FZ_ERROR_ARGUMENT,"The PDF has no pages");
        fz_lookup_metadata(d.ctx,d.pdf,FZ_META_INFO_TITLE,d.title,sizeof(d.title));
        fz_lookup_metadata(d.ctx,d.pdf,FZ_META_INFO_AUTHOR,d.author,sizeof(d.author));
        pd_extract(&d);
        if(opt && opt->check_input) {
            PD_InputInfo info={0};info.pages=d.npage;
            for(int i=0;i<d.npage;i++) {
                Page *p=&d.pages[i];int glyphs=pd_page_glyphs(p);
                info.glyphs+=glyphs;info.text_pages+=glyphs>=PD_MIN_PAGE_GLYPHS;
                for(int j=0;j<p->nmark;j++)if(p->marks[j].image){info.image_pages++;break;}
            }
            pd_check(&d,"Checking PDF text",d.npage,d.npage);
            if(opt->check_input(opt->user,&info))
                fz_throw(d.ctx,FZ_ERROR_ABORT,"Conversion stopped: no usable PDF text");
        }
        pd_book_metadata(&d);
        if(d.pages[0].number!=0) {
            PD_BookInfo info;char metadata_error[200];
            if(pd_book_info(input,&info,metadata_error,sizeof(metadata_error))) {
                snprintf(d.title,sizeof(d.title),"%s",info.title);snprintf(d.author,sizeof(d.author),"%s",info.author);
                snprintf(d.reference,sizeof(d.reference),"%s",info.reference);
            }
        }
        if(!d.title[0]) {
            const char *name=strrchr(input,'/');snprintf(d.title,sizeof(d.title),"%s",name?name+1:input);
            char *dot=strrchr(d.title,'.');if(dot)*dot=0;
        }
        if(opt && opt->book_title && opt->book_title[0])pd_metadata_text(d.title,sizeof(d.title),opt->book_title);
        if(opt && opt->book_author)pd_metadata_text(d.author,sizeof(d.author),opt->book_author);
        pd_layout(&d);pd_reconstruct(&d);pd_write_epub(&d,temporary,r);
        r->model=d.model_stats;
        pd_check(&d,"Finishing",r->block_count,r->block_count);
        /* Publish only after the entire archive was closed successfully. */
        int sync_fd=open(temporary,O_RDONLY);
        if(sync_fd>=0){int e=fsync(sync_fd);close(sync_fd);if(e)fz_throw(d.ctx,FZ_ERROR_SYSTEM,"Cannot flush the EPUB");}
        if(rename(temporary,output))fz_throw(d.ctx,FZ_ERROR_SYSTEM,"Cannot save EPUB: %s",strerror(errno));
        snprintf(r->title,sizeof(r->title),"%s",d.title);
        snprintf(r->author,sizeof(r->author),"%s",d.author);
        snprintf(r->source,sizeof(r->source),"%s",input);snprintf(r->output,sizeof(r->output),"%s",output);
        clock_gettime(CLOCK_MONOTONIC,&end);
        r->seconds=(end.tv_sec-start.tv_sec)+(end.tv_nsec-start.tv_nsec)/1e9;
        ok=1;
    }
    fz_catch(d.ctx) {snprintf(err,len,"%s",fz_caught_message(d.ctx));}
    pd_drop_document(&d);fz_drop_context(d.ctx);
    if(!ok){unlink(temporary);pd_free_result(r);}
    else if(result)*result=r;else pd_free_result(r);
    return ok;
}
