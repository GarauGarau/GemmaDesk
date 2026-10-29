#include "internal.h"
#include <ctype.h>
#include <float.h>
#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

/* Geometry is kept in MuPDF's displayed page coordinates. PDF /Rotate has
 * already been applied. Local text direction is a separate transformation. */
static int compare_lines(const void *aa,const void *bb) {
    const Line *a=aa,*b=bb;
    if(a->box.y0!=b->box.y0)return a->box.y0<b->box.y0?-1:1;
    return a->box.x0<b->box.x0?-1:a->box.x0>b->box.x0;
}
static const char *trim(const char *s) {while(isspace((unsigned char)*s))s++;return s;}
/* Estimate prose scale on this page. A long small-type appendix or table
 * must not set the paragraph spacing for the entire article. */
static float prose_size(Page *p,float fallback) {
    int hist[512]={0},upper[512]={0},best=0,top=0;
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];int bin=(int)lroundf(l->size*4);
        float y=(l->box.y0-p->box.y0)/(p->box.y1-p->box.y0);
        if(l->owner>=0 || l->heading || l->caption || l->margin || fabsf(l->dy)>.05f ||
           l->letters<20 || l->words<5 || l->math>l->n*.1f || l->bold>l->n*.5f ||
           l->box.x1-l->box.x0<(p->box.x1-p->box.x0)*.25f || y<.08f || y>.8f || bin<20 || bin>=160)continue;
        hist[bin]+=l->letters;if(hist[bin]>hist[best])best=bin;
        if(y<.55f){upper[bin]+=l->letters;if(upper[bin]>upper[top])top=bin;}
    }
    if(upper[top]>=100)return top/4.f;
    return hist[best]>=100?best/4.f:fallback;
}
static void drop_caps(Document *d,Page *p) {
    for(int i=0;i<p->nline;i++) {
        Line *cap=&p->lines[i];
        if(cap->n!=1 || cap->letters!=1 || cap->size<p->body*1.7f || fabsf(cap->dy)>.05f)continue;
        int best=-1;float top=FLT_MAX;
        for(int j=0;j<p->nline;j++) {
            Line *l=&p->lines[j];float gap=l->box.x0-cap->box.x1;
            if(i==j || l->letters<15 || fabsf(l->dy)>.05f || fabsf(l->size-p->body)>p->body*.15f ||
               gap<-p->body*.3f || gap>p->body || l->box.y0<cap->box.y0-p->body*.5f ||
               l->box.y0>cap->box.y1-p->body*.3f || !islower((unsigned char)*trim(l->text)))continue;
            if(l->box.y0<top){top=l->box.y0;best=j;}
        }
        if(best<0)continue;
        Line *l=&p->lines[best];
        l->glyph=pd_grow(d->ctx,l->glyph,&l->cap,l->n+1,sizeof(Glyph));
        memmove(l->glyph+1,l->glyph,(size_t)l->n*sizeof(Glyph));l->glyph[0]=cap->glyph[0];
        l->n++;l->letters++;l->bold+=cap->bold;l->box=rect_union(l->box,cap->box);l->dropcap=1;
        size_t bytes=strlen(l->text);char *text=fz_malloc(d->ctx,bytes+8);
        int n=fz_runetochar(text,cap->glyph[0].c);memcpy(text+n,l->text,bytes+1);
        fz_free(d->ctx,l->text);l->text=text;
        fz_free(d->ctx,cap->glyph);fz_free(d->ctx,cap->text);memset(cap,0,sizeof(*cap));
    }
}
typedef struct {PD_Rect box;int index;} RulePiece;
static int compare_rule(const void *aa,const void *bb) {
    const RulePiece *a=aa,*b=bb;
    if(a->box.y0!=b->box.y0)return a->box.y0<b->box.y0?-1:1;
    return a->box.x0<b->box.x0?-1:a->box.x0>b->box.x0;
}
static void join_rules(Document *d,Page *p) {
    if(p->complex_graphics || p->nmark<2)return;
    RulePiece *pieces=fz_calloc(d->ctx,(size_t)p->nmark,sizeof(*pieces));int n=0;
    for(int i=0;i<p->nmark;i++) {
        Mark *m=&p->marks[i];
        if(!m->image && m->box.y1-m->box.y0<p->body*.3f)pieces[n++]=(RulePiece){m->box,i};
    }
    qsort(pieces,n,sizeof(*pieces),compare_rule);
    for(int i=0;i<n;i++) {
        Mark *a=&p->marks[pieces[i].index];if(a->owner>=0)continue;
        for(int j=i+1;j<n;j++) {
            Mark *b=&p->marks[pieces[j].index];
            if(pieces[j].box.y0>a->box.y1+p->body*.08f)break;
            if(b->owner>=0 || fabsf(b->box.y0+b->box.y1-a->box.y0-a->box.y1)>p->body*.12f ||
               b->box.x0>a->box.x1+p->body*.5f || a->box.x0>b->box.x1+p->body*.5f)continue;
            a->box=rect_union(a->box,b->box);b->owner=INT_MAX;
        }
    }
    fz_free(d->ctx,pieces);n=0;
    for(int i=0;i<p->nmark;i++)if(p->marks[i].owner<0)p->marks[n++]=p->marks[i];
    p->nmark=n;
}
/* Font changes can split a visual line into independent text objects.
 * Rejoin close fragments before finding paragraphs or sorting columns.
 * Numeric table cells and two-dimensional math remain separate. */
static void line_text(Document *d,Line *a) {
    fz_buffer *text=fz_new_buffer(d->ctx,128);
    fz_try(d->ctx) {
        for(int k=0;k<a->n;k++){if(a->glyph[k].flags&G_GAP)fz_append_byte(d->ctx,text,' ');pd_utf8(d->ctx,text,a->glyph[k].c);}
        char *replacement=fz_strdup(d->ctx,fz_string_from_buffer(d->ctx,text));
        fz_free(d->ctx,a->text);a->text=replacement;
    }
    fz_always(d->ctx) fz_drop_buffer(d->ctx,text);
    fz_catch(d->ctx) fz_rethrow(d->ctx);
}
static void join_scripts(Document *d,Page *p) {
    /* Some publishers emit raised callouts as separate text objects. Insert
     * them at their actual glyph position, never as an independent paragraph.
     * Note linking still requires a unique definition and prose context. */
    for(int i=0;i<p->nline;i++) {
        Line *b=&p->lines[i];if(!b->n || b->n>3 || fabsf(b->dy)>.05f)continue;
        int valid=1;
        for(int k=0;k<b->n;k++) {int c=b->glyph[k].c;if(!(c>='0' && c<='9') && c!='*' && c!=0x2217 && c!=0x2020 && c!=0x2021)valid=0;}
        if(!valid)continue;
        int best=-1,at=0;float nearest=FLT_MAX;
        for(int j=0;j<p->nline;j++) {
            Line *a=&p->lines[j];float dy=a->baseline-b->baseline;
            if(j==i || a->letters<8 || fabsf(a->dy)>.05f || b->size>=a->size*.9f ||
               dy<a->size*.18f || dy>a->size*.9f || b->box.x0<a->box.x0 || b->box.x1>a->box.x1+a->size*.6f)continue;
            int k=0;while(k<a->n && a->glyph[k].box.x0<b->box.x0)k++;
            if(!k)continue;
            float gap=b->box.x0-a->glyph[k-1].box.x1;
            if(gap<-a->size*.15f || gap>a->size*.6f ||
               (k<a->n && a->glyph[k].box.x0<b->box.x1-a->size*.1f))continue;
            if(dy<nearest){best=j;at=k;nearest=dy;}
        }
        if(best<0)continue;
        Line *a=&p->lines[best];
        a->glyph=pd_grow(d->ctx,a->glyph,&a->cap,a->n+b->n,sizeof(Glyph));
        memmove(a->glyph+at+b->n,a->glyph+at,(size_t)(a->n-at)*sizeof(Glyph));
        memcpy(a->glyph+at,b->glyph,(size_t)b->n*sizeof(Glyph));
        a->n+=b->n;a->digits+=b->digits;a->bad+=b->bad;a->math+=b->math;a->bold+=b->bold;
        a->box=rect_union(a->box,b->box);line_text(d,a);
        fz_free(d->ctx,b->glyph);fz_free(d->ctx,b->text);memset(b,0,sizeof(*b));
    }
}
static void join_fragments(Document *d,Page *p) {
    for(int i=0;i<p->nline;i++) {
        Line *a=&p->lines[i];
        if(!a->n || fabsf(a->dy)>.05f || a->math)continue;
        for(;;) {
            int best=-1;float distance=p->body*1.8f;
            for(int j=0;j<p->nline;j++) {
                Line *b=&p->lines[j];
                if(i==j || !b->n || fabsf(b->dy)>.05f || b->math || b->box.x0<=a->box.x0)continue;
                float gap=b->box.x0-a->box.x1;
                if(gap<-.2f*p->body || gap>=distance || fabsf(a->baseline-b->baseline)>.16f*p->body)continue;
                float join_gap=a->source_block==b->source_block?1.8f:.75f;
                int prose=a->letters>0 && b->letters>0 && gap<p->body*join_gap;
                int prefix=!a->letters && a->n<12 && b->letters>3 &&
                    (b->size>p->body*1.10f || b->bold>b->n*.6f);
                if(prose || prefix){best=j;distance=gap;}
            }
            if(best<0)break;
            Line *b=&p->lines[best];
            a->glyph=pd_grow(d->ctx,a->glyph,&a->cap,a->n+b->n,sizeof(Glyph));
            if(a->glyph[a->n-1].c>32 && b->glyph[0].c>32 && distance>p->body*.08f)b->glyph[0].flags|=G_GAP;
            memcpy(a->glyph+a->n,b->glyph,(size_t)b->n*sizeof(Glyph));
            a->baseline=(a->baseline*a->n+b->baseline*b->n)/(a->n+b->n);
            if(b->n>a->n)a->size=b->size;
            a->n+=b->n;a->bad+=b->bad;a->letters+=b->letters;a->digits+=b->digits;a->bold+=b->bold;a->words+=b->words;
            a->box=rect_union(a->box,b->box);
            fz_free(d->ctx,b->glyph);fz_free(d->ctx,b->text);memset(b,0,sizeof(*b));
            line_text(d,a);
        }
    }
    int n=0;
    for(int i=0;i<p->nline;i++)if(p->lines[i].n)p->lines[n++]=p->lines[i];
    p->nline=n;
}
/* Estimate body spacing from unclaimed, body-sized prose. Caption notes and
 * plot labels must not determine the paragraph spacing of the main text.
 * Re-estimate after object ownership is known: even same-size figure notes
 * can have a different leading from the body. */
static float line_spacing(Page *p) {
    int hist[1024]={0},best=0;
    for(int i=0;i<p->nline;i++) {
        Line *a=&p->lines[i];
        if(a->owner>=0 || a->heading || a->caption || a->margin || a->letters<20 ||
           fabsf(a->dy)>.05f || a->math>a->n*.15f || fabsf(a->size-p->body)>p->body*.08f)continue;
        float nearest=p->body*3.6f;
        for(int j=0;j<p->nline;j++) {
            Line *b=&p->lines[j];float gap=b->baseline-a->baseline;
            if(b->owner>=0 || b->heading || b->caption || b->margin || b->letters<20 ||
               fabsf(b->dy)>.05f || gap<p->body*.8f || gap>=nearest || fabsf(b->size-a->size)>p->body*.08f)continue;
            float overlap=rect_x_overlap(a->box,b->box);
            if(overlap<fminf(a->box.x1-a->box.x0,b->box.x1-b->box.x0)*.5f)continue;
            nearest=gap;
        }
        if(nearest<p->body*3.5f) {
            int bin=(int)lroundf(nearest*4);
            if(bin>0 && bin<1024){hist[bin]++;if(hist[bin]>hist[best])best=bin;}
        }
    }
    return hist[best]>=3?best/4.f:p->body*1.6f;
}
static const char *caption_identifier(const char *s) {
    while(*s) {
        int c,bytes=fz_chartorune(&c,s);
        if(!isdigit((unsigned char)*s) && !(*s>='A' && *s<='Z') && *s!='.' &&
           !(c>=0xe000 && c<=0xf8ff) && c!=0xfffd)break;
        s+=bytes;
    }
    return s;
}
static int caption_type(const char *s) {
    s=trim(s);int kind=0;
    if(!strncasecmp(s,"Table",5) && isspace((unsigned char)s[5])){kind=1;s+=5;}
    else if(!strncasecmp(s,"Figure",6) && isspace((unsigned char)s[6])){kind=2;s+=6;}
    else if(!strncasecmp(s,"Fig.",4) && isspace((unsigned char)s[4])){kind=2;s+=4;}
    if(!kind)return 0;
    s=trim(s);const char *start=s;
    /* Require a caption identifier. "Table 2 shows ..." in a paragraph is
     * not a caption merely because its first word happens to be Table. */
    s=caption_identifier(s);
    int delimiter=0;if(*s)fz_chartorune(&delimiter,s);
    if(s==start || (*s && *s!=':' && delimiter!=0x2013 && delimiter!=0x2014 && !isspace((unsigned char)*s)))return 0;
    if(delimiter==0x2013 || delimiter==0x2014)return kind;
    if(*s==':' || (s>start && s[-1]=='.'))return kind;
    s=trim(s);
    if(*s && islower((unsigned char)*s))return 0;
    return kind;
}
static int heading_prefix(const char *s,const char **title,int *letter) {
    s=trim(s);const char *start=s;int level=1;
    *letter=0;*title=s;
    const char *roman=s;while(*roman && strchr("IVX",*roman))roman++;
    int rn=(int)(roman-s);
    if(rn>0 && rn<=8 && (*roman=='.' || isspace((unsigned char)*roman)) &&
       (rn>1 || *s=='I')) {*letter=2;s=roman;}
    else if(*s>='A' && *s<='Z' && (isspace((unsigned char)s[1]) || s[1]=='.')){*letter=1;s++;}
    else {
        int digits=0,value=0;
        while(isdigit((unsigned char)*s)){value=value*10+(*s++-'0');if(++digits>2)return 0;}
        if(!digits || !value)return 0; /* years and 0.309 are not section labels */
    }
    while(*s=='.') {
        s++;if(!isdigit((unsigned char)*s))break;
        level++;int n=0;
        while(isdigit((unsigned char)*s)){s++;if(++n>3)return 0;}
    }
    if(s==start || !isspace((unsigned char)*s))return 0;
    s=trim(s);if(!isalpha((unsigned char)*s))return 0;
    *title=s;return level>4?4:level;
}
static int numbered_heading(const char *s) {
    const char *title;int letter;
    return heading_prefix(s,&title,&letter);
}
static int word_heading(const char *s) {
    const char *words[]={"Abstract","References","Bibliography","Acknowledgements","Acknowledgments","Introduction","Conclusion","Conclusions","Appendix","Appendices"};
    s=trim(s);
    for(size_t i=0;i<sizeof(words)/sizeof(words[0]);i++)if(!strcasecmp(s,words[i]))return 1;
    return 0;
}
static int page_number(Line *l,Page *p) {
    if(l->n>10 || l->letters || l->math || l->bad)return 0;
    float h=p->box.y1-p->box.y0;
    if(fabsf(l->dy)>.95f) {
        float w=p->box.x1-p->box.x0;
        if(!(l->box.x1<p->box.x0+w*.1f || l->box.x0>p->box.x1-w*.1f))return 0;
    } else {
        if(fabsf(l->dy)>.05f)return 0;
        int top=l->baseline<p->box.y0+h*.2f,bottom=l->baseline>p->box.y1-h*.2f;
        if(!top && !bottom)return 0;
        /* A folio can sit below a generous crop margin. It must still be
         * outside the text, not merely in an arbitrary percentage band. */
        for(int i=0;i<p->nline;i++) {
            Line *t=&p->lines[i];if(t==l || fabsf(t->dy)>.05f || t->letters<4)continue;
            if(top && t->baseline<l->baseline-l->size*.5f)return 0;
            if(bottom && t->baseline>l->baseline+l->size*.5f)return 0;
        }
    }
    /* Only decimal folios, optionally enclosed in parentheses. Scientific
     * decimals, signed values and symbols are ordinary content. */
    const char *s=l->text;while(*s==' ')s++;
    int paren=*s=='(';if(paren)s++;
    if(!isdigit((unsigned char)*s))return 0;
    int n=0;while(isdigit((unsigned char)*s)) {
        if(n>100000)return 0;
        n=n*10+*s++-'0';
    }
    if(paren){if(*s!=')')return 0;s++;}
    while(*s==' ')s++;
    return *s?0:n;
}
static void confirm_folios(Document *d,Page *p) {
    /* Moving a marginal number out of the reading flow requires evidence
     * from another page: matching typography/height and a sequential folio.
     * Mirrored left/right footers are deliberately allowed. */
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];int n=l->margin;
        if(!n)continue;
        l->margin=0;
        float y=(l->baseline-p->box.y0)/(p->box.y1-p->box.y0);
        float x=((l->box.x0+l->box.x1)*.5f-p->box.x0)/(p->box.x1-p->box.x0);
        if(fabsf(l->dy)>.95f) {float t=x;x=((l->box.y0+l->box.y1)*.5f-p->box.y0)/(p->box.y1-p->box.y0);y=1-fminf(t,1-t);}
        int floating_matches=0;
        for(int j=0;j<d->npage && !l->margin;j++) {
            Page *q=&d->pages[j];int delta=q->number-p->number;
            if(!delta || abs(delta)>4)continue;
            for(int k=0;k<q->nline;k++) {
                Line *t=&q->lines[k];
                float ty=(t->baseline-q->box.y0)/(q->box.y1-q->box.y0);
                float size=fmaxf(t->size,l->size);
                if(page_number(t,q)!=n+delta || n+delta<=0 || fabsf(t->size-l->size)>size*.1f)continue;
                float tx=((t->box.x0+t->box.x1)*.5f-q->box.x0)/(q->box.x1-q->box.x0);
                if(fabsf(t->dy)>.95f) {float v=tx;tx=((t->box.y0+t->box.y1)*.5f-q->box.y0)/(q->box.y1-q->box.y0);ty=1-fminf(v,1-v);}
                if(fabsf(ty-y)<.004f) {l->margin=1;break;}
                /* Float-only pages may place the footer closer to their
                 * content. Two neighbouring pages must confirm the same
                 * numbering offset, font and horizontal position. */
                float lx=(l->box.x0+l->box.x1)*.5f,tx_abs=(t->box.x0+t->box.x1)*.5f;
                int horizontal=fabsf(tx-x)<.01f || fabsf((lx-p->box.x0)-(tx_abs-q->box.x0))<size*1.2f ||
                    fabsf((p->box.x1-lx)-(q->box.x1-tx_abs))<size*1.2f;
                if(((y>.5f)==(ty>.5f) && horizontal) || (p->number==0 && y>.8f)) {
                    if(++floating_matches>=2)l->margin=1;
                    break;
                }
            }
        }
    }
}
static int appendix_title(const char *s);
static int references_title(const char *s);
static int caption_label_only(Line *l) {
    if(!l->caption)return 0;
    const char *s=l->text;while(*s && !isspace((unsigned char)*s))s++;
    s=caption_identifier(trim(s));
    if(*s==':')s++;
    return !*trim(s);
}
/* Use baseline distances in the same column. The old absolute glyph-box
 * gap mistook ordinary double spacing for a section break. Typography is
 * independent evidence: a decimal at the start of prose is not sufficient. */
static void headings(Page *p) {
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];l->caption=caption_type(l->text);l->margin=page_number(l,p);
        if(l->front){l->heading=l->front==1?1:0;continue;}
        if(l->caption || l->margin || l->letters<3 || l->words>24 || l->n>190 || fabsf(l->dy)>.05f)continue;
        float before=FLT_MAX,after=FLT_MAX;Line *previous=NULL;
        for(int j=0;j<p->nline;j++) {
            Line *t=&p->lines[j];
            if(i==j || fabsf(t->dy)>.05f || t->letters<3 || rect_x_overlap(l->box,t->box)<=0)continue;
            float delta=l->baseline-t->baseline;
            if(delta>p->body*.5f && delta<before){before=delta;previous=t;}
            if(delta<-p->body*.5f)after=fminf(after,-delta);
        }
        const char *title;int letter,level=heading_prefix(l->text,&title,&letter);
        int salient=l->bold>l->n*.6f || l->size>p->body*1.10f;
        /* A page dominated by a small caption need not have small body
         * text. Compare vertical gaps in this line's scale as well. */
        float leading=fmaxf(p->leading,l->size*1.12f);
        int isolated=before>leading*1.3f && after>leading*1.15f;
        int known=word_heading(trim(l->text)) || references_title(l->text) || appendix_title(l->text) || (level && word_heading(title));
        size_t previous_length=previous?strlen(previous->text):0;
        while(previous_length && isspace((unsigned char)previous->text[previous_length-1]))previous_length--;
        int unfinished=previous_length && !strchr(".!?:;",previous->text[previous_length-1]);
        int continuing=previous && previous->words>=4 &&
            fabsf(previous->size-l->size)<l->size*.08f &&
            (previous->bold>previous->n*.6f)==(l->bold>l->n*.6f) &&
            ((previous->source_block==l->source_block && before<leading*1.25f) ||
             (known && unfinished && before<l->size*2.8f));
        if(continuing && !level && l->bold<l->n*.5f)continue;
        /* A mathematical alphabet is not a section label. Body sentences
         * remain prose even when an entire line happens to be bold. */
        if(l->bad || l->math>fmaxf(2,l->letters*.25f))continue;
        size_t length=strlen(l->text);
        int sentence=length && strchr(".;,",l->text[length-1]) && l->words>8;
        if(sentence && !known)continue;
        /* A leading letter is also a word in ordinary prose ("A figure",
         * "I estimate"). A small size difference cannot establish a
         * lettered section: require a title initial or independent bold
         * typography. Body size can be underestimated on note-heavy pages. */
        int label=level && (isupper((unsigned char)*title) || known || l->bold>l->n*.6f);
        int numbered=label && (salient || (isolated && isupper((unsigned char)*title) && l->words<13));
        int plain=l->bold>l->n*.65f && l->size>=p->body*.96f && before>p->leading*1.1f;
        int large=l->size>p->body*1.16f && before>p->leading*1.1f &&
            (l->bold>l->n*.5f || (p->number==0 && l->size>p->body*1.4f));
        if((known && (isolated || (salient && before>leading*1.1f))) || numbered || plain || large)
            l->heading=level?level:l->size>p->body*1.3f?1:2;
        if(l->front)l->heading=l->front==1?1:0;
    }
    /* Journal captions often put "Table 1" and its bold title on separate
     * lines. The title is a caption continuation, not a section barrier. */
    for(int i=0;i<p->nline;i++) {
        Line *cap=&p->lines[i];if(!caption_label_only(cap))continue;
        float bottom=cap->box.y1;
        for(int pass=0;pass<3;pass++) {
            Line *next=NULL;float distance=cap->size*1.5f;
            for(int j=i+1;j<p->nline;j++) {
                Line *l=&p->lines[j];float gap=l->box.y0-bottom;
                if(gap<0 || gap>=distance || l->caption || l->margin || l->front ||
                   l->words>16 || l->letters<3 || fabsf(l->dy)>.05f ||
                   fabsf(l->size-cap->size)>cap->size*.25f ||
                   rect_x_overlap(cap->box,l->box)<=0 || numbered_heading(l->text))continue;
                int rule=0;
                for(int k=0;k<p->nmark;k++) {
                    PD_Rect r=p->marks[k].box;
                    if(r.y0>=bottom && r.y1<=l->box.y0 && r.x1-r.x0>p->body*5 &&
                       r.y1-r.y0<p->body*.3f && rect_x_overlap(r,l->box)>0)rule=1;
                }
                if(!rule){next=l;distance=gap;}
            }
            if(!next)break;
            next->heading=0;next->prose=-1;bottom=next->box.y1;
        }
    }
}
Block *pd_add_block(Document *d,Page *p,PD_Kind kind,PD_Rect box,const char *why) {
    p->blocks=pd_grow(d->ctx,p->blocks,&p->cblock,p->nblock+1,sizeof(Block));
    Block *b=&p->blocks[p->nblock++];memset(b,0,sizeof(*b));
    b->pub.kind=kind;b->pub.page=p->number;b->pub.box=box;
    snprintf(b->pub.reason,sizeof(b->pub.reason),"%s",why);
    return b;
}
void pd_claim_line(Document *d,Page *p,Block *b,int i) {
    Line *l=&p->lines[i];
    if(l->owner>=0)return;
    b->lines=pd_grow(d->ctx,b->lines,&b->cap,b->n+1,sizeof(int));
    b->lines[b->n++]=i;l->owner=(int)(b-p->blocks);
    if(b->n==1)b->pub.front=l->front;
    b->pub.box=rect_union(b->pub.box,l->box);b->pub.glyphs+=l->n;
    if(b->pub.kind==PD_HEADING && l->heading && !b->pub.level)b->pub.level=l->heading;
    if(!b->pub.label[0] && l->text) {
        size_t n=strlen(l->text);
        if(n>=sizeof(b->pub.label)) {
            n=sizeof(b->pub.label)-5;
            while(n>0 && (((unsigned char)l->text[n]&0xc0)==0x80))n--;
        }
        memcpy(b->pub.label,l->text,n);b->pub.label[n]=0;
    }
}
static void claim_area(Document *d,Page *p,Block *b,int include_marks) {
    /* Grow to whole intersecting lines: a region must not clip an exponent
     * merely because its initial geometric seed covered most of a word. */
    for(int pass=0;pass<5;pass++) {
        int changed=0;
        for(int i=0;i<p->nline;i++) {
            Line *l=&p->lines[i];
            if(l->owner<0 && rect_overlap(b->pub.box,l->box)) {
                pd_claim_line(d,p,b,i);changed=1;
            }
        }
        if(include_marks)for(int i=0;i<p->nmark;i++) {
            Mark *m=&p->marks[i];
            if(m->owner<0 && rect_overlap(rect_pad(b->pub.box,1.5f),m->box)) {
                m->owner=(int)(b-p->blocks);
                b->pub.box=rect_union(b->pub.box,m->box);changed=1;
            }
        }
        if(!changed)break;
    }
}
static void all_graphic(Document *d,Page *p,const char *why) {
    Block *b=pd_add_block(d,p,PD_PAGE_IMAGE,p->box,why);
    b->pub.review=1;
    for(int i=0;i<p->nline;i++)pd_claim_line(d,p,b,i);
    for(int i=0;i<p->nmark;i++)p->marks[i].owner=0;
    snprintf(b->pub.label,sizeof(b->pub.label),"Page %d",p->number+1);
}
static int orientation(Line *l) {
    float degrees=atan2f(l->dy,l->dx)*180.0f/3.14159265358979323846f;
    int quadrant=(int)lroundf(degrees/90.0f);
    return ((quadrant%4)+4)%4;
}
static void rotated_regions(Document *d,Page *p) {
    for(int q=1;q<4;q++) {
        int count=0,digits=0,caption=0;PD_Rect area={FLT_MAX,FLT_MAX,-FLT_MAX,-FLT_MAX};
        for(int i=0;i<p->nline;i++) {
            Line *l=&p->lines[i];
            if(l->owner<0 && !l->margin && orientation(l)==q && l->n>0) {
                area=rect_union(area,l->box);count+=l->n;digits+=l->digits;
                if(l->caption==1)caption=1;
            }
        }
        if(count<4)continue;
        Block *b=pd_add_block(d,p,caption || digits>count*.12f?PD_TABLE:PD_FIGURE,
                             rect_pad(area,2.5f),"Local text direction; original region rotated upright");
        b->pub.rotation=(360-q*90)%360;
        /* Small rotated labels in an otherwise ordinary page need review. */
        b->pub.review=1;
        claim_area(d,p,b,1);
    }
}
static int numeric_row(Line *l) {
    /* Digits embedded in a word or identifier are not numeric cells.
     * Require an actual numeric token, preserving decimal/scientific
     * notation, parentheses and significance markers as printed. */
    int numbers=0,digits=0,bad=0;
    for(const char *s=l->text;;) {
        int c=0;if(*s)s+=fz_chartorune(&c,s);
        if(c<=32) {
            if(digits && !bad)numbers++;
            digits=bad=0;if(!c)break;
        } else if(c>='0' && c<='9')digits++;
        else if(c!=0x2212 && (c>=128 || !strchr("+-.,()[]*/%eE",c)))bad=1;
    }
    return numbers>0 && (l->digits>=2 || !l->letters);
}
static int inside(PD_Rect a,PD_Rect b,float tolerance) {
    return a.x0>=b.x0-tolerance && a.y0>=b.y0-tolerance &&
           a.x1<=b.x1+tolerance && a.y1<=b.y1+tolerance;
}
static int note_start(const char *s) {
    s=trim(s);
    return !strncasecmp(s,"Notes:",6) || !strncasecmp(s,"Note:",5) ||
           !strncasecmp(s,"Notes.",6) || !strncasecmp(s,"Note.",5) ||
           !strncasecmp(s,"Source:",7) || !strncasecmp(s,"Sources:",8);
}
/* Captions, prose and graphical labels have different roles. Recognising
 * prose first makes it a barrier to region expansion (PDFFigures 2.0), not
 * something to be swallowed by a growing drawing bounding box. */
static void prose_regions(Page *p) {
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];
        if(l->prose==-1)continue;
        l->prose=!l->caption && !l->heading && !l->margin && fabsf(l->dy)<.05f &&
            l->size>=p->body*.94f && l->letters>25 && l->words>=6 && l->math<l->n*.1f;
        if(l->prose)for(int j=0;j<p->nmark;j++) {
            Mark *m=&p->marks[j];
            if(rect_area(m->box)<rect_area(p->box)*.8f && inside(l->box,m->box,1)) {l->prose=0;break;}
        }
    }
    /* Caption continuation and table/figure notes may themselves be long
     * prose lines, including at the body font size. Track these explicitly. */
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];
        if(!l->caption && !note_start(l->text))continue;
        l->prose=-1;Line *last=l;
        for(int j=i+1;j<p->nline;j++) {
            Line *t=&p->lines[j];
            if(t->box.y0-last->box.y1>l->size)break;
            if(t->caption || t->heading || t->margin || fabsf(t->dy)>.05f)continue;
            float delta=t->baseline-last->baseline;
            if(delta<l->size*.7f || delta>l->size*1.65f || fabsf(t->size-l->size)>.4f)continue;
            if(fabsf(t->box.x0-l->box.x0)>p->body*2 || rect_x_overlap(last->box,t->box)<=0)continue;
            t->prose=-1;last=t;
        }
    }
    /* Short final lines inherit a neighbouring prose role, but do not
     * propagate through notes, captions or a font-size change. */
    for(int pass=0;pass<2;pass++)for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];
        if(l->prose || l->caption || l->heading || l->margin || l->letters<8 || fabsf(l->dy)>.05f)continue;
        for(int j=0;j<p->nline;j++) {
            Line *t=&p->lines[j];float gap=l->baseline-t->baseline;
            if(t->prose!=1 || gap<p->body*.8f || gap>p->leading*1.25f || fabsf(l->size-t->size)>.4f)continue;
            if(fabsf(l->box.x0-t->box.x0)<p->body*2 && rect_x_overlap(l->box,t->box)>0){l->prose=1;break;}
        }
    }
}
typedef struct { PD_Rect extent, region; float score; int direction; } Proposal;
static PD_Rect caption_box(Page *p,int index) {
    Line *cap=&p->lines[index];PD_Rect box=cap->box;float base=cap->baseline;
    for(int j=index+1;j<p->nline;j++) {
        Line *l=&p->lines[j];
        if(l->box.y0>box.y1+cap->size)break;
        if(l->owner>=0 || l->caption || l->prose!=-1 || note_start(l->text) ||
           fabsf(l->size-cap->size)>.4f || l->baseline<base+cap->size*.7f)continue;
        if(l->baseline-base>cap->size*1.65f || fabsf(l->box.x0-cap->box.x0)>p->body*2)continue;
        box=rect_union(box,l->box);base=l->baseline;
    }
    return box;
}
static Proposal object_proposal(Page *p,int index,PD_Rect caption,int direction,const PD_Rect *grid) {
    Proposal out={0};out.direction=direction;PD_Rect zone=grid?*grid:p->box;
    float middle=(p->box.x0+p->box.x1)/2;int left=0,right=0;
    for(int i=0;i<p->nline;i++)if(p->lines[i].prose==1) {
        if(p->lines[i].box.x1<middle)left++;
        if(p->lines[i].box.x0>middle)right++;
    }
    /* Two independent captions on the same row are also evidence of a
     * column boundary, even on a page dominated by drawings or small text. */
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];
        if(i==index || !l->caption || fabsf(l->dy)>.05f ||
           fabsf(l->baseline-p->lines[index].baseline)>p->body)continue;
        if((caption.x1<middle && l->box.x0>middle) || (caption.x0>middle && l->box.x1<middle))left=right=3;
    }
    if(!grid && left>=3 && right>=3) {
        if(caption.x1<middle)zone.x1=middle;
        else if(caption.x0>middle)zone.x0=middle;
    }
    if(direction>0)zone.y0=caption.y1+.1f;else zone.y1=caption.y0-.1f;
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];if(i==index || inside(l->box,caption,.1f) || rect_x_overlap(zone,l->box)<=0)continue;
        int cell_heading=grid && l->heading && l->size<=p->lines[index].size*1.12f && !word_heading(trim(l->text));
        if((!l->heading || cell_heading) && !l->caption && !l->margin && (grid || l->prose!=1))continue;
        if(direction>0 && l->box.y0>=caption.y1)zone.y1=fminf(zone.y1,l->box.y0-1);
        if(direction<0 && l->box.y1<=caption.y0)zone.y0=fmaxf(zone.y0,l->box.y1+1);
    }
    if(zone.y1<=zone.y0)return out;
    if(grid && zone.y1<grid->y1-.5f)return out;
    PD_Rect extent={FLT_MAX,FLT_MAX,-FLT_MAX,-FLT_MAX};
    int marks=0,numbers=0,rows=0,rules=0,aligned=0;
    float last_y=-FLT_MAX,ink=0,first_column=FLT_MAX,last_column=-FLT_MAX;
    for(int i=0;i<p->nmark;i++) {
        Mark *m=&p->marks[i];if(m->owner>=0 || !inside(m->box,zone,.5f))continue;
        float width=m->box.x1-m->box.x0,height=m->box.y1-m->box.y0;
        if(rect_area(m->box)>rect_area(p->box)*.9f)continue;
        extent=rect_union(extent,m->box);ink+=fminf(rect_area(m->box),p->body*p->body*20);
        if(m->image || (width>p->body && height>p->body))marks++;
        if(!m->image && width>p->body*8 && height<p->body*.25f)rules++;
    }
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];if(l->owner>=0 || l->margin || !inside(l->box,zone,.5f))continue;
        extent=rect_union(extent,l->box);
        if(numeric_row(l)) {
            numbers++;
            if(l->box.y0-last_y>p->body*.6f){rows++;last_y=l->box.y0;}
        }
        int cell_words=grid?24:6;
        if(l->words<=cell_words && fabsf(l->dy)<.05f && l->prose!=-1)for(int j=0;j<i;j++) {
            Line *t=&p->lines[j];float gap=l->baseline-t->baseline;
            if(t->owner>=0 || t->words>cell_words || t->prose==-1 || gap<p->body*.6f || gap>p->leading*6 ||
               !inside(t->box,zone,.5f) || fabsf(t->dy)>.05f)continue;
            if(fminf(fabsf(l->box.x0-t->box.x0),fabsf(l->box.x1-t->box.x1))<p->body*.45f) {
                aligned++;first_column=fminf(first_column,l->box.x0);last_column=fmaxf(last_column,l->box.x0);break;
            }
        }
    }
    int kind=p->lines[index].caption;
    int tabular=(numbers>=2 && rows>=2 && aligned>=2) ||
        (aligned>=4 && last_column-first_column>p->body*3 && (rules>=2 || aligned>=6));
    if((kind==1 && !tabular) || (kind==2 && !marks && ink<p->body*p->body*4))return out;
    float distance=direction>0?extent.y0-caption.y1:caption.y0-extent.y1;
    if(distance>p->body*8 || rect_area(extent)<p->body*p->body*4)return out;
    out.region=zone;out.extent=extent;
    out.score=(sqrtf(rect_area(extent))+fminf(ink/(p->body*p->body),30)+rows*p->body)/
              (1+fmaxf(0,distance)/(p->body*3));
    return out;
}
static Proposal ruled_table_proposal(Page *p,int index,PD_Rect caption) {
    Proposal best={0};
    /* Long explanatory captions and word-heavy cells can look like prose.
     * Matching top/bottom rules plus repeated columns provide independent
     * evidence for a table; headings, other captions and folios still stop it. */
    for(int i=0;i<p->nmark;i++) {
        Mark *top=&p->marks[i];PD_Rect a=top->box;
        if(top->owner>=0 || top->image || a.y0<caption.y1 || a.y0-caption.y1>p->body*14 ||
           a.x1-a.x0<p->body*12 || a.y1-a.y0>p->body*.3f)continue;
        for(int j=0;j<p->nmark;j++) {
            Mark *bottom=&p->marks[j];PD_Rect b=bottom->box;
            if(bottom->owner>=0 || bottom->image || b.y0-a.y1<p->body*2 ||
               b.y1-b.y0>p->body*.3f || fabsf(a.x0-b.x0)>p->body || fabsf(a.x1-b.x1)>p->body)continue;
            PD_Rect grid={fminf(a.x0,b.x0)-1,caption.y1+.1f,fmaxf(a.x1,b.x1)+1,b.y1+.5f};
            if(rect_x_overlap(grid,caption)<=0)continue;
            Proposal candidate=object_proposal(p,index,caption,1,&grid);
            if(candidate.score>best.score)best=candidate;
        }
    }
    return best;
}
static void caption_regions(Document *d,Page *p) {
    prose_regions(p);
    for(int i=0;i<p->nline;i++) {
        Line *cap=&p->lines[i];
        if(cap->owner>=0 || !cap->caption || fabsf(cap->dy)>.05f)continue;
        PD_Rect caption=caption_box(p,i);
        Proposal above=object_proposal(p,i,caption,-1,NULL),below=object_proposal(p,i,caption,1,NULL);
        Proposal best=above.score>below.score?above:below;
        int ruled=0;
        if(cap->caption==1) {
            Proposal framed=ruled_table_proposal(p,i,caption);
            if(framed.score>best.score){best=framed;ruled=1;}
        }
        if(best.score<=0)continue;
        Block *b=pd_add_block(d,p,cap->caption==1?PD_TABLE:PD_FIGURE,caption,
            ruled?"Caption and coextensive table rules with repeated column evidence":
            "Caption, graphic primitives and text roles; prose and other captions bound the region");
        snprintf(b->pub.label,sizeof(b->pub.label),"%s",cap->text);b->pub.review=1;
        for(int j=0;j<p->nline;j++) {
            Line *l=&p->lines[j];
            if(l->owner<0 && (inside(l->box,caption,.2f) || (!l->margin && inside(l->box,best.region,.5f))))
                pd_claim_line(d,p,b,j);
        }
        for(int j=0;j<p->nmark;j++) {
            Mark *m=&p->marks[j];
            if(m->owner<0 && inside(m->box,best.region,.5f)) {
                m->owner=(int)(b-p->blocks);b->pub.box=rect_union(b->pub.box,m->box);
            }
        }
        /* Notes follow the complete object whether its caption is above or
         * below it. Require an explicit note label, then its text-role and
         * geometry continuity; a neighbouring body paragraph is a boundary. */
        int in_notes=0;
        for(int j=i+1;j<p->nline;j++) {
            Line *l=&p->lines[j];
            if(l->box.y0>b->pub.box.y1+p->body*2)break;
            if(l->owner>=0 || l->box.y0<b->pub.box.y1-.5f ||
               rect_x_overlap(l->box,b->pub.box)<=0 || l->margin)continue;
            if(l->prose!=-1 || l->caption || l->heading || (!in_notes && !note_start(l->text)))break;
            pd_claim_line(d,p,b,j);in_notes=1;
        }
        b->pub.box=rect_pad(b->pub.box,1.5f);
    }
}
static void contents_regions(Document *d,Page *p) {
    int title=0,leaders=0,numbers=0;float width=p->box.x1-p->box.x0;
    PD_Rect box={FLT_MAX,FLT_MAX,-FLT_MAX,-FLT_MAX};
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];if(l->owner>=0 || l->margin || !l->n)continue;
        if(!strcasecmp(trim(l->text),"Contents") || !strcasecmp(trim(l->text),"Table of Contents"))title=1;
        int dots=0;for(const char *s=l->text;*s;s++)dots+=*s=='.';
        if(dots>=5)leaders++;
        if(!l->letters && l->digits && l->n<8 && l->box.x0>p->box.x0+width*.66f)numbers++;
        box=rect_union(box,l->box);
    }
    if(numbers<8 || leaders<5 || (!title && leaders<10))return;
    Block *b=pd_add_block(d,p,PD_TABLE,rect_pad(box,1.5f),
        "Printed contents: repeated leaders and aligned page references; original hierarchy retained");
    snprintf(b->pub.label,sizeof(b->pub.label),"Contents");b->pub.review=1;claim_area(d,p,b,1);
}
static void continued_tables(Document *d,Page *p) {
    /* An uncaptioned continuation can repeat a ruled column header. Require
     * that header plus repeated numeric columns, rather than treating every
     * multi-column page as a table. Each source page remains one whole part. */
    float width=p->box.x1-p->box.x0,height=p->box.y1-p->box.y0;
    for(int i=0;i<p->nline;i++) {
        Line *head=&p->lines[i];
        if(head->owner>=0 || head->margin || head->caption || head->letters<3 || head->words>6 ||
           fabsf(head->dy)>.05f || head->box.y0>p->box.y0+height*.3f)continue;
        PD_Rect header=head->box;int cells=0;
        for(int j=0;j<p->nline;j++) {
            Line *l=&p->lines[j];
            if(l->owner<0 && !l->margin && !l->caption && l->letters>=3 && l->words<=6 &&
               fabsf(l->baseline-head->baseline)<head->size*.3f) {header=rect_union(header,l->box);cells++;}
        }
        if(cells<4 || header.x1-header.x0<width*.65f)continue;
        int above=0,below=0;PD_Rect area=header;
        for(int j=0;j<p->nmark;j++) {
            Mark *m=&p->marks[j];
            if(m->owner>=0 || m->image || m->box.y1-m->box.y0>head->size*.3f ||
               m->box.x1-m->box.x0<(header.x1-header.x0)*.9f ||
               rect_x_overlap(m->box,header)<(header.x1-header.x0)*.9f)continue;
            if(m->box.y0<=header.y0 && header.y0-m->box.y1<head->size) {above++;area=rect_union(area,m->box);}
            if(m->box.y1>=head->baseline && m->box.y0-header.y1<head->size && m->box.y0>header.y0)below++;
        }
        if(!above || !below)continue;
        float end=p->box.y1;
        for(int j=0;j<p->nline;j++) {
            Line *l=&p->lines[j];if(l->owner>=0 || l->margin || l->box.y0<header.y1 || rect_x_overlap(l->box,header)<=0)continue;
            int prose=l->words>14 && l->box.x1-l->box.x0>(header.x1-header.x0)*.7f;
            if(l->caption || note_start(l->text) || prose || (l->heading && l->size>head->size*1.2f))end=fminf(end,l->box.y0);
        }
        int rows=0,in_row=0,bins[128]={0};float baseline=-FLT_MAX,last=header.y1;
        for(int j=0;j<p->nline;j++) {
            Line *l=&p->lines[j];
            if(l->owner>=0 || l->margin || l->box.y0<header.y0-.1f || l->box.y0>=end ||
               l->box.x0<header.x0-head->size || l->box.x1>header.x1+head->size)continue;
            if(l->box.y0-last>head->size*3){end=last+.5f;break;}
            last=fmaxf(last,l->box.y1);area=rect_union(area,l->box);
            if(l->words<=2 && l->digits && !l->letters) {
                if(fabsf(l->baseline-baseline)>head->size*.35f) {rows+=in_row>=2;in_row=0;baseline=l->baseline;}
                in_row++;int bin=(int)((l->box.x0-p->box.x0)/width*128);
                if(bin>=0 && bin<128)bins[bin]++;
            }
        }
        rows+=in_row>=2;int columns=0;for(int j=0;j<128;j++)columns+=bins[j]>=3;
        if(rows<3 || columns<2)continue;
        for(int j=0;j<p->nmark;j++) {
            Mark *m=&p->marks[j];
            if(m->owner<0 && m->box.y0>=header.y0-head->size && m->box.y1<=end &&
               m->box.x0>=area.x0-head->size && m->box.x1<=area.x1+head->size)area=rect_union(area,m->box);
        }
        int notes=0;
        for(int j=0;j<p->nline;j++) {
            Line *l=&p->lines[j];if(l->owner>=0 || l->margin || l->box.y0<area.y1-.5f || rect_x_overlap(l->box,area)<=0)continue;
            if(l->box.y0>area.y1+head->size*2 || l->prose!=-1 || l->caption || (!notes && !note_start(l->text)))break;
            area=rect_union(area,l->box);notes=1;
        }
        Block *b=pd_add_block(d,p,PD_TABLE,rect_pad(area,1.5f),
            "Uncaptioned ruled header with repeated numeric rows and columns; complete source table part retained");
        b->pub.review=1;claim_area(d,p,b,1);
    }
}
static void table_regions(Document *d,Page *p) {
    /* Uncaptioned tables: repeated separate cells on the same baselines,
     * with numeric evidence. Group adjacent candidate rows by their gap.
     * This is a geometric candidate detector, not a trained Table Transformer. */
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];if(l->owner>=0 || l->margin || l->heading)continue;
        int row_lines=0,num=0,rows=0,last_row=-1;float y=l->baseline;
        PD_Rect area=l->box;int end=i;
        for(int j=i;j<p->nline;j++) {
            Line *t=&p->lines[j];if(t->owner>=0 || t->margin)continue;
            if(t->box.y0-area.y1>p->body*1.8f)break;
            if(t->heading || t->caption || (t->words>14 && t->letters>40))break;
            if(fabsf(t->baseline-y)>p->body*.55f) {
                if(row_lines>=3 && num>=2){rows++;last_row=j-1;}
                else if(rows)break;
                row_lines=0;num=0;y=t->baseline;
            }
            row_lines++;if(numeric_row(t))num++;
            area=rect_union(area,t->box);end=j;
        }
        if(row_lines>=3 && num>=2){rows++;last_row=end;}
        if(rows>=3 && last_row>=i) {
            area=l->box;
            for(int j=i;j<=last_row;j++)if(p->lines[j].owner<0)area=rect_union(area,p->lines[j].box);
            Block *b=pd_add_block(d,p,PD_TABLE,rect_pad(area,2.5f),"Repeated cell baselines and numeric columns; check table boundaries");
            b->pub.review=1;claim_area(d,p,b,1);
        }
    }
}
static float rect_distance(PD_Rect a,PD_Rect b) {
    float x=fmaxf(0,fmaxf(a.x0-b.x1,b.x0-a.x1));
    float y=fmaxf(0,fmaxf(a.y0-b.y1,b.y0-a.y1));
    return hypotf(x,y);
}
static void text_decorations(Page *p) {
    for(int i=0;i<p->nmark;i++) {
        Mark *m=&p->marks[i];
        if(m->owner>=0 || m->image || m->box.y1-m->box.y0>p->body*.15f)continue;
        for(int j=0;j<p->nline;j++) {
            Line *l=&p->lines[j];
            if(l->owner>=0 || l->math || fabsf(l->dy)>.05f || l->n<1 || l->size<p->body*.8f)continue;
            float delta=m->box.y0-l->baseline;
            if(delta<-.1f || delta>l->size*.3f || rect_x_overlap(l->box,m->box)<=0)continue;
            int count=0;
            for(int k=0;k<l->n;k++)if(rect_x_overlap(l->glyph[k].box,m->box)>0){l->glyph[k].flags|=G_UNDERLINE;count++;}
            if(count)m->owner=INT_MAX;
        }
    }
}
static void figures(Document *d,Page *p) {
    /* Grow graphic regions by proximity, then attach intersecting labels.
     * This conservative bounding-box grouping can merge neighbouring panels;
     * it does not claim to reproduce PDFFigures' evaluation results. */
    int n=p->nmark;
    for(int i=0;i<n;i++) {
        Mark *seed=&p->marks[i];if(seed->owner>=0)continue;
        float w=seed->box.x1-seed->box.x0,h=seed->box.y1-seed->box.y0;
        if(!seed->image && (w<2 || h<2))continue; /* rules handled after math */
        PD_Rect area=seed->box;
        if(rect_area(area)>.92f*rect_area(p->box))continue;
        Block *b=pd_add_block(d,p,PD_FIGURE,area,"Connected graphics and labels; original appearance retained");
        seed->owner=(int)(b-p->blocks);
        for(int pass=0;pass<6;pass++) {
            int changed=0;
            for(int j=0;j<n;j++) {
                Mark *m=&p->marks[j];
                if(m->owner<0 && rect_distance(b->pub.box,m->box)<p->body*.9f &&
                   rect_area(m->box)<rect_area(p->box)*.92f) {
                    b->pub.box=rect_union(b->pub.box,m->box);m->owner=(int)(b-p->blocks);changed=1;
                }
            }
            if(!changed)break;
        }
        PD_Rect graphics=b->pub.box;
        for(int j=0;j<p->nline;j++) {
            Line *l=&p->lines[j];
            if(l->owner>=0 || l->margin)continue;
            float overlap=rect_x_overlap(l->box,graphics);
            if(rect_overlap(rect_pad(graphics,p->body*.7f),l->box) ||
               (l->caption==2 && overlap>0 && rect_distance(l->box,graphics)<p->body*4))
                pd_claim_line(d,p,b,j);
        }
        b->pub.box=rect_pad(b->pub.box,2);
        claim_area(d,p,b,1);
        b->pub.review=1;
    }
    /* Captions without a connected graphics seed remain ordinary text.
     * They are not discarded or fabricated into an empty image. */
}
static int math_root(int *parent,int i) {
    while(parent[i]!=i){parent[i]=parent[parent[i]];i=parent[i];}return i;
}
static void compact_blocks(Page *p) {
    int next=0;
    for(int i=0;i<p->nblock;i++)if(p->blocks[i].pub.kind!=(PD_Kind)-1) {
        if(i!=next)p->blocks[next]=p->blocks[i];
        for(int j=0;j<p->blocks[next].n;j++)p->lines[p->blocks[next].lines[j]].owner=next;
        for(int j=0;j<p->nmark;j++)if(p->marks[j].owner==i)p->marks[j].owner=next;
        next++;
    }
    p->nblock=next;
}
static const Glyph *single_delimiter(Page *p,Block *b) {
    if(b->pub.kind!=PD_TEXT)return NULL;
    const Glyph *result=NULL;
    for(int i=0;i<b->n;i++) {
        Line *l=&p->lines[b->lines[i]];
        if(l->caption || l->heading || l->margin)return NULL;
        for(int j=0;j<l->n;j++)if(!pd_math_space(l->glyph[j].c)) {
            if(result)return NULL;
            result=&l->glyph[j];
        }
    }
    return result;
}
/* A PDF can place the two tall matrix fences in separate text objects.
 * Reunite only complete isolated delimiters around one equation, only when
 * the enclosed source produces a verified matrix tree. No prose is claimed. */
static void matrix_regions(Document *d,Page *p) {
    for(int bi=0;bi<p->nblock;bi++) {
        Block *b=&p->blocks[bi];if(b->pub.kind!=PD_EQUATION)continue;
        for(int li=0;li<p->nblock;li++) {
            const Glyph *left=single_delimiter(p,&p->blocks[li]);
            if(!left || (left->c!='[' && left->c!='(' && left->c!='{') || left->box.x1>b->pub.box.x0)continue;
            int close=left->c=='['?']':left->c=='('?')':'}';
            for(int ri=0;ri<p->nblock;ri++) {
                const Glyph *right=single_delimiter(p,&p->blocks[ri]);
                if(!right || right->c!=close || right->box.x0<b->pub.box.x1 ||
                   left->box.y1<b->pub.box.y0 || left->box.y0>b->pub.box.y1 ||
                   right->box.y1<b->pub.box.y0 || right->box.y0>b->pub.box.y1)continue;
                PD_Rect box=rect_union(b->pub.box,rect_union(left->box,right->box));int safe=1;
                if(pd_math_box_edited(d,p,box))continue;
                for(int i=0;i<p->nblock;i++)if(i!=bi && i!=li && i!=ri && p->blocks[i].pub.kind!=(PD_Kind)-1 &&
                    rect_overlap(box,p->blocks[i].pub.box))safe=0;
                if(!safe)continue;
                Reconstruction r={.task=2,.line=-1,.display=1,.box=box};
                int accepted=pd_source_math(d,p,&r) && strstr(r.markup,"<mtable>");
                fz_free(d->ctx,r.raw);fz_free(d->ctx,r.markup);if(!accepted)continue;
                int fences[2]={li,ri};
                for(int f=0;f<2;f++) {
                    Block *from=&p->blocks[fences[f]];
                    for(int j=0;j<from->n;j++){p->lines[from->lines[j]].owner=-1;pd_claim_line(d,p,b,from->lines[j]);}
                    fz_free(d->ctx,from->lines);memset(from,0,sizeof(*from));from->pub.kind=(PD_Kind)-1;
                }
                snprintf(b->pub.reason,sizeof(b->pub.reason),"Paired source delimiters and verified matrix cells");
                break;
            }
            if(p->blocks[li].pub.kind==(PD_Kind)-1)break;
        }
    }
    /* Tall fences may instead already belong to an equation whose rectangle
     * overlaps a separately owned matrix row. Merge complete blocks, never
     * render the row twice by borrowing its glyphs during reconstruction. */
    for(int i=0;i<p->nblock;i++)if(p->blocks[i].pub.kind==PD_EQUATION)
        for(int j=i+1;j<p->nblock;j++)if(p->blocks[j].pub.kind==PD_EQUATION &&
            rect_overlap(p->blocks[i].pub.box,p->blocks[j].pub.box)) {
            PD_Rect box=rect_union(p->blocks[i].pub.box,p->blocks[j].pub.box);int safe=1;
            if(pd_math_box_edited(d,p,box))continue;
            for(int k=0;k<p->nblock;k++)if(k!=i && k!=j && p->blocks[k].pub.kind!=(PD_Kind)-1 &&
                rect_overlap(box,p->blocks[k].pub.box))safe=0;
            if(!safe)continue;
            Reconstruction r={.task=2,.line=-1,.display=1,.box=box};
            int accepted=pd_source_math(d,p,&r) && strstr(r.markup,"<mtable>");
            fz_free(d->ctx,r.raw);fz_free(d->ctx,r.markup);if(!accepted)continue;
            Block *dest=&p->blocks[i],*from=&p->blocks[j];
            for(int k=0;k<from->n;k++){p->lines[from->lines[k]].owner=-1;pd_claim_line(d,p,dest,from->lines[k]);}
            for(int k=0;k<p->nmark;k++)if(p->marks[k].owner==j)p->marks[k].owner=i;
            snprintf(dest->pub.reason,sizeof(dest->pub.reason),"Overlapping source components with a verified matrix tree");
            fz_free(d->ctx,from->lines);memset(from,0,sizeof(*from));from->pub.kind=(PD_Kind)-1;
        }
    compact_blocks(p);
}
/* Fraction bars connect ABOVE and BELOW source baselines. Connected
 * components handle nested fractions; a complete source-checked layout tree
 * is required before ownership changes. This removes the dependence on the
 * body-prose line spacing for the supported mathematical structures. */
static void fraction_regions(Document *d,Page *p) {
    if(!p->nmark || !p->nline)return;
    int *parent=fz_malloc_array(d->ctx,p->nline+p->nmark,int);fz_var(parent);
    fz_try(d->ctx) {
        for(int i=0;i<p->nline+p->nmark;i++)parent[i]=i;
        /* Existing equation boundaries are indivisible. Taking only the
         * fraction out of a larger expression would strand its right-hand
         * side or equation number in a separate paragraph. */
        for(int i=0;i<p->nblock;i++) {
            Block *b=&p->blocks[i];if(b->pub.kind!=PD_EQUATION || !b->n)continue;
            for(int j=1;j<b->n;j++)parent[b->lines[j]]=b->lines[0];
        }
        for(int mi=0;mi<p->nmark;mi++) {
            Mark *m=&p->marks[mi];
            if(m->image || m->owner<0 || m->owner>=p->nblock || p->blocks[m->owner].pub.kind!=PD_EQUATION)continue;
            PD_Rect bar=m->box;int above=-1,below=-1;float top=FLT_MAX,bottom=FLT_MAX;
            for(int li=0;li<p->nline;li++) {
                Line *l=&p->lines[li];
                if(l->owner<0 || l->owner>=p->nblock || p->blocks[l->owner].pub.kind!=PD_EQUATION || !l->math ||
                   l->heading || l->caption || l->margin || fabsf(l->dy)>.05f)continue;
                if(bar.y1-bar.y0>l->size*.2f || bar.x1-bar.x0<l->size*.3f)continue;
                float x=(l->box.x0+l->box.x1)*.5f,y=(l->box.y0+l->box.y1)*.5f;
                if(x<bar.x0 || x>bar.x1)continue;
                if(y<bar.y0 && bar.y0-l->box.y1<top){above=li;top=bar.y0-l->box.y1;}
                if(y>bar.y1 && l->box.y0-bar.y1<bottom){below=li;bottom=l->box.y0-bar.y1;}
            }
            if(above<0 || below<0 || top>p->lines[above].size || bottom>p->lines[below].size)continue;
            int a=math_root(parent,above),b=math_root(parent,below),mark=p->nline+mi;
            parent[b]=a;parent[mark]=a;
        }
        for(int root=0;root<p->nline;root++) {
            if(math_root(parent,root)!=root)continue;
            PD_Rect box=p->lines[root].box;int members=0;
            int destination=-1;
            for(int i=0;i<p->nblock;i++) {
                Block *b=&p->blocks[i];
                if(b->pub.kind==PD_EQUATION && b->n && math_root(parent,b->lines[0])==root) {
                    box=rect_union(box,b->pub.box);members++;if(destination<0)destination=i;
                }
            }
            if(members<2)continue;
            int safe=1;
            for(int i=0;i<p->nline;i++)if(rect_overlap(box,p->lines[i].box)) {
                Line *l=&p->lines[i];
                if(math_root(parent,i)!=root || l->heading || l->caption || l->margin ||
                   l->box.x0<box.x0 || l->box.x1>box.x1 || l->box.y0<box.y0 || l->box.y1>box.y1)safe=0;
            }
            for(int i=0;i<p->nmark;i++)if(rect_overlap(box,p->marks[i].box)) {
                int owner=p->marks[i].owner;
                if(owner<0 || owner>=p->nblock || p->blocks[owner].pub.kind!=PD_EQUATION || !p->blocks[owner].n ||
                   math_root(parent,p->blocks[owner].lines[0])!=root)safe=0;
            }
            if(!safe)continue;
            Reconstruction r={.task=2,.line=-1,.display=1,.box=box};
            int accepted=pd_source_math(d,p,&r);fz_free(d->ctx,r.raw);fz_free(d->ctx,r.markup);
            if(!accepted)continue;
            Block *dest=&p->blocks[destination];dest->pub.box=box;
            snprintf(dest->pub.reason,sizeof(dest->pub.reason),"Fraction-rule component with a verified source layout tree");
            for(int i=destination+1;i<p->nblock;i++) {
                Block *b=&p->blocks[i];
                if(b->pub.kind!=PD_EQUATION || !b->n || math_root(parent,b->lines[0])!=root)continue;
                for(int j=0;j<b->n;j++){p->lines[b->lines[j]].owner=-1;pd_claim_line(d,p,dest,b->lines[j]);}
                for(int j=0;j<p->nmark;j++)if(p->marks[j].owner==i)p->marks[j].owner=destination;
                fz_free(d->ctx,b->lines);memset(b,0,sizeof(*b));b->pub.kind=(PD_Kind)-1;
            }
        }
        compact_blocks(p);
    }
    fz_always(d->ctx)fz_free(d->ctx,parent);
    fz_catch(d->ctx)fz_rethrow(d->ctx);
}
static void math_and_unknown(Document *d,Page *p) {
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];if(l->owner>=0 || l->margin || l->heading)continue;
        int display=l->math>0 && (l->math>l->n*.18f || l->letters<18);
        int scattered=0;
        for(int j=i+1;j<p->nline && p->lines[j].box.y0<l->box.y1;j++) {
            Line *t=&p->lines[j];
            if(t->owner<0 && rect_distance(l->box,t->box)<p->body &&
               t->math && (t->math>t->n*.18f || t->letters<18))scattered=1;
        }
        if(l->bad || display || scattered) {
            Block *b=pd_add_block(d,p,(l->math || scattered)?PD_EQUATION:PD_FIGURE,rect_pad(l->box,1.8f),
                                 l->bad?"Unmapped PDF glyphs; original region retained":
                                        "Mathematical typography and two-dimensional glyph geometry");
            b->pub.review=1;pd_claim_line(d,p,b,i);
            for(int pass=0;pass<3;pass++)for(int j=0;j<p->nline;j++) {
                Line *t=&p->lines[j];
                if(t->owner>=0 || t->margin || t->heading || t->caption)continue;
                float gap=rect_distance(b->pub.box,t->box);
                int equation_number=t->letters==0 && t->digits>0 && t->n<10 &&
                    t->box.y0<b->pub.box.y1+p->body*.5f && t->box.y1>b->pub.box.y0-p->body*.5f;
                if((gap<p->body*.9f && (t->math || t->size<p->body*.83f)) || equation_number)
                    pd_claim_line(d,p,b,j);
            }
            /* Short connective labels inside a display's vertical span are
             * part of its two-dimensional arrangement (e.g. "where"). */
            for(int j=0;j<p->nline;j++) {
                Line *t=&p->lines[j];
                if(t->owner<0 && !t->margin && !t->heading && !t->caption &&
                   t->words<3 && t->n<16 && t->box.y0>=b->pub.box.y0 &&
                   t->box.y1<=b->pub.box.y1 && rect_distance(t->box,b->pub.box)<p->body*5)
                    pd_claim_line(d,p,b,j);
            }
            claim_area(d,p,b,1);
        } else if(l->math) {
            /* Mark whole space-delimited tokens. Adjacent punctuation and
             * sub/superscripts belong to the same graphical expression. */
            int start=0;
            while(start<l->n) {
                while(start<l->n && l->glyph[start].c<=32)start++;
                int end=start,math=0;
                while(end<l->n && l->glyph[end].c>32) {if(l->glyph[end].flags&G_MATH)math=1;end++;}
                if(math)for(int k=start;k<end;k++)l->glyph[k].flags|=G_INLINE;
                start=end+1;
            }
        }
    }
}
static void leftover_marks(Document *d,Page *p) {
    for(int i=0;i<p->nmark;i++) {
        Mark *m=&p->marks[i];if(m->owner>=0)continue;
        if(rect_area(m->box)>rect_area(p->box)*.92f) {
            /* A large coloured/shaded background cannot be faithfully
             * separated from its overlaid text with these rules. */
            continue;
        }
        int best=-1;float distance=p->body;
        for(int j=0;j<p->nblock;j++) {
            PD_Kind k=p->blocks[j].pub.kind;
            if(k!=PD_TABLE && k!=PD_FIGURE && k!=PD_EQUATION)continue;
            float delta=rect_distance(m->box,p->blocks[j].pub.box);
            if(delta<distance){distance=delta;best=j;}
        }
        if(best>=0) {
            Block *b=&p->blocks[best];b->pub.box=rect_union(b->pub.box,rect_pad(m->box,1));
            m->owner=best;claim_area(d,p,b,1);
        } else {
            int rule=m->box.y1-m->box.y0<p->body*.2f && m->box.x1-m->box.x0>p->body*4;
            Block *b=pd_add_block(d,p,rule?PD_RULE:PD_FIGURE,rect_pad(m->box,1.5f),
                                 rule?"Horizontal text separator":"Unclassified drawing retained; check region");
            b->pub.review=!rule;m->owner=(int)(b-p->blocks);
            if(!rule)claim_area(d,p,b,1);
            if(!b->pub.label[0])snprintf(b->pub.label,sizeof(b->pub.label),"Drawing on page %d",p->number+1);
        }
    }
}
/* An indented first line can follow a completely full final line. Require
 * the following line to return to the established margin so that hanging
 * references are not mistaken for a new paragraph on every second line. */
static int returns_to_margin(Page *p,int index,float left,float size) {
    Line *a=&p->lines[index];
    for(int k=index+1;k<p->nline;k++) {
        Line *t=&p->lines[k];float gap=t->baseline-a->baseline;
        if(gap>p->leading*1.25f)break;
        if(t->owner>=0 || t->heading || t->margin || gap<size*.6f ||
           fabsf(t->size-size)>size*.12f || rect_x_overlap(a->box,t->box)<size*3)continue;
        return fabsf(t->box.x0-left)<size*.4f;
    }
    return 0;
}
static void paragraphs(Document *d,Page *p) {
    /* Short papers and isolated test pages can contain more note text than
     * body text. Estimate the note/body contrast from ordinary upper-page
     * prose instead of allowing the notes themselves to set that scale. */
    int sizes[512]={0},best=0;float note_body=p->body;
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];int bin=(int)lroundf(l->size*4);
        if(l->owner>=0 || (l->heading && (l->bold>l->n*.5f || l->size>p->body*1.35f)) ||
           l->caption || l->margin || l->letters<20 || l->words<5 ||
           l->box.y0>p->box.y0+(p->box.y1-p->box.y0)*.55f || bin<1 || bin>=512)continue;
        sizes[bin]+=l->letters;if(sizes[bin]>sizes[best])best=bin;
    }
    if(best)note_body=best/4.f;
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];if(l->owner>=0)continue;
        char label[16];int labelled_note=pd_note_label(l,label) && l->size<note_body*.96f;
        PD_Kind kind=l->margin?PD_MARGIN:l->note_start?PD_NOTE:l->heading?PD_HEADING:
                     (l->size<note_body*.86f || labelled_note) && l->box.y0>p->box.y0+(p->box.y1-p->box.y0)*.6f?PD_NOTE:PD_TEXT;
        Block *b=pd_add_block(d,p,kind,l->box,
                             l->heading?"Numbering, typography and surrounding whitespace":
                             l->margin?"Source page number retained in page navigation and source map":
                             "Neighbouring baselines, font scale and column alignment");
        b->pub.level=l->heading;pd_claim_line(d,p,b,i);
        if(kind==PD_MARGIN)continue;
        Line *last=l;
        for(int j=i+1;j<p->nline;j++) {
            Line *t=&p->lines[j];
            if(t->box.y0-last->box.y1>p->leading)break;
            if(t->owner>=0 || t->margin || t->caption!=l->caption || t->front!=l->front)continue;
            if(kind==PD_HEADING) {
                if(t->heading!=l->heading || (l->front!=1 && (numbered_heading(t->text) || word_heading(t->text))) ||
                   fabsf(t->size-l->size)>(l->front==1?l->size*.25f:.4f) || (l->front!=1 && (t->bold<t->n*.6f || l->bold<l->n*.6f)))break;
            } else if(t->heading) {
                if(rect_x_overlap(b->pub.box,t->box)>p->body)break;
                continue;
            }
            if(fabsf(t->size-l->size)>(l->front==1?l->size*.25f:p->body*.12f) || fabsf(t->dy)>.08f)continue;
            float overlap=rect_x_overlap(last->box,t->box);
            float minwidth=fminf(last->box.x1-last->box.x0,t->box.x1-t->box.x0);
            int short_tail=kind!=PD_HEADING && t->box.x1-t->box.x0<p->body*2.5f &&
                t->box.x0<=b->pub.box.x0+p->body*.4f && b->pub.box.x0-t->box.x0<p->body*2;
            int title_center=l->front==1 && fabsf(t->box.x0+t->box.x1-l->box.x0-l->box.x1)<l->size*2;
            if((overlap<minwidth*.45f && !short_tail) ||
               (!title_center && fabsf(t->box.x0-l->box.x0)>p->body*(l->dropcap?6.f:3.5f)))continue;
            if(t->baseline-last->baseline>(kind==PD_HEADING?l->size*1.9f:p->leading*1.25f))continue;
            if(t->box.y0<last->box.y0+p->body*.35f)continue;
            if(t->note_start)break;
            if(kind==PD_NOTE) {char label[16];if(pd_note_label(t,label))break;}
            if(kind!=PD_HEADING) {
                int fresh=t->box.x0-last->box.x0>p->body*.6f;
                int short_end=last->box.x1<b->pub.box.x1-p->body*2;
                int wrapped_cap=l->dropcap && t->box.y0<l->glyph[0].box.y1+p->body*.3f;
                if(fresh && !wrapped_cap && (short_end || (b->n>=2 && returns_to_margin(p,j,b->pub.box.x0,l->size))))break;
                /* Return to the first-line margin starts the next item in
                 * a hanging-indent bibliography or list. */
                if(!l->dropcap && b->n>=2 && last->box.x0-l->box.x0>p->body*.6f &&
                   fabsf(t->box.x0-l->box.x0)<p->body*.4f)break;
            }
            pd_claim_line(d,p,b,j);last=t;
        }
        if(kind==PD_TEXT && b->n>=2) {
            float rest=p->lines[b->lines[1]].box.x0;
            for(int j=2;j<b->n;j++)rest=fminf(rest,p->lines[b->lines[j]].box.x0);
            if(l->box.x0-rest>p->body*.6f)b->indent=1;
            if(rest-l->box.x0>p->body*.6f)b->indent=-1;
        }
    }
}
static int compare_block_y(const void *aa,const void *bb) {
    const Block *a=aa,*b=bb;
    if(a->pub.box.y0!=b->pub.box.y0)return a->pub.box.y0<b->pub.box.y0?-1:1;
    return a->pub.box.x0<b->pub.box.x0?-1:a->pub.box.x0>b->pub.box.x0;
}
static int compare_block_x(const void *aa,const void *bb) {
    const Block *a=aa,*b=bb;
    return a->pub.box.x0<b->pub.box.x0?-1:a->pub.box.x0>b->pub.box.x0;
}
static int column_side(Block *b,float cut,float unit) {
    if(b->pub.kind==PD_MARGIN || b->pub.kind==PD_DETAIL)return 3;
    if(b->pub.box.x1<=cut+unit*.3f)return 0;
    if(b->pub.box.x0>=cut-unit*.3f)return 1;
    return 2;
}
static void reading_order(Block *b,int n,float unit,int depth);
/* A heading in the right column must not jump ahead of prose in the left.
 * Vote for gutters using simultaneous prose lines, then order each column
 * between genuinely spanning blocks. Whole-page XY cuts alone cannot make
 * this distinction when the two columns have differently placed headings. */
static int column_order(Document *d,Page *p) {
    int votes[512]={0},best=0,peak=0;float width=p->box.x1-p->box.x0;
    for(int i=0;i<p->nline;i++) {
        Line *a=&p->lines[i];
        if(a->front || a->margin || a->heading || a->letters<20 || a->words<5 ||
           fabsf(a->dy)>.05f || fabsf(a->size-p->body)>p->body*.18f)continue;
        for(int j=0;j<p->nline;j++) {
            Line *b=&p->lines[j];float gap=b->box.x0-a->box.x1;
            if(b->front || b->margin || b->heading || b->letters<20 || b->words<5 ||
               fabsf(b->dy)>.05f || fabsf(b->size-p->body)>p->body*.18f ||
               fabsf(a->baseline-b->baseline)>p->body*.6f || gap<p->body || gap>width*.16f)continue;
            int x0=(int)((a->box.x1+p->body*.3f-p->box.x0)/width*512);
            int x1=(int)((b->box.x0-p->body*.3f-p->box.x0)/width*512);
            for(int x=x0;x<=x1;x++)if(x>=128 && x<384)votes[x]++;
        }
    }
    for(int x=128;x<384;x++)if(votes[x]>peak){peak=votes[x];best=x;}
    if(peak<3)return 0;
    int end=best;while(end+1<384 && votes[end+1]==peak)end++;
    float cut=p->box.x0+(best+end+1)*.5f/512*width;
    int n=p->nblock,count[4]={0};
    for(int i=0;i<n;i++)count[column_side(&p->blocks[i],cut,p->body)]++;
    if(!count[0] || !count[1])return 0;
    qsort(p->blocks,n,sizeof(Block),compare_block_y);
    Block *out=fz_calloc(d->ctx,(size_t)n,sizeof(Block)+1);unsigned char *used=(unsigned char*)(out+n);int next=0;
    for(int span=0;span<=n;span++) {
        if(span<n && column_side(&p->blocks[span],cut,p->body)!=2)continue;
        float bottom=span<n?p->blocks[span].pub.box.y0-p->body*.25f:FLT_MAX;
        for(int side=0;side<2;side++) {
            int begin=next;
            for(int i=0;i<n;i++)if(!used[i] && column_side(&p->blocks[i],cut,p->body)==side && p->blocks[i].pub.box.y0<bottom) {
                out[next++]=p->blocks[i];used[i]=1;
            }
            reading_order(out+begin,next-begin,p->body,0);
        }
        if(span<n){out[next++]=p->blocks[span];used[span]=1;}
    }
    for(int i=0;i<n;i++)if(!used[i])out[next++]=p->blocks[i];
    memcpy(p->blocks,out,(size_t)n*sizeof(Block));fz_free(d->ctx,out);return 1;
}
/* Recursive whitespace cuts. A continuous column gutter takes precedence
 * over paragraph gaps. Spanning material blocks that gutter and therefore
 * causes a horizontal split before the columns can be separated. */
static void reading_order(Block *b,int n,float unit,int depth) {
    if(n<2 || depth>32)return;
    qsort(b,n,sizeof(Block),compare_block_x);
    float right=b[0].pub.box.x1,best=unit*1.25f;int cut=-1;
    for(int i=1;i<n;i++) {
        float gap=b[i].pub.box.x0-right;
        if(gap>best){best=gap;cut=i;}
        right=fmaxf(right,b[i].pub.box.x1);
    }
    if(cut>0) {
        reading_order(b,cut,unit,depth+1);reading_order(b+cut,n-cut,unit,depth+1);return;
    }
    qsort(b,n,sizeof(Block),compare_block_y);
    right=b[0].pub.box.y1;best=unit*.65f;cut=-1;
    for(int i=1;i<n;i++) {
        float gap=b[i].pub.box.y0-right;
        if(gap>best){best=gap;cut=i;}
        right=fmaxf(right,b[i].pub.box.y1);
    }
    if(cut>0) {
        reading_order(b,cut,unit,depth+1);reading_order(b+cut,n-cut,unit,depth+1);
    } else qsort(b,n,sizeof(Block),compare_block_y);
}
/* Canonicalise letter-spaced editorial headings, not body text. This
 * recognises typography such as R E F E R E N C E S without naming a publisher. */
static void heading_key(const char *s,char out[160]) {
    int n=0;
    for(;*s && n<159;s++)if(isalnum((unsigned char)*s))out[n++]=(char)tolower((unsigned char)*s);
    out[n]=0;
}
static int references_title(const char *s) {
    char key[160];heading_key(s,key);
    for(int pass=0;pass<2;pass++) {
        if(!strcmp(key,"references") || !strcmp(key,"bibliography") ||
           !strcmp(key,"literaturecited") || !strcmp(key,"workscited"))return 1;
        const char *title;int letter;if(!heading_prefix(s,&title,&letter))break;
        heading_key(title,key);
    }
    return 0;
}
static int appendix_title(const char *s) {
    const char *title;int letter;
    if(heading_prefix(s,&title,&letter))s=title;
    s=trim(s);
    const char *words[]={"Appendix","Appendices","Online appendix","Online appendices",
                         "Supplementary appendix","Supplementary material","Supplementary materials",
                         "Supplemental material","Supplemental materials",
                         "Supplementary information","Online supplement","Supplement"};
    for(size_t i=0;i<sizeof(words)/sizeof(words[0]);i++) {
        size_t n=strlen(words[i]);
        if(!strncasecmp(s,words[i],n) && (!s[n] || isspace((unsigned char)s[n]) || s[n]==':' || s[n]=='.')) {
            const char *tail=trim(s+n);
            /* Object references are sentences, even when they start a line.
             * A heading may have an appendix identifier and a title; a
             * reference such as 'Appendix D) and ...' has neither. */
            const char *objects[]={"Figure ","Figures ","Fig. ","Figs. ","Table ","Tables ","Section ","Sections ","Equation ","Equations "};
            for(size_t k=0;k<sizeof(objects)/sizeof(*objects);k++)if(!strncasecmp(tail,objects[k],strlen(objects[k])))return 0;
            if(isalpha((unsigned char)tail[0]) && tail[1]==')')return 0;
            if(*tail && islower((unsigned char)*tail))return 0;
            if(isupper((unsigned char)tail[0]) && (tail[1]==' ' || tail[1]=='.')) {
                const char *rest=trim(tail+2);
                if(islower((unsigned char)*rest))return 0;
            }
            return 1;
        }
    }
    char key[160];heading_key(s,key);
    return !strcmp(key,"appendix") || !strcmp(key,"appendices") || !strcmp(key,"supplementarymaterial");
}
typedef struct { int appendix,references,reference_level,nested,roman; } SectionState;
static int section_at_heading(SectionState *state,const char *text,int level,int letter_evidence) {
    const char *title;int letter;int prefix=heading_prefix(text,&title,&letter);
    if(references_title(text)) {
        state->references=1;state->reference_level=level;
        state->nested=state->appendix && prefix && letter==1;
    } else {
        int explicit_appendix=appendix_title(text);
        /* No page fraction or leading A may switch off main text. A bare
         * lettered appendix needs the document's bibliography boundary or
         * a consistent series, established by section_scopes. */
        if(explicit_appendix || (prefix && letter==1 && (state->appendix || letter_evidence))) {
            state->appendix=1;state->references=0;
        } else if(state->references && level<=state->reference_level)state->references=0;
        if(!explicit_appendix && state->appendix && prefix && !letter && level==1)state->appendix=0;
        if(prefix && letter==2)state->roman=1;
    }
    return state->references?(PD_REFERENCES|(state->nested?PD_APPENDIX:0)):
           state->appendix?PD_APPENDIX:0;
}
static int excluded(Document *d,int section) {
    return d->opt && (((section&PD_REFERENCES) && d->opt->exclude_references) ||
                     ((section&PD_APPENDIX) && d->opt->exclude_appendices));
}
static void section_scopes(Document *d) {
    SectionState state={0};int section=0;
    for(int i=0;i<d->npage;i++)for(int j=0;j<d->pages[i].nblock;j++) {
        Page *p=&d->pages[i];Block *b=&p->blocks[j];
        /* The sequence context supplies evidence; physical location does not.
         * Lettered subsections under Roman main sections stay in main text. */
        int letter_evidence=state.references && !state.nested;
        if(!letter_evidence && !state.roman && b->pub.kind==PD_HEADING) {
            const char *title;int letter;
            int level=heading_prefix(b->pub.label,&title,&letter);
            if(level==1 && letter==1 && b->pub.label[0]=='A') {
                int following=0;
                for(int qi=i;qi<d->npage && !following;qi++)for(int qj=qi==i?j+1:0;qj<d->pages[qi].nblock;qj++) {
                    Block *next=&d->pages[qi].blocks[qj];if(next->pub.kind!=PD_HEADING || next->pub.front)continue;
                    const char *tail;int type;int depth=heading_prefix(next->pub.label,&tail,&type);
                    if(depth==1 && type==1 && next->pub.label[0]=='B')following=1;
                    else if(depth==1 && (type==0 || type==2))following=-1;
                    if(following)break;
                }
                /* A/B can also be main sections. Only a preceding global
                 * bibliography establishes an unlabelled appendix series. */
                int bibliography=0;
                for(int qi=0;qi<=i;qi++)for(int qj=0;qj<(qi==i?j:d->pages[qi].nblock);qj++)
                    if(d->pages[qi].blocks[qj].pub.kind==PD_HEADING && references_title(d->pages[qi].blocks[qj].pub.label))bibliography=1;
                letter_evidence=following>0 && bibliography;
            }
        }
        if(b->pub.kind==PD_HEADING && !b->pub.front) {
            const char *title;int letter;
            if(state.roman && !state.appendix && !state.references && heading_prefix(b->pub.label,&title,&letter) && letter==1)
                b->pub.level=b->pub.level<2?2:b->pub.level;
            section=section_at_heading(&state,b->pub.label,b->pub.level,letter_evidence);
        }
        if(b->pub.kind==PD_PAGE_IMAGE) {
            /* A retained original may cross a boundary. Do not silently
             * discard its main text; preserve it and explain the conflict. */
            int any_keep=0,any_drop=0,all_sections=0;
            for(int k=0;k<p->nline;k++) {
                Line *l=&p->lines[k];
                if(l->heading)section=section_at_heading(&state,l->text,l->heading,state.references && !state.nested);
                if(l->margin)continue;
                all_sections|=section;
                if(excluded(d,section))any_drop=1;else any_keep=1;
            }
            b->pub.section=all_sections?all_sections:section;
            b->pub.omitted=any_drop?!any_keep:excluded(d,section);
            if(any_keep && any_drop)snprintf(b->pub.reason,sizeof(b->pub.reason),
                "Original page crosses an excluded section; whole page kept to preserve its included content");
        } else {
            b->pub.section=section;b->pub.omitted=excluded(d,section);
        }
        if(b->pub.omitted)b->pub.anchor[0]=0;
    }
    /* A header folio before an excluded section must not keep an empty
     * spine item alive. Its inclusion follows the remaining page content. */
    for(int i=0;i<d->npage;i++) {
        Page *p=&d->pages[i];int keep=0,sections=0;
        for(int j=0;j<p->nblock;j++)if(p->blocks[j].pub.kind!=PD_MARGIN) {
            keep|=!p->blocks[j].pub.omitted;sections|=p->blocks[j].pub.section;
        }
        if(keep)continue;
        for(int j=0;j<p->nblock;j++)if(p->blocks[j].pub.kind==PD_MARGIN) {
            p->blocks[j].pub.omitted=1;p->blocks[j].pub.section=sections;
            p->blocks[j].pub.anchor[0]=0;
        }
    }
}
static void page_continuations(Document *d) {
    for(int i=1;i<d->npage;i++) {
        Page *p=&d->pages[i-1],*q=&d->pages[i];Block *a=NULL,*b=NULL;
        if(q->number!=p->number+1)continue;
        for(int j=0;j<p->nblock;j++)if(!p->blocks[j].pub.omitted && !p->blocks[j].pub.note && p->blocks[j].pub.kind!=PD_MARGIN && p->blocks[j].pub.kind!=PD_DETAIL)a=&p->blocks[j];
        for(int j=0;j<q->nblock;j++)if(!q->blocks[j].pub.omitted && !q->blocks[j].pub.note && q->blocks[j].pub.kind!=PD_MARGIN && q->blocks[j].pub.kind!=PD_DETAIL){b=&q->blocks[j];break;}
        if(!a || !b || a->pub.kind!=PD_TEXT || b->pub.kind!=PD_TEXT || a->n<2 || b->n<2 ||
           a->pub.section!=b->pub.section || b->indent)continue;
        Line *last=&p->lines[a->lines[a->n-1]],*first=&q->lines[b->lines[0]];
        float pw=p->box.x1-p->box.x0,qw=q->box.x1-q->box.x0;
        if(a->pub.box.y1<p->box.y0+(p->box.y1-p->box.y0)*.65f ||
           b->pub.box.y0>q->box.y0+(q->box.y1-q->box.y0)*.20f ||
           a->pub.box.x1-a->pub.box.x0<pw*.55f || b->pub.box.x1-b->pub.box.x0<qw*.55f ||
           fabsf(last->size-first->size)>d->body*.08f ||
           fabsf((a->pub.box.x0-p->box.x0)-(b->pub.box.x0-q->box.x0))>d->body*.5f ||
           fabsf(first->box.x0-b->pub.box.x0)>d->body*.4f ||
           last->box.x1<a->pub.box.x1-d->body)continue;
        const char *start=trim(first->text);size_t n=strlen(last->text);
        while(n && isspace((unsigned char)last->text[n-1]))n--;
        /* Do not infer a continuation from geometry alone after a sentence
         * boundary, before a capital, across notes or across columns. */
        if(!n || strchr(".!?:;",last->text[n-1]) || !islower((unsigned char)*start))continue;
        b->pub.continues=a->pub.id;a->join_next=1;
    }
}
/* The same grouping pipeline builds both alternatives. The original
 * source lines and marks are never removed by either candidate. */
static void page_blocks(Document *d,Page *p,int use_model) {
    pd_page_details(d,p);
    if(use_model)pd_model_regions(d,p);
    contents_regions(d,p);caption_regions(d,p);continued_tables(d,p);
    rotated_regions(d,p);table_regions(d,p);text_decorations(p);
    figures(d,p);math_and_unknown(d,p);leftover_marks(d,p);fraction_regions(d,p);
    p->body=prose_size(p,p->body);p->leading=line_spacing(p);
    pd_note_candidates(p);
    paragraphs(d,p);
    if(!column_order(d,p))reading_order(p->blocks,p->nblock,p->body,0);
}
/* A detector may suggest several panels, but one source caption establishes
 * a common object. Its already-grouped components must still have one owner.
 * This check uses component identities, not the name or dimensions of a paper. */
static int preserves_objects(Page *p,Block *base,int count,int *marks) {
    for(int i=0;i<count;i++) {
        Block *b=&base[i];if(b->pub.kind!=PD_TABLE && b->pub.kind!=PD_FIGURE)continue;
        int captions=0,owner=-1;
        for(int j=0;j<b->n;j++)captions+=p->lines[b->lines[j]].caption!=0;
        if(captions!=1)continue;
        for(int j=0;j<b->n;j++) {
            int at=p->lines[b->lines[j]].owner;
            if(owner<0)owner=at;
            if(at<0 || owner!=at)return 0;
        }
        for(int j=0;j<p->nmark;j++)if(marks[j]==i && p->marks[j].owner!=owner)return 0;
        if(owner<0 || p->blocks[owner].pub.kind!=b->pub.kind)return 0;
        /* Independent captions cannot be merged into the same new object. */
        int chosen=0;
        for(int j=0;j<p->blocks[owner].n;j++)chosen+=p->lines[p->blocks[owner].lines[j]].caption!=0;
        if(chosen>1)return 0;
    }
    return 1;
}
void pd_layout(Document *d) {
    int next_id=1;
    pd_model_start(d);
    for(int pi=0;pi<d->npage;pi++) {
        pd_check(d,"Analysing layout",pi,d->npage);
        Page *p=&d->pages[pi];p->body=prose_size(p,d->body);
        join_rules(d,p);
        drop_caps(d,p);
        join_scripts(d,p);
        join_fragments(d,p);
        p->leading=line_spacing(p);
        if(p->nline>1)qsort(p->lines,p->nline,sizeof(Line),compare_lines);
        int nchar=pd_page_glyphs(p);fz_var(nchar);
        headings(p);
        confirm_folios(d,p);
        int forced=d->opt && d->opt->keep_pages && p->number<d->opt->keep_count && d->opt->keep_pages[p->number];
        pd_model_page(d,p);
        int background=0;
        for(int j=0;j<p->nmark;j++)if(rect_area(p->marks[j].box)>.92f*rect_area(p->box))background=1;
        if(forced || nchar<PD_MIN_PAGE_GLYPHS || p->complex_graphics || background || p->front_sheet) {
            all_graphic(d,p,forced?"Page retained as an image by user":
                        p->front_sheet?"Publisher or repository cover sheet; original publication details retained":
                        nchar<PD_MIN_PAGE_GLYPHS?"No usable text layer; page preserved without OCR":
                        "Complex or full-page graphics; page preserved for inspection");
        } else {
            page_blocks(d,p,0);
            if(p->nhints) {
                p->nbaseline=p->nblock;p->baseline=fz_calloc(d->ctx,p->nblock,sizeof(PD_Block));
                for(int i=0;i<p->nblock;i++)p->baseline[i]=p->blocks[i].pub;
                pd_model_compare(d,p);
                Block *base=p->blocks;int count=p->nblock,capacity=p->cblock;
                Line *saved=NULL;int *flags=NULL,*marks=NULL;
                float body=p->body,leading=p->leading;
                fz_var(base);fz_var(saved);fz_var(flags);fz_var(marks);
                fz_try(d->ctx) {
                    saved=fz_calloc(d->ctx,p->nline,sizeof(Line));flags=fz_calloc(d->ctx,nchar,sizeof(int));
                    marks=fz_calloc(d->ctx,p->nmark,sizeof(int));
                    memcpy(saved,p->lines,p->nline*sizeof(Line));
                    for(int i=0,k=0;i<p->nline;i++) {
                        for(int j=0;j<p->lines[i].n;j++)flags[k++]=p->lines[i].glyph[j].flags;
                        p->lines[i].owner=-1;
                    }
                    for(int i=0;i<p->nmark;i++){marks[i]=p->marks[i].owner;p->marks[i].owner=-1;}
                    p->blocks=NULL;p->nblock=p->cblock=0;
                    page_blocks(d,p,1);
                    if(!preserves_objects(p,base,count,marks)) {
                        for(int i=0;i<p->nblock;i++)fz_free(d->ctx,p->blocks[i].lines);
                        fz_free(d->ctx,p->blocks);p->blocks=base;p->nblock=count;p->cblock=capacity;base=NULL;
                        memcpy(p->lines,saved,p->nline*sizeof(Line));p->body=body;p->leading=leading;
                        for(int i=0,k=0;i<p->nline;i++)for(int j=0;j<p->lines[i].n;j++)p->lines[i].glyph[j].flags=flags[k++];
                        for(int i=0;i<p->nmark;i++)p->marks[i].owner=marks[i];
                        for(int i=0;i<p->nhints;i++)if(p->hints[i].applied) {
                            p->hints[i].applied=0;d->model_stats.applied--;
                            snprintf(p->hints[i].decision,sizeof(p->hints[i].decision),"Algorithmic page retained: caption/component relationships would regress");
                        }
                    }
                }
                fz_always(d->ctx) {
                    if(base && base!=p->blocks){for(int i=0;i<count;i++)fz_free(d->ctx,base[i].lines);fz_free(d->ctx,base);}
                    fz_free(d->ctx,saved);fz_free(d->ctx,flags);fz_free(d->ctx,marks);
                }
                fz_catch(d->ctx)fz_rethrow(d->ctx);
            }
        }
        if(d->opt && d->opt->reader)matrix_regions(d,p);
        int assigned=0;
        for(int j=0;j<p->nblock;j++) {
            Block *b=&p->blocks[j];b->pub.id=next_id++;
            snprintf(b->pub.anchor,sizeof(b->pub.anchor),"p%04d.xhtml#b%d",p->number+1,b->pub.id);
            assigned+=b->pub.glyphs;
            if(!b->pub.label[0])snprintf(b->pub.label,sizeof(b->pub.label),"%s",pd_kind_name(b->pub.kind));
            /* Crop padding is not allowed to wander outside the page. */
            b->pub.box=from_fz(fz_intersect_rect(to_fz(b->pub.box),to_fz(p->box)));
        }
        if(assigned!=nchar)fz_throw(d->ctx,FZ_ERROR_GENERIC,"Glyph accounting failed on page %d: %d / %d",p->number+1,assigned,nchar);
    }
    section_scopes(d);
    pd_footnotes(d);
    page_continuations(d);
}
