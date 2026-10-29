/* KEPUB framing for the XHTML produced by this exporter. See Kobo's EPUB
 * guidelines and kepubify's format documentation. This is not an arbitrary
 * HTML repairer: XML, links, text, MathML and SVG must remain intact. */
#include "internal.h"
#include <ctype.h>
#include <string.h>

typedef struct {
    fz_context *ctx;
    fz_buffer *out;
    int paragraph,segment,new_paragraph;
} Kobo;

static int one_of(const char *tag,const char *const *tags) {
    for(int i=0;tags[i];i++)if(!strcmp(tag,tags[i]))return 1;
    return 0;
}
static void span(Kobo *k) {
    if(k->new_paragraph){k->paragraph++;k->segment=0;k->new_paragraph=0;}
    fz_append_printf(k->ctx,k->out,"<span class=\"koboSpan\" id=\"kobo.%d.%d\">",k->paragraph,++k->segment);
}
static void node(Kobo *k,fz_xml *x,int annotate,int depth) {
    if(depth>128)fz_throw(k->ctx,FZ_ERROR_LIMIT,"KEPUB nesting limit exceeded");
    const char *text=fz_xml_text(x);
    if(text) {
        const char *p=text;while(*p && isspace((unsigned char)*p))p++;
        int wrap=annotate && *p;
        if(wrap)span(k);
        pd_xml(k->ctx,k->out,text);
        if(wrap)fz_append_string(k->ctx,k->out,"</span>");
        return;
    }
    const char *tag=fz_xml_tag(x);
    if(!tag)fz_throw(k->ctx,FZ_ERROR_FORMAT,"Invalid KEPUB element");
    const char *const blocks[]={"p","ol","ul","table","h1","h2","h3","h4","h5","h6",NULL};
    const char *const opaque[]={"math","svg","script","style","pre","audio","video",NULL};
    const char *const empty[]={"img","br","hr","meta","link","input","wbr",NULL};
    int body=!strcmp(tag,"body"),image=annotate && !strcmp(tag,"img");
    if(annotate && one_of(tag,blocks))k->new_paragraph=1;
    if(image){k->new_paragraph=1;span(k);}
    if(one_of(tag,opaque))annotate=0;
    fz_append_printf(k->ctx,k->out,"<%s",tag);
    for(int i=0;;i++) {
        const char *key,*value=fz_dom_get_attribute(k->ctx,x,i,&key);if(!key)break;
        fz_append_printf(k->ctx,k->out," %s=\"",key);pd_xml(k->ctx,k->out,value);fz_append_byte(k->ctx,k->out,'"');
    }
    if(one_of(tag,empty))fz_append_string(k->ctx,k->out,"/>");
    else {
        fz_append_byte(k->ctx,k->out,'>');
        if(body)fz_append_string(k->ctx,k->out,"<div id=\"book-columns\"><div id=\"book-inner\">");
        for(fz_xml *c=fz_xml_down(x);c;c=fz_xml_next(c))node(k,c,body?1:annotate,depth+1);
        if(!strcmp(tag,"head"))fz_append_string(k->ctx,k->out,
            "<style id=\"kobostylehacks\">div#book-inner{margin-top:0;margin-bottom:0}"
            "body#cover #book-columns,body#cover #book-inner{height:100%}</style>");
        if(body)fz_append_string(k->ctx,k->out,"</div></div>");
        fz_append_printf(k->ctx,k->out,"</%s>",tag);
    }
    if(image)fz_append_string(k->ctx,k->out,"</span>");
}
fz_buffer *pd_kepub_xhtml(fz_context *ctx,fz_buffer *input) {
    fz_xml *xml=NULL;Kobo k={.ctx=ctx,.new_paragraph=1};fz_var(xml);fz_var(k);
    fz_try(ctx) {
        xml=fz_parse_xml(ctx,input,1);
        if(!fz_xml_is_tag(fz_xml_root(xml),"html"))fz_throw(ctx,FZ_ERROR_FORMAT,"Invalid KEPUB XHTML root");
        k.out=fz_new_buffer(ctx,fz_buffer_storage(ctx,input,NULL)+1024);
        fz_append_string(ctx,k.out,"<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<!DOCTYPE html>\n");
        node(&k,fz_xml_root(xml),0,0);
    }
    fz_always(ctx)fz_drop_xml(ctx,xml);
    fz_catch(ctx){fz_drop_buffer(ctx,k.out);fz_rethrow(ctx);}
    return k.out;
}
