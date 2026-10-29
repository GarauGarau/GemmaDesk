#include "internal.h"
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

typedef struct {int c,used;PD_Rect box;float size,baseline;int flags;} Evidence;
typedef struct {PD_Rect box;float baseline,size;int n;} Bounds;
typedef struct {PD_Rect box;int used;} Shape;
static int evidence_y(const void *aa,const void *bb) {
    const Evidence *a=aa,*b=bb;float delta=(a->box.y0+a->box.y1)-(b->box.y0+b->box.y1);
    return delta<0?-1:delta>0;
}
static int evidence_x(const void *aa,const void *bb) {
    const Evidence *a=aa,*b=bb;return a->box.x0<b->box.x0?-1:a->box.x0>b->box.x0;
}
static void sequence_text(fz_xml *xml,int *tokens,int *n,int *ok,int depth) {
    if(depth>100){*ok=0;return;}
    const char *text=fz_xml_text(xml);
    if(text)while(*text) {
        int c;text+=fz_chartorune(&c,text);if(pd_math_space(c))continue;
        if(*n>=8192){*ok=0;return;}tokens[(*n)++]=c;
    }
    for(fz_xml *child=fz_xml_down(xml);child;child=fz_xml_next(child))sequence_text(child,tokens,n,ok,depth+1);
}
static int canonical(int c) {
    if(c=='-' || c==0x2010 || c==0x2212)return '-';
    if(c==0x2018 || c==0x2019)return '\'';
    return c;
}
static Bounds empty_bounds(void){return (Bounds){{0,0,0,0},0,0,0};}
static Bounds join_bounds(Bounds a,Bounds b) {
    if(!a.n)return b;
    if(!b.n)return a;
    a.box=rect_union(a.box,b.box);a.baseline=(a.baseline*a.n+b.baseline*b.n)/(a.n+b.n);
    a.size=fmaxf(a.size,b.size);a.n+=b.n;return a;
}
/* Match printed symbols once each. Relations are then checked against their
 * actual source geometry, so a syntactically valid x_i cannot replace x^i. */
static Bounds match_math(fz_xml *xml,Evidence *source,int count,Shape *shapes,int nshapes,int *ok,int depth,int style,int cell,int accent) {
    Bounds whole=empty_bounds();if(depth>100){*ok=0;return whole;}
    const char *text=fz_xml_text(xml),*tag=fz_xml_tag(xml);
    if(tag && (!strcmp(tag,"td") || !strcmp(tag,"th") || !strcmp(tag,"mtd")))cell=1;
    if(tag && !strcmp(tag,"math"))style=-1;
    const char *variant=fz_xml_att(xml,"mathvariant");
    if(variant) {
        if(!strcmp(variant,"normal"))style=0;
        else if(!strcmp(variant,"italic"))style=G_ITALIC;
        else if(!strcmp(variant,"bold"))style=G_BOLD;
        else if(!strcmp(variant,"bold-italic"))style=G_BOLD|G_ITALIC;
        else if(!strcmp(variant,"double-struck"))style=G_DOUBLE_STRUCK;
        else if(!strcmp(variant,"script"))style=G_SCRIPT;
        else *ok=0;
    }
    if(tag && (!strcmp(tag,"b") || !strcmp(tag,"strong")))style=G_BOLD;
    if(tag && (!strcmp(tag,"i") || !strcmp(tag,"em")))style=G_ITALIC;
    Evidence *last_symbol=NULL;
    if(text)for(const char *s=text;*s;) {
        int c;s+=fz_chartorune(&c,s);if(pd_math_space(c))continue;
        int best=-1;
        for(int i=0;i<count;i++)if(!source[i].used && (accent?
            pd_math_accent(source[i].c)==(c==0x2192?c:pd_math_accent(c)) && (c==0x2192 || pd_math_accent(c)):canonical(source[i].c)==canonical(c))){best=i;break;}
        if(best<0 && accent && c==0xaf) {
            for(int i=0;i<nshapes;i++)if(!shapes[i].used) {
                PD_Rect b=shapes[i].box;
                float size=0;for(int j=0;j<count;j++)size=fmaxf(size,source[j].size);
                if(b.y1-b.y0>size*.2f || b.x1-b.x0<(b.y1-b.y0)*3)continue;
                shapes[i].used=1;whole=join_bounds(whole,(Bounds){b,b.y1,0,1});best=-2;break;
            }
            if(best==-2)continue;
        }
        if(best<0){*ok=0;continue;}
        Evidence *g=&source[best];g->used=1;
        if(cell && last_symbol && fabsf(g->baseline-last_symbol->baseline)<g->size*.6f &&
           g->box.x0-last_symbol->box.x1>fmaxf(g->size,last_symbol->size)*2.2f)*ok=0;
        last_symbol=g;
        if(!accent && style>=-1 && (g->flags&G_BOLD)!=(style<0?0:style&G_BOLD))*ok=0;
        if(style>=-1 && (g->flags&G_MATH_VARIANT)!=(style<0?0:style&G_MATH_VARIANT))*ok=0;
        if(style>=0 && !(g->flags&G_MATH_VARIANT) && ((c<128 && isalpha(c)) || (c>=0x370 && c<=0x3ff)) &&
           (g->flags&G_ITALIC)!=(style&G_ITALIC))*ok=0;
        whole=join_bounds(whole,(Bounds){g->box,g->baseline,g->size,1});
    }
    Bounds parts[4],previous=empty_bounds();int n=0;
    for(fz_xml *child=fz_xml_down(xml);child;child=fz_xml_next(child)) {
        int is_accent=tag && n==1 && ((!strcmp(tag,"mover") && fz_xml_att(xml,"accent") && !strcmp(fz_xml_att(xml,"accent"),"true")) ||
            (!strcmp(tag,"munder") && fz_xml_att(xml,"accentunder") && !strcmp(fz_xml_att(xml,"accentunder"),"true")));
        Bounds b=match_math(child,source,count,shapes,nshapes,ok,depth+1,style,cell,accent || is_accent);whole=join_bounds(whole,b);
        if(tag && previous.n && b.n) {
            if((!strcmp(tag,"mrow") || !strcmp(tag,"mtr")) &&
               b.box.x0<previous.box.x0-fmaxf(previous.size,b.size)*.2f)*ok=0;
            if((!strcmp(tag,"mtr") || !strcmp(tag,"tr")) &&
               (b.box.x0<previous.box.x0 || fmaxf(b.box.y0,previous.box.y0)>fminf(b.box.y1,previous.box.y1)+b.size*.15f))*ok=0;
            if((!strcmp(tag,"mtable") || !strcmp(tag,"tbody")) && b.box.y0<previous.box.y0+previous.size*.2f)*ok=0;
        }
        if(b.n)previous=b;
        if(n<4)parts[n]=b;
        n++;
    }
    if(tag && n>=2 && parts[0].n && parts[1].n) {
        Bounds base=parts[0],script=parts[1];float tolerance=base.size*.12f;
        if(!strcmp(tag,"msub") || !strcmp(tag,"msubsup")) {
            if(script.baseline<=base.baseline+tolerance || script.size>base.size*1.02f)*ok=0;
        }
        if(!strcmp(tag,"msup")) {
            if(script.baseline>=base.baseline-tolerance || script.size>base.size*1.02f)*ok=0;
        }
        if(!strcmp(tag,"msubsup") && n==3 && parts[2].n &&
           (parts[2].baseline>=base.baseline-tolerance || parts[2].size>base.size*1.02f))*ok=0;
        if(!strcmp(tag,"mfrac") && (base.box.y0>=script.box.y0 ||
           base.baseline>=script.baseline ||
           fmaxf(base.box.x0,script.box.x0)>fminf(base.box.x1,script.box.x1)+base.size*.2f))*ok=0;
        if(!strcmp(tag,"mfrac") && (!fz_xml_att(xml,"linethickness") || strcmp(fz_xml_att(xml,"linethickness"),"0"))) {
            int bar=0;float width=fmaxf(base.box.x1-base.box.x0,script.box.x1-script.box.x0);
            for(int i=0;i<nshapes;i++) {
                PD_Rect b=shapes[i].box;
                if(!shapes[i].used && b.y1-b.y0<whole.size*.25f && b.x1-b.x0>=width*.65f &&
                   b.y0>=base.box.y1-whole.size*.25f && b.y1<=script.box.y0+whole.size*.25f &&
                   b.x0<=fmaxf(base.box.x0,script.box.x0) && b.x1>=fminf(base.box.x1,script.box.x1)) {
                    shapes[i].used=1;bar=1;break;
                }
            }
            if(!bar)*ok=0;
        }
        int accented=fz_xml_att_eq(xml,"accent","true") || fz_xml_att_eq(xml,"accentunder","true");
        float base_y=accented?(base.box.y0+base.box.y1)*.5f:base.baseline;
        float script_y=accented?(script.box.y0+script.box.y1)*.5f:script.baseline;
        if(!strcmp(tag,"mover") && script_y>=base_y-tolerance)*ok=0;
        if(!strcmp(tag,"munder") && script_y<=base_y+tolerance)*ok=0;
        if((!strcmp(tag,"mover") || !strcmp(tag,"munder")) &&
           (fz_xml_att(xml,"accent") || fz_xml_att(xml,"accentunder")) &&
           (script.box.x1<base.box.x0 || script.box.x0>base.box.x1))*ok=0;
        if(!strcmp(tag,"munderover") && (script.baseline<=base.baseline+tolerance || n!=3 ||
            !parts[2].n || parts[2].baseline>=base.baseline-tolerance))*ok=0;
    }
    /* A radical is implicit in MathML but visible in the PDF. It cannot be
     * invented merely because the radicand's characters happen to match. */
    if(tag && (!strcmp(tag,"msqrt") || !strcmp(tag,"mroot"))) {
        int radical=-1;
        for(int i=0;i<count;i++)if(!source[i].used && source[i].c==0x221a &&
            source[i].box.x0<whole.box.x0 && source[i].box.y0<=whole.box.y1){radical=i;break;}
        if(radical<0)*ok=0;
        else {
            source[radical].used=1;
            for(int i=0;i<nshapes;i++) {
                PD_Rect b=shapes[i].box;
                if(!shapes[i].used && b.y1-b.y0<whole.size*.25f && b.y0<whole.box.y0 &&
                   whole.box.y0-b.y0<whole.size*.5f && b.x0<=whole.box.x0 && b.x1>=whole.box.x1)shapes[i].used=1;
            }
        }
    }
    return whole;
}
static int verify_reconstruction(Document *d,Page *p,Reconstruction *r,const Glyph **order,int count) {
    Evidence *source=NULL;Shape *shapes=NULL;int n=0,cap=0,ok=1,nshapes=0;fz_xml *xml=NULL;fz_buffer *buf=NULL;
    fz_var(source);fz_var(shapes);fz_var(xml);fz_var(buf);fz_var(ok);fz_var(n);
    fz_try(d->ctx) {
        if(order)source=fz_calloc(d->ctx,(size_t)count,sizeof(Evidence));
        for(int li=0;li<p->nline;li++) {
            if(r->line>=0 && li!=r->line)continue;
            Line *l=&p->lines[li];
            for(int gi=0;gi<l->n;gi++) {
                if(r->line>=0 && (gi<r->first || gi>=r->end))continue;
                Glyph *g=&l->glyph[gi];if(pd_math_space(g->c))continue;
                float cx=(g->box.x0+g->box.x1)*.5f,cy=(g->box.y0+g->box.y1)*.5f;
                if(cx<r->box.x0 || cx>r->box.x1 || cy<r->box.y0 || cy>r->box.y1)continue;
                if(r->block && l->owner>=0 && l->owner<p->nblock && p->blocks[l->owner].pub.id!=r->block){ok=0;continue;}
                if(g->flags&G_BAD){ok=0;continue;}
                int index=n;
                if(order) {
                    for(index=0;index<count && order[index]!=g;index++){}
                    if(index==count || source[index].size>0){ok=0;continue;}
                } else source=pd_grow(d->ctx,source,&cap,n+1,sizeof(Evidence));
                PD_Rect region=g->box;float baseline=g->baseline;
                if(r->rotation){region=from_fz(fz_transform_rect(to_fz(region),fz_rotate(r->rotation)));baseline=region.y1;}
                source[index]=(Evidence){g->c,0,region,g->size,baseline,g->flags};n++;
            }
        }
        if(n==0)ok=0;
        if(n>8192)ok=0;
        if(order && n!=count)ok=0;
        int nmarks=p->nmark;const Mark *marks=p->marks;
        if(order)marks=pd_math_marks(p,&nmarks);
        if(r->task==2 && nmarks) {
            shapes=fz_calloc(d->ctx,nmarks,sizeof(Shape));
            for(int i=0;i<nmarks;i++)if(rect_overlap(marks[i].box,r->box)) {
                PD_Rect b=marks[i].box;
                if(r->rotation)b=from_fz(fz_transform_rect(to_fz(b),fz_rotate(r->rotation)));
                shapes[nshapes++]=(Shape){b,0};
            }
        }
        if(ok) {
            buf=fz_new_buffer_from_copied_data(d->ctx,(unsigned char*)r->markup,strlen(r->markup));
            xml=fz_parse_xml(d->ctx,buf,1);
            if(r->task==3 || r->task==1) {
                /* A bag of characters cannot establish that 0.12 stayed in
                 * its cell. Require the complete geometric reading sequence
                 * as well; difficult wrapped cells remain for inspection. */
                int tokens[8192],ntokens=0;sequence_text(fz_xml_root(xml),tokens,&ntokens,&ok,0);
                qsort(source,n,sizeof(Evidence),evidence_y);
                for(int begin=0;begin<n;) {
                    int end=begin+1;float mid=(source[begin].box.y0+source[begin].box.y1)*.5f;
                    while(end<n && (source[end].box.y0+source[end].box.y1)*.5f-mid<source[begin].size*.55f)end++;
                    qsort(source+begin,end-begin,sizeof(Evidence),evidence_x);begin=end;
                }
                if(ntokens!=n)ok=0;
                for(int i=0;i<n && i<ntokens;i++)if(canonical(source[i].c)!=canonical(tokens[i]))ok=0;
            }
            match_math(fz_xml_root(xml),source,n,shapes,nshapes,&ok,0,r->task==2?-1:-2,0,0);
            for(int i=0;i<n;i++)if(!source[i].used)ok=0;
            for(int i=0;i<nshapes;i++)if(!shapes[i].used)ok=0;
        }
    }
    fz_always(d->ctx){fz_drop_xml(d->ctx,xml);fz_drop_buffer(d->ctx,buf);fz_free(d->ctx,source);fz_free(d->ctx,shapes);}
    fz_catch(d->ctx){ok=0;}
    return ok;
}
int pd_verify_reconstruction(Document *d,Page *p,Reconstruction *r) {
    return verify_reconstruction(d,p,r,NULL,0);
}
/* Only source-derived trees supply a correspondence. Model text must still
 * establish its own matches. Coverage, uniqueness and geometry are checked
 * in both paths; repeated symbols are not interchangeable in a source tree. */
int pd_verify_ordered_math(Document *d,Page *p,Reconstruction *r,const Glyph **order,int count) {
    if(!order || count<1 || count>8192)return 0;
    return verify_reconstruction(d,p,r,order,count);
}
static int glyph_x(const void *aa,const void *bb) {
    const Glyph *a=aa,*b=bb;
    if(fabsf(a->box.x0-b->box.x0)>.1f)return a->box.x0<b->box.x0?-1:1;
    return a->baseline<b->baseline?-1:a->baseline>b->baseline;
}
/* Recover only a single reliable baseline with geometric scripts. Bars,
 * roots, unmapped glyphs and multiple baselines require recognition. */
static char *source_formula(Document *d,Page *p,Reconstruction *r,int *hierarchical) {
    Line merged={0},*l=r->line>=0?&p->lines[r->line]:&merged;
    int first=r->first,end=r->end;
    fz_buffer *tex=fz_new_buffer(d->ctx,128);char *result=NULL;fz_var(result);fz_var(merged);fz_var(first);fz_var(end);
    fz_try(d->ctx) {
        if(r->line<0) {
            /* A PDF may emit a base, exponent and subscript as three text
             * objects. Reconstruct their common baseline from geometry. */
            for(int li=0;li<p->nline;li++)for(int gi=0;gi<p->lines[li].n;gi++) {
                Glyph *g=&p->lines[li].glyph[gi];float x=(g->box.x0+g->box.x1)*.5f,y=(g->box.y0+g->box.y1)*.5f;
                if(pd_math_space(g->c) || x<r->box.x0 || x>r->box.x1 || y<r->box.y0 || y>r->box.y1)continue;
                merged.glyph=pd_grow(d->ctx,merged.glyph,&merged.cap,merged.n+1,sizeof(Glyph));
                merged.glyph[merged.n++]=*g;merged.size=fmaxf(merged.size,g->size);
            }
            int bases=0;
            for(int i=0;i<merged.n;i++)if(merged.glyph[i].size>=merged.size*.9f){merged.baseline+=merged.glyph[i].baseline;bases++;}
            if(bases)merged.baseline/=bases;
            merged.dx=1;first=0;end=merged.n;
            qsort(merged.glyph,merged.n,sizeof(Glyph),glyph_x);
        }
        int safe=fabsf(l->dy)<.05f,script=0,base=0;float script_baseline=0,script_size=0;
        if(end<=first || end-first>8192)safe=0;
        for(int j=0;j<p->nmark;j++)if(rect_overlap(r->box,p->marks[j].box))safe=0;
        float base_size=0;
        for(int i=first;i<end;i++) {
            int c=l->glyph[i].c;
            if(!pd_math_space(c) && !pd_math_accent(c) && !(c<128 && strchr("[](){}",c)))base_size=fmaxf(base_size,l->glyph[i].size);
        }
        for(int i=first;i<end;i++) {
            Glyph *g=&l->glyph[i];if(g->flags&G_BAD || g->c=='\\')safe=0;
            if(pd_math_accent(g->c)){safe=0;*hierarchical=2;}
            if(g->c<128 && strchr("[](){}",g->c) && base_size>0 && g->box.y1-g->box.y0>base_size*1.8f){safe=0;*hierarchical=2;}
            if(pd_math_space(g->c)){if(g->c<=32 || g->c==0xa0)fz_append_string(d->ctx,tex,"\\,");continue;}
            if(g->c>=0x2190 && g->c<=0x21ff)for(int j=first;j<end;j++) {
                Glyph *label=&l->glyph[j];float x=(label->box.x0+label->box.x1)*.5f;
                if(label->size<g->size*.95f && fabsf(label->baseline-g->baseline)>g->size*.16f &&
                   x>=g->box.x0 && x<=g->box.x1)*hierarchical=1;
            }
            int kind=pd_glyph_script(l,g);
            if(!kind && fabsf(g->baseline-l->baseline)>l->size*.3f)safe=0;
            if(kind && !base)safe=0;
            /* Try structural reconstruction before committing a flat
             * script when the PDF contains another nested baseline. */
            if(kind && kind==script && fabsf(g->baseline-script_baseline)>fmaxf(g->size,script_size)*.18f &&
               fminf(g->size,script_size)<fmaxf(g->size,script_size)*.95f)*hierarchical=1;
            if(kind!=script) {
                if(script)fz_append_byte(d->ctx,tex,'}');
                if(kind)fz_append_string(d->ctx,tex,kind>0?"^{":"_{");
                script=kind;
                script_baseline=g->baseline;script_size=g->size;
            }
            pd_math_glyph(d->ctx,tex,g);
            if(!kind && g->c>32)base=1;
        }
        if(script)fz_append_byte(d->ctx,tex,'}');
        if(safe)result=fz_strdup(d->ctx,fz_string_from_buffer(d->ctx,tex));
    }
    fz_always(d->ctx){fz_drop_buffer(d->ctx,tex);fz_free(d->ctx,merged.glyph);}
    fz_catch(d->ctx)fz_rethrow(d->ctx);
    return result;
}
static Reconstruction *candidate(Document *d,Page *p,int task,int block,PD_Rect box) {
    p->recon=pd_grow(d->ctx,p->recon,&p->crecon,p->nrecon+1,sizeof(Reconstruction));
    Reconstruction *r=&p->recon[p->nrecon++];memset(r,0,sizeof(*r));
    r->task=task;r->block=block;r->line=-1;r->box=box;
    for(int i=0;i<p->nblock;i++)if(p->blocks[i].pub.id==block)r->display=p->blocks[i].pub.kind==PD_EQUATION;
    return r;
}
static int integer_attribute(fz_xml *x,const char *name) {
    const char *s=fz_xml_att(x,name);if(!s)return 1;
    char *end;long n=strtol(s,&end,10);return !*end && n>=1 && n<=512?(int)n:0;
}
static void table_text(Document *d,fz_xml *x,fz_buffer *out,int *ok,int depth) {
    if(depth>64){*ok=0;return;}
    const char *s=fz_xml_text(x),*tag=fz_xml_tag(x);
    if(s) {
        /* Math delimiters in a cell invoke the same parser as display math. */
        while(*s) {
            const char *start=strstr(s,"\\("),*dollar=strchr(s,'$');int width=2;
            if(dollar && (!start || dollar<start)){start=dollar;width=1;}
            if(!start){pd_xml(d->ctx,out,s);break;}
            fz_buffer *prefix=fz_new_buffer_from_copied_data(d->ctx,(const unsigned char*)s,(size_t)(start-s));
            pd_xml(d->ctx,out,fz_string_from_buffer(d->ctx,prefix));fz_drop_buffer(d->ctx,prefix);
            const char *end=width==2?strstr(start+2,"\\)"):strchr(start+1,'$');
            if(!end){*ok=0;break;}
            fz_buffer *tex=fz_new_buffer_from_copied_data(d->ctx,(const unsigned char*)start,(size_t)(end-start+width));char reason[160];
            char *math=pd_mathml(d->ctx,fz_string_from_buffer(d->ctx,tex),0,reason,sizeof(reason));fz_drop_buffer(d->ctx,tex);
            if(!math){*ok=0;break;}
            fz_append_string(d->ctx,out,math);fz_free(d->ctx,math);s=end+width;
        }
    }
    if(tag && strcmp(tag,"td") && strcmp(tag,"th") && strcmp(tag,"b") && strcmp(tag,"strong") &&
       strcmp(tag,"i") && strcmp(tag,"em") && strcmp(tag,"span") && strcmp(tag,"sup") && strcmp(tag,"sub") && strcmp(tag,"br")){*ok=0;return;}
    int styled=tag && (!strcmp(tag,"sup") || !strcmp(tag,"sub") || !strcmp(tag,"b") || !strcmp(tag,"i"));
    if(styled)fz_append_printf(d->ctx,out,"<%s>",tag);
    if(tag && !strcmp(tag,"br"))fz_append_string(d->ctx,out,"<br/>");
    for(fz_xml *c=fz_xml_down(x);c;c=fz_xml_next(c))table_text(d,c,out,ok,depth+1);
    if(styled)fz_append_printf(d->ctx,out,"</%s>",tag);
}
/* OTSL encodes a rectangular lattice. Continuation tokens refer to an
 * existing cell; ragged rows and impossible merges are rejected, never
 * repaired by inventing empty cells. */
typedef struct {int row,col,rows,cols;const char *text;size_t length;} Cell;
static char *otsl_html(Document *d,const char *raw) {
    const char *s=raw;int *grid=NULL;Cell *cells=NULL;fz_buffer *out=NULL;char *result=NULL;
    int row=0,col=0,width=0,ncell=0,ok=1;
    fz_var(grid);fz_var(cells);fz_var(out);fz_var(result);fz_var(row);fz_var(col);fz_var(width);fz_var(ncell);fz_var(ok);fz_var(s);
    fz_try(d->ctx) {
        grid=fz_calloc(d->ctx,512*512,sizeof(int));cells=fz_calloc(d->ctx,8192,sizeof(Cell));
        while(*s && ok) {
            while(isspace((unsigned char)*s))s++;
            if(!*s)break;
            if(!strncmp(s,"<nl>",4)) {
                if(!row)width=col;else if(col!=width)ok=0;
                row++;col=0;s+=4;if(row>512)ok=0;continue;
            }
            if(row>=512 || col>=512 || ncell>=8192){ok=0;break;}
            int kind=!strncmp(s,"<fcel>",6)?1:!strncmp(s,"<ecel>",6)?2:!strncmp(s,"<lcel>",6)?3:
                !strncmp(s,"<ucel>",6)?4:!strncmp(s,"<xcel>",6)?5:0;
            if(!kind){ok=0;break;}s+=6;
            const char *begin=s,*end=s;
            while(*end && strncmp(end,"<fcel>",6) && strncmp(end,"<ecel>",6) && strncmp(end,"<lcel>",6) &&
                  strncmp(end,"<ucel>",6) && strncmp(end,"<xcel>",6) && strncmp(end,"<nl>",4))end++;
            while(begin<end && isspace((unsigned char)*begin))begin++;
            const char *last=end;while(last>begin && isspace((unsigned char)last[-1]))last--;
            if(kind!=1 && last>begin){ok=0;break;}
            int id=0;
            if(kind<=2){id=++ncell;cells[id-1]=(Cell){row,col,1,1,begin,(size_t)(last-begin)};}
            if(kind==3 && col>0)id=grid[row*512+col-1];
            if(kind==4 && row>0)id=grid[(row-1)*512+col];
            if(kind==5 && row>0 && col>0 && grid[(row-1)*512+col]==grid[row*512+col-1])id=grid[row*512+col-1];
            if(!id){ok=0;break;}
            Cell *cell=&cells[id-1];
            if(kind==3 && cell->row!=row)ok=0;
            if(kind==4 && cell->col!=col)ok=0;
            if(cell->rows<row-cell->row+1)cell->rows=row-cell->row+1;
            if(cell->cols<col-cell->col+1)cell->cols=col-cell->col+1;
            grid[row*512+col]=id;
            col++;s=end;
        }
        if(col || !row || !width)ok=0;
        /* Verify the entire rectangle, including the intersection of a
         * rowspan and a colspan. Each lattice position has one owner. */
        for(int i=0;i<ncell && ok;i++) {
            Cell *c=&cells[i];if(c->row+c->rows>row || c->col+c->cols>width){ok=0;break;}
            for(int y=c->row;y<c->row+c->rows;y++)for(int x=c->col;x<c->col+c->cols;x++)if(grid[y*512+x]!=i+1)ok=0;
        }
        if(ok) {
            out=fz_new_buffer(d->ctx,512);fz_append_string(d->ctx,out,"<table>");
            for(int y=0;y<row;y++) {
                fz_append_string(d->ctx,out,"<tr>");
                for(int i=0;i<ncell;i++) {
                    Cell *c=&cells[i];if(c->row!=y)continue;
                    fz_append_printf(d->ctx,out,"<td rowspan=\"%d\" colspan=\"%d\">",c->rows,c->cols);
                    fz_buffer *text=fz_new_buffer_from_copied_data(d->ctx,(const unsigned char*)c->text,c->length);
                    pd_xml(d->ctx,out,fz_string_from_buffer(d->ctx,text));fz_drop_buffer(d->ctx,text);fz_append_string(d->ctx,out,"</td>");
                }
                fz_append_string(d->ctx,out,"</tr>");
            }
            fz_append_string(d->ctx,out,"</table>");result=fz_strdup(d->ctx,fz_string_from_buffer(d->ctx,out));
        }
    }
    fz_always(d->ctx){fz_free(d->ctx,grid);fz_free(d->ctx,cells);fz_drop_buffer(d->ctx,out);}
    fz_catch(d->ctx)fz_rethrow(d->ctx);
    return result;
}
static char *native_table(Document *d,const char *raw,char reason[192]) {
    fz_xml *xml=NULL;fz_buffer *buf=NULL,*out=NULL;char *result=NULL;fz_var(xml);fz_var(buf);fz_var(out);fz_var(result);
    fz_try(d->ctx) {
        const char *start=strstr(raw,"<table"),*end=start?strstr(start,"</table>"):NULL;
        if(!start || !end){snprintf(reason,192,"No complete HTML table in recognition output");}
        else {
            buf=fz_new_buffer_from_copied_data(d->ctx,(const unsigned char*)start,(size_t)(end+8-start));
            xml=fz_parse_xml(d->ctx,buf,1);fz_xml *root=fz_xml_root(xml);
            out=fz_new_buffer(d->ctx,512);fz_append_string(d->ctx,out,"<table class=\"native-table\"><tbody>");
            int ok=1,rows=0,columns=0,spans[512]={0};
            for(fz_xml *tr=fz_xml_find_dfs(root,"tr",NULL,NULL);tr;tr=fz_xml_find_next_dfs(tr,"tr",NULL,NULL)) {
                if(++rows>512){ok=0;break;}int col=0;
                fz_append_string(d->ctx,out,"<tr>");
                for(fz_xml *td=fz_xml_down(tr);td;td=fz_xml_next(td)) {
                    if(fz_xml_text(td)){const char *t=fz_xml_text(td);while(isspace((unsigned char)*t))t++;if(*t)ok=0;continue;}
                    if(!fz_xml_is_tag(td,"td") && !fz_xml_is_tag(td,"th")){ok=0;continue;}
                    int cs=integer_attribute(td,"colspan"),rs=integer_attribute(td,"rowspan");
                    while(col<512 && spans[col])col++;
                    if(!cs || !rs || col+cs>512){ok=0;break;}
                    for(int c=col;c<col+cs;c++){if(spans[c])ok=0;spans[c]=rs;}
                    col+=cs;const char *tag=fz_xml_tag(td);
                    fz_append_printf(d->ctx,out,"<%s colspan=\"%d\" rowspan=\"%d\">",tag,cs,rs);table_text(d,td,out,&ok,0);fz_append_printf(d->ctx,out,"</%s>",tag);
                }
                int width=col;for(int c=0;c<512;c++)if(spans[c]){if(c+1>width)width=c+1;spans[c]--;}
                if(rows==1)columns=width;else if(width!=columns)ok=0;
                fz_append_string(d->ctx,out,"</tr>");
            }
            for(int c=0;c<512;c++)if(spans[c])ok=0;
            fz_append_string(d->ctx,out,"</tbody></table>");
            if(ok && rows>0 && columns>0)result=fz_strdup(d->ctx,fz_string_from_buffer(d->ctx,out));
            else snprintf(reason,192,"Table grid is incomplete, overlapping or unsupported");
        }
    }
    fz_always(d->ctx){fz_drop_xml(d->ctx,xml);fz_drop_buffer(d->ctx,buf);fz_drop_buffer(d->ctx,out);}
    fz_catch(d->ctx){snprintf(reason,192,"Table markup could not be parsed completely");}
    return result;
}
static void parse_content(Document *d,Reconstruction *r) {
    if(r->task==2)r->markup=pd_mathml(d->ctx,r->raw,r->display,r->reason,sizeof(r->reason));
    if(r->task==3) {
        if(strstr(r->raw,"<fcel>") || strstr(r->raw,"<ecel>")) {
            char *html=otsl_html(d,r->raw);
            if(html){r->markup=native_table(d,html,r->reason);fz_free(d->ctx,html);}
            else snprintf(r->reason,sizeof(r->reason),"OTSL grid is incomplete or has inconsistent cell spans");
        } else r->markup=native_table(d,r->raw,r->reason);
    }
    if(r->task==1) {
        fz_buffer *out=fz_new_buffer(d->ctx,128);fz_append_string(d->ctx,out,"<p>");pd_xml(d->ctx,out,r->raw);fz_append_string(d->ctx,out,"</p>");
        r->markup=fz_strdup(d->ctx,fz_string_from_buffer(d->ctx,out));fz_drop_buffer(d->ctx,out);
    }
}
int pd_check_recognition(int task,const char *text,char *error,size_t size) {
    Document d={0};Reconstruction r={0};int ok=0;fz_var(r);fz_var(ok);
    d.ctx=fz_new_context(NULL,NULL,0);if(!d.ctx)return 0;
    r.task=task;r.line=-1;r.raw=(char*)text;
    fz_try(d.ctx){parse_content(&d,&r);ok=r.markup!=NULL;snprintf(error,size,"%s",ok?"Syntax checked. Compare the content with the PDF before using it.":r.reason);}
    fz_catch(d.ctx)snprintf(error,size,"%s",fz_caught_message(d.ctx));
    fz_free(d.ctx,r.markup);fz_drop_context(d.ctx);return ok;
}
static const PD_Correction *correction(Document *d,Page *p,Reconstruction *r) {
    if(!d->opt)return NULL;
    for(int i=d->opt->correction_count-1;i>=0;i--) {
        const PD_Correction *c=&d->opt->corrections[i];
        if(c->page==p->number+1 && c->task==r->task && fabsf(c->box.x0-r->box.x0)<.01f &&
           fabsf(c->box.y0-r->box.y0)<.01f && fabsf(c->box.x1-r->box.x1)<.01f && fabsf(c->box.y1-r->box.y1)<.01f)return c;
    }
    return NULL;
}
/* Preserve decisions saved for old, smaller formula regions when a new
 * layout would join them. A decision for the complete new frame permits it. */
int pd_math_box_edited(Document *d,Page *p,PD_Rect box) {
    if(!d->opt)return 0;
    int contained=0;
    for(int i=d->opt->correction_count-1;i>=0;i--) {
        const PD_Correction *c=&d->opt->corrections[i];if(c->page!=p->number+1 || c->task!=2)continue;
        PD_Rect a=c->box;
        if(fabsf(a.x0-box.x0)<.01f && fabsf(a.y0-box.y0)<.01f && fabsf(a.x1-box.x1)<.01f && fabsf(a.y1-box.y1)<.01f)return 0;
        if(a.x0>=box.x0-.01f && a.x1<=box.x1+.01f && a.y0>=box.y0-.01f && a.y1<=box.y1+.01f)contained=1;
    }
    return contained;
}
static int has_source_text(Page *p,Reconstruction *r) {
    for(int li=0;li<p->nline;li++) {
        if(r->line>=0 && li!=r->line)continue;
        Line *l=&p->lines[li];
        for(int gi=0;gi<l->n;gi++) {
            if(r->line>=0 && (gi<r->first || gi>=r->end))continue;
            Glyph *g=&l->glyph[gi];if(g->c<=32)continue;
            float x=(g->box.x0+g->box.x1)*.5f,y=(g->box.y0+g->box.y1)*.5f;
            if(x<r->box.x0 || x>r->box.x1 || y<r->box.y0 || y>r->box.y1)continue;
            return 1;
        }
    }
    return 0;
}
static int symbol_order(const void *aa,const void *bb) {
    int a=*(const int*)aa,b=*(const int*)bb;return (a>b)-(a<b);
}
static int evidence_symbol(const void *aa,const void *bb) {
    const Evidence *a=aa,*b=bb;return (a->c>b->c)-(a->c<b->c);
}
/* A second look can repair a clipped symbol, not a table-grid/parser limit or
 * an order/style disagreement. Compare symbol multiplicities, then require
 * every missing occurrence to have source evidence outside the rendered crop.
 * Verification still uses the original region; expansion never relaxes it. */
int pd_recognition_retry_box(Document *d,Page *p,Reconstruction *r,PD_Rect *box) {
    if(!r->markup || r->accepted || r->task==1)return 0;
    Evidence *source=NULL;fz_xml *xml=NULL;fz_buffer *buf=NULL;
    int n=0,cap=0,ok=1,retry=0;PD_Rect expanded=r->box;
    PD_Rect crop=from_fz(fz_intersect_rect(to_fz(rect_pad(r->box,.6f)),to_fz(p->box)));
    fz_var(source);fz_var(xml);fz_var(buf);fz_var(n);fz_var(ok);fz_var(retry);fz_var(expanded);
    fz_try(d->ctx) {
        for(int li=0;li<p->nline;li++) {
            if(r->line>=0 && li!=r->line)continue;
            Line *l=&p->lines[li];
            for(int gi=0;gi<l->n;gi++) {
                if(r->line>=0 && (gi<r->first || gi>=r->end))continue;
                Glyph *g=&l->glyph[gi];if(g->c<=32 || g->c==0xa0)continue;
                float x=(g->box.x0+g->box.x1)*.5f,y=(g->box.y0+g->box.y1)*.5f;
                if(x<r->box.x0 || x>r->box.x1 || y<r->box.y0 || y>r->box.y1)continue;
                if(g->flags&G_BAD || n>=8192){ok=0;continue;}
                source=pd_grow(d->ctx,source,&cap,n+1,sizeof(Evidence));
                source[n++]=(Evidence){.c=canonical(g->c),.box=from_fz(fz_intersect_rect(to_fz(g->box),to_fz(p->box)))};
            }
        }
        if(ok && n) {
            int tokens[8192],ntokens=0;
            buf=fz_new_buffer_from_copied_data(d->ctx,(const unsigned char*)r->markup,strlen(r->markup));
            xml=fz_parse_xml(d->ctx,buf,1);sequence_text(fz_xml_root(xml),tokens,&ntokens,&ok,0);
            for(int i=0;i<ntokens;i++)tokens[i]=canonical(tokens[i]);
            qsort(tokens,ntokens,sizeof(int),symbol_order);qsort(source,n,sizeof(Evidence),evidence_symbol);
            int out=0;
            for(int begin=0;ok && begin<n;) {
                int end=begin+1,first=out,clipped=0;
                while(end<n && source[end].c==source[begin].c)end++;
                if(out<ntokens && tokens[out]<source[begin].c){ok=0;break;}
                while(out<ntokens && tokens[out]==source[begin].c)out++;
                int missing=(end-begin)-(out-first);
                if(missing<0){ok=0;break;}
                if(missing)for(int i=begin;i<end;i++) {
                    PD_Rect g=source[i].box;
                    if(g.x0<crop.x0 || g.y0<crop.y0 || g.x1>crop.x1 || g.y1>crop.y1) {
                        clipped++;expanded=rect_union(expanded,g);
                    }
                }
                if(missing>clipped)ok=0;
                if(missing>0)retry=1;
                begin=end;
            }
            if(out!=ntokens)ok=0;
        } else ok=0;
    }
    fz_always(d->ctx){fz_free(d->ctx,source);fz_drop_xml(d->ctx,xml);fz_drop_buffer(d->ctx,buf);}
    fz_catch(d->ctx){ok=0;}
    if(ok && retry){*box=expanded;return 1;}
    return 0;
}
static int content_is_graphical(Document *d,Page *p,Reconstruction *r) {
    if(!d->opt || !d->opt->reader) {
        snprintf(r->reason,sizeof(r->reason),"Original artwork selected; automatic transcription skipped");
        return 1;
    }
    if(r->task!=2)return 0;
    for(int i=0;i<p->nblock;i++)if(p->blocks[i].pub.id==r->block) {
        PD_Block *b=&p->blocks[i].pub;
        if(b->kind==PD_FIGURE || b->kind==PD_TABLE || b->kind==PD_PAGE_IMAGE) {
            snprintf(r->reason,sizeof(r->reason),"Math inside original artwork; automatic transcription skipped");return 1;
        }
    }
    /* Preserve the existing wide-expression guard. Removing it produced
     * measured horizontal overflow in compound expressions and mixed prose.
     * MathML support alone does not establish a usable narrow-screen layout. */
    float width=r->rotation==90 || r->rotation==270?r->box.y1-r->box.y0:r->box.x1-r->box.x0;
    float size=r->display?d->body:r->line>=0?p->lines[r->line].size:0;
    if(size>0 && width/size>=24) {
        snprintf(r->reason,sizeof(r->reason),"Wide formula retained as artwork; automatic transcription skipped");return 1;
    }
    return 0;
}
static void recognize(Document *d,Page *p,Reconstruction *r,int rotation) {
    r->rotation=rotation;
    const PD_Correction *edit=correction(d,p,r);
    if(edit) {
        r->manual=1;
        if(edit->action==2){snprintf(r->reason,sizeof(r->reason),"Original retained by user");return;}
        r->raw=fz_strdup(d->ctx,edit->text?edit->text:"");parse_content(d,r);
        r->accepted=r->markup!=NULL;
        if(r->accepted)snprintf(r->reason,sizeof(r->reason),"Content inspected and accepted by user; syntax checked");
        return;
    }
    if(content_is_graphical(d,p,r)){r->skipped=1;return;}
    if(!has_source_text(p,r)) {
        snprintf(r->reason,sizeof(r->reason),"Original retained; region has no PDF text to verify recognition");
        return;
    }
    int hierarchical=0;
    if(r->task==2)r->raw=source_formula(d,p,r,&hierarchical);
    if(r->raw) {
        parse_content(d,r);r->accepted=r->markup && pd_verify_reconstruction(d,p,r);
        if(r->accepted && !hierarchical){snprintf(r->reason,sizeof(r->reason),"Native source symbols, style and script geometry");return;}
    }
    if(r->task==2 && pd_source_math(d,p,r))return;
    if(r->accepted){snprintf(r->reason,sizeof(r->reason),"Native source symbols, style and script geometry");return;}
    if(r->raw) {
        if(!d->model){snprintf(r->reason,sizeof(r->reason),"Source reconstruction needs inspection; original retained");return;}
        fz_free(d->ctx,r->raw);fz_free(d->ctx,r->markup);r->raw=r->markup=NULL;
    }
    if(!d->model){snprintf(r->reason,sizeof(r->reason),"Original retained; recognition unavailable");return;}
    PD_Rect crop=r->box;
    for(int attempt=0;attempt<2;attempt++) {
        double seconds=0;int cached=0;
        char *raw=pd_model_region(d,p,crop,rotation,r->task,attempt,&seconds,&cached);
        r->seconds+=seconds;r->cached+=cached;r->attempts++;
        if(!raw)break;
        fz_free(d->ctx,r->raw);fz_free(d->ctx,r->markup);r->raw=raw;r->markup=NULL;
        parse_content(d,r);r->accepted=r->markup && pd_verify_reconstruction(d,p,r);
        if(r->accepted){snprintf(r->reason,sizeof(r->reason),"Recognition matches source symbols, order and tested spatial relations");return;}
        if(attempt || !pd_recognition_retry_box(d,p,r,&crop))break;
        r->retry_box=crop;
    }
    if(r->markup)snprintf(r->reason,sizeof(r->reason),"Original retained; symbols, style, order or spatial relations disagree");
    else if(!r->raw)snprintf(r->reason,sizeof(r->reason),"Original retained; recognition unavailable");
}
/* The recognizer reads the grid, while the original object keeps its caption
 * and notes. Trim only explicit labels outside all numeric-cell evidence.
 * Work in the object's upright coordinates, including sideways tables. */
static PD_Rect table_content_box(Page *p,Block *b) {
    fz_matrix turn=fz_rotate(b->pub.rotation);
    PD_Rect box=from_fz(fz_transform_rect(to_fz(b->pub.box),turn));
    float first=box.y1,last=box.y0;int cells=0;
    for(int i=0;i<b->n;i++) {
        Line *l=&p->lines[b->lines[i]];
        if(l->caption || !l->digits || l->letters>l->digits*.5f)continue;
        PD_Rect cell=from_fz(fz_transform_rect(to_fz(l->box),turn));
        first=fminf(first,cell.y0);last=fmaxf(last,cell.y1);cells++;
    }
    if(cells<2)return b->pub.box;
    for(int i=0;i<b->n;i++) {
        Line *l=&p->lines[b->lines[i]];PD_Rect label=from_fz(fz_transform_rect(to_fz(l->box),turn));
        const char *s=l->text;while(isspace((unsigned char)*s))s++;
        int note=!strncasecmp(s,"Note:",5) || !strncasecmp(s,"Notes:",6) ||
                 !strncasecmp(s,"Note.",5) || !strncasecmp(s,"Notes.",6) ||
                 !strncasecmp(s,"Source:",7) || !strncasecmp(s,"Sources:",8);
        if(l->caption && label.y1<first-l->size*.2f)box.y0=fmaxf(box.y0,label.y1+l->size*.15f);
        if((l->caption || note) && label.y0>last+l->size*.2f)box.y1=fminf(box.y1,label.y0-l->size*.15f);
    }
    return from_fz(fz_transform_rect(to_fz(box),fz_rotate(-b->pub.rotation)));
}
/* PDF text extraction may put a spacing/combining accent in its own token,
 * separated from its base by synthetic spaces. Join only the vertically
 * superposed letter on the same source line; never claim intervening prose. */
static void inline_accents(Document *d,Page *p,Line *l) {
    for(int ai=0;ai<l->n;ai++) {
        Glyph *a=&l->glyph[ai];if(!(a->flags&G_INLINE) || !pd_math_accent(a->c))continue;
        float x=(a->box.x0+a->box.x1)*.5f,y=(a->box.y0+a->box.y1)*.5f;
        int base=-1;
        for(int bi=0;bi<l->n;bi++)if(bi!=ai) {
            Glyph *b=&l->glyph[bi];int c=b->c;
            if(!((c<128 && isalpha(c)) || (c>=0x370 && c<=0x3ff) || (c>=0x1d400 && c<=0x1d7ff)))continue;
            if(x<b->box.x0 || x>b->box.x1 || y>=(b->box.y0+b->box.y1)*.5f ||
               b->box.y0-a->box.y1>b->size*.5f || fabsf(b->size-a->size)>b->size*.2f)continue;
            if(base>=0){base=-1;break;}base=bi;
        }
        if(base<0)continue;
        int start=base<ai?base:ai,end=base>ai?base:ai,ok=1;
        for(int i=start+1;i<end;i++)if(!pd_math_space(l->glyph[i].c) && !(l->glyph[i].flags&G_INLINE))ok=0;
        int first=start,last=end;
        while(first>0 && (l->glyph[first-1].flags&G_INLINE))first--;
        while(last+1<l->n && (l->glyph[last+1].flags&G_INLINE))last++;
        PD_Rect box=l->glyph[first].box;
        for(int i=first+1;i<=last;i++)box=rect_union(box,l->glyph[i].box);
        if(pd_math_box_edited(d,p,box))ok=0;
        if(ok)for(int i=start;i<=end;i++)l->glyph[i].flags|=G_INLINE;
    }
}
void pd_reconstruct(Document *d) {
    /* Keep inspectable regions and explicit edits in both profiles. Automatic
     * transcription is reserved for content the selected reader can use. */
    if((!d->opt || (!d->opt->reader && !d->opt->correction_count)) && !d->model)return;
    for(int pi=0;pi<d->npage;pi++) {
        Page *p=&d->pages[pi];pd_check(d,"Reconstructing content",pi,d->npage);
        for(int bi=0;bi<p->nblock;bi++) {
            Block *b=&p->blocks[bi];if(b->pub.omitted || b->pub.kind==PD_MARGIN || b->pub.kind==PD_DETAIL)continue;
            if(b->pub.kind==PD_EQUATION || b->pub.kind==PD_TABLE) {
                Reconstruction *r=candidate(d,p,b->pub.kind==PD_TABLE?3:2,b->pub.id,b->pub.kind==PD_TABLE?table_content_box(p,b):b->pub.box);
                b->pub.native=p->nrecon;
                if(b->pub.kind==PD_EQUATION && b->n==1){r->line=b->lines[0];r->first=0;r->end=p->lines[r->line].n;}
                recognize(d,p,r,b->pub.rotation);if(!r->accepted && !r->skipped)b->pub.review=1;
            } else if(b->pub.kind==PD_PAGE_IMAGE) {
                /* Existing explicit corrections do not require another OCR
                 * run. Preserve them even when inference is skipped. */
                if(!p->nhints && d->opt)for(int ci=0;ci<d->opt->correction_count;ci++) {
                    const PD_Correction *c=&d->opt->corrections[ci];
                    if(c->page!=p->number+1 || c->task<1 || c->task>3)continue;
                    if(c->box.x0<p->box.x0 || c->box.y0<p->box.y0 || c->box.x1>p->box.x1 || c->box.y1>p->box.y1)continue;
                    int duplicate=0;
                    for(int ri=0;ri<p->nrecon;ri++) {
                        Reconstruction *r=&p->recon[ri];
                        if(r->task==c->task && fabsf(r->box.x0-c->box.x0)<.01f && fabsf(r->box.y0-c->box.y0)<.01f &&
                           fabsf(r->box.x1-c->box.x1)<.01f && fabsf(r->box.y1-c->box.y1)<.01f)duplicate=1;
                    }
                    if(!duplicate){Reconstruction *r=candidate(d,p,c->task,b->pub.id,c->box);recognize(d,p,r,0);}
                }
                for(int hi=0;hi<p->nhints;hi++) {
                    ModelHint *h=&p->hints[hi];int task=!strcmp(h->role,"table")?3:!strncmp(h->role,"equation",8)?2:
                        !strcmp(h->role,"text") || !strcmp(h->role,"paragraph_title") || !strcmp(h->role,"doc_title")?1:0;
                    if(!task)continue;
                    Reconstruction *r=candidate(d,p,task,b->pub.id,h->box);recognize(d,p,r,(360-h->rotation)%360);
                }
            }
            if(b->pub.kind!=PD_EQUATION && b->pub.kind!=PD_PAGE_IMAGE) {
                for(int li=0;li<b->n;li++) {
                    int index=b->lines[li];Line *l=&p->lines[index];
                    if(d->opt && d->opt->reader && b->pub.kind!=PD_FIGURE && b->pub.kind!=PD_TABLE)inline_accents(d,p,l);
                    /* Symbol fonts inside figure labels and cells still
                     * belong to the inventory. Their source artwork remains
                     * the reading representation, so they are not counted
                     * as visible native math. */
                    if(b->pub.kind==PD_FIGURE || b->pub.kind==PD_TABLE)for(int start=0;start<l->n;) {
                        while(start<l->n && l->glyph[start].c<=32)start++;
                        int end=start,math=0;
                        while(end<l->n && l->glyph[end].c>32){math|=l->glyph[end].flags&G_MATH;end++;}
                        if(math)for(int j=start;j<end;j++)l->glyph[j].flags|=G_INLINE;
                        start=end+1;
                    }
                    for(int g=0;g<l->n;g++) {
                        if(l->glyph[g].ref || !(l->glyph[g].flags&G_INLINE))continue;
                        int end=g+1;PD_Rect box=l->glyph[g].box;
                        while(end<l->n && (l->glyph[end].flags&G_INLINE) && !l->glyph[end].ref)box=rect_union(box,l->glyph[end++].box);
                        Reconstruction *r=candidate(d,p,2,b->pub.id,box);r->line=index;r->first=g;r->end=end;
                        l->glyph[g].native=p->nrecon;recognize(d,p,r,0);g=end-1;
                    }
                }
            }
        }
    }
}
fz_buffer *pd_reconstruction_report(Document *d) {
    fz_buffer *out=fz_new_buffer(d->ctx,4096);
    fz_append_printf(d->ctx,out,"{\"schema\":3,\"source_sha256\":\"%s\",\"acceptance\":\"exact source symbol inventory and supported spatial relations; not a semantic proof\",\"retry_policy\":\"One expanded crop only for missing source symbols clipped by its boundary\",\"regions\":[",d->source_digest);
    int comma=0;
    for(int pi=0;pi<d->npage;pi++)for(int i=0;i<d->pages[pi].nrecon;i++) {
        Reconstruction *r=&d->pages[pi].recon[i];
        fz_append_printf(d->ctx,out,"%s{\"page\":%d,\"index\":%d,\"block\":%d,\"task\":%d,\"accepted\":%s,\"cached\":%s,\"seconds\":%.3f,\"bbox\":[%.4f,%.4f,%.4f,%.4f],\"reason\":",comma++?",":"",d->pages[pi].number+1,i+1,r->block,r->task,r->accepted?"true":"false",r->cached?"true":"false",r->seconds,r->box.x0,r->box.y0,r->box.x1,r->box.y1);
        pd_json(d->ctx,out,r->reason);fz_append_printf(d->ctx,out,",\"visible\":%d,\"manual\":%s,\"skipped\":%s,\"attempts\":%d",r->visible,r->manual?"true":"false",r->skipped?"true":"false",r->attempts);
        if(r->attempts>1)fz_append_printf(d->ctx,out,",\"retry_bbox\":[%.4f,%.4f,%.4f,%.4f]",r->retry_box.x0,r->retry_box.y0,r->retry_box.x1,r->retry_box.y1);
        fz_append_string(d->ctx,out,",\"raw\":");pd_json(d->ctx,out,r->raw?r->raw:"");
        fz_append_string(d->ctx,out,",\"markup\":");pd_json(d->ctx,out,r->markup?r->markup:"");fz_append_byte(d->ctx,out,'}');
    }
    fz_append_string(d->ctx,out,"]}");return out;
}
