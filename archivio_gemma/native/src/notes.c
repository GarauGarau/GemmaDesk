#include "internal.h"
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

static int marker(int c) {
    if(c==0xb9)return '1';
    if(c==0xb2)return '2';
    if(c==0xb3)return '3';
    if(c==0x2070 || (c>=0x2074 && c<=0x2079))return '0'+c-0x2070;
    if((c>='0' && c<='9') || c=='*' || c==0x2020 || c==0x2021 || c==0x2217)return c==0x2217?'*':c;
    return 0;
}
int pd_glyph_script(Line *l,Glyph *g) {
    if(g->c==0xb9 || g->c==0xb2 || g->c==0xb3 || g->c==0x2070 || (g->c>=0x2074 && g->c<=0x2079))return 1;
    if(g->size>=l->size*.90f)return 0;
    if(g->baseline<l->baseline-l->size*.16f)return 1;
    if(g->baseline>l->baseline+l->size*.12f)return -1;
    return 0;
}
static int raised_marker(Line *l,int i) {
    Glyph *g=&l->glyph[i];int c=marker(g->c);
    if(!c)return 0;
    if(pd_glyph_script(l,g)==1)return 1;
    /* Asterisks and daggers often have a raised glyph shape at the ordinary
     * font size. Require a word ending and a matching definition later. */
    if(c>='0' && c<='9')return 0;
    return i>0 && (i+1==l->n || l->glyph[i+1].c<=32 ||
        (l->glyph[i+1].c<128 && ispunct(l->glyph[i+1].c)) || (l->glyph[i+1].flags&G_GAP));
}
static int label_at(Line *l,int start,char label[16],int raised) {
    int end=start,n=0,count=0;
    while(end<l->n && marker(l->glyph[end].c) && (!raised || raised_marker(l,end))) {
        if(++count>3)return 0;
        char encoded[8];int bytes=fz_runetochar(encoded,marker(l->glyph[end].c));
        if(n+bytes>=16)return 0;
        memcpy(label+n,encoded,(size_t)bytes);n+=bytes;end++;
    }
    label[n]=0;return end>start?end:0;
}
int pd_note_label(Line *l,char label[16]) {
    int start=0;while(start<l->n && l->glyph[start].c<=32)start++;
    int end=label_at(l,start,label,0);if(!end || end>=l->n || l->letters<8)return 0;
    int c=l->glyph[end].c;
    /* Scientific decimals and ordinal words are not note labels. */
    if(c=='.' && end+1<l->n && l->glyph[end+1].c>='0' && l->glyph[end+1].c<='9')return 0;
    if(c<128 && islower(c) && pd_glyph_script(l,&l->glyph[start])!=1)return 0;
    return end;
}
static int prose_callout(Line *l,int start,int end) {
    if(!start || start==end)return 0;
    int letters=0,digits=0,begin=start;
    for(int i=start-1;i>=0 && l->glyph[i].c>32;i--) {
        Glyph *g=&l->glyph[i];
        if(g->flags&G_MATH)return 0;
        if(g->c<128 && isalpha(g->c))letters++;
        if(g->c<128 && isdigit(g->c))digits++;
        begin=i;if(g->flags&G_GAP)break;
    }
    /* A superscript on a single mathematical variable is not a footnote.
     * Matching the number alone would turn x squared into a note link. */
    char word[32];int n=0;
    for(int i=begin;i<start && n<31;i++)if(l->glyph[i].c<128 && isalpha(l->glyph[i].c))word[n++]=(char)tolower(l->glyph[i].c);
    word[n]=0;
    const char *operators[]={"log","ln","exp","sin","cos","tan","cot","sinh","cosh","mm","cm","km","kg","mol"};
    for(size_t i=0;i<sizeof(operators)/sizeof(*operators);i++)if(!strcmp(word,operators[i]))return 0;
    return letters>=2 || (digits>=3 && l->glyph[start-1].c==')');
}
static void clear_marker_math(Line *l,int start,int end) {
    int a=start,b=end;
    while(a>0 && l->glyph[a-1].c>32)a--;
    while(b<l->n && l->glyph[b].c>32)b++;
    for(int i=a;i<b;i++)if((i<start || i>=end) && (l->glyph[i].flags&G_MATH))return;
    for(int i=a;i<b;i++)l->glyph[i].flags&=~G_INLINE;
}
/* A note boundary is established before paragraph aggregation. Matching
 * source callouts prevents a new definition being swallowed by an earlier
 * note; font contrast/rules provide independent layout evidence. */
void pd_note_candidates(Page *p) {
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];char label[16];int prefix=pd_note_label(l,label);
        l->note_start=0;
        if(!prefix || l->caption || l->owner>=0)continue;
        int match=0,separator=0;
        for(int j=0;j<p->nmark;j++) {
            PD_Rect r=p->marks[j].box;
            if(r.y1<l->box.y0 && l->box.y0-r.y1<p->leading*4 &&
               r.y1-r.y0<p->body*.3f && r.x1-r.x0>p->body*4 &&
               rect_x_overlap(r,l->box)>p->body*2)separator=1;
        }
        for(int j=0;j<p->nline && !match;j++) {
            Line *t=&p->lines[j];if(t->box.y1>=l->box.y0 || t->caption || t->words<2)continue;
            if(!separator && l->size>=t->size*.97f)continue;
            for(int k=1;k<t->n;k++)if(raised_marker(t,k)) {
                char name[16];int end=label_at(t,k,name,1);
                if(end && !strcmp(name,label) && prose_callout(t,k,end)){match=1;break;}
            }
        }
        if(match){l->note_start=prefix;l->heading=0;}
    }
}
static int definition(Page *p,Block *b,char label[16]) {
    if(b->pub.kind!=PD_NOTE || !b->n || (!p->lines[b->lines[0]].note_start && b->pub.box.y0<=p->box.y0+(p->box.y1-p->box.y0)*.55f))return 0;
    return pd_note_label(&p->lines[b->lines[0]],label);
}
void pd_footnotes(Document *d) {
    for(int pi=0;pi<d->npage;pi++) {
        Page *p=&d->pages[pi];
        for(int bi=0;bi<p->nblock;bi++) {
            Block *note=&p->blocks[bi];char label[16];int prefix=definition(p,note,label);
            if(!prefix)continue;
            int duplicates=0;
            for(int j=0;j<p->nblock;j++)if(j!=bi) {
                char other[16];if(definition(p,&p->blocks[j],other) && !strcmp(label,other))duplicates++;
            }
            if(duplicates) {
                note->pub.review=1;
                snprintf(note->pub.reason,sizeof(note->pub.reason),"Repeated footnote marker on this source page; association left for inspection");
                continue;
            }
            int first_ref=d->nref,kept=0,section=-1;
            for(int j=0;j<p->nblock;j++) {
                Block *body=&p->blocks[j];
                if((body->pub.kind!=PD_TEXT && body->pub.kind!=PD_HEADING && body->pub.kind!=PD_FIGURE &&
                    body->pub.kind!=PD_TABLE && body->pub.kind!=PD_EQUATION) ||
                   body->pub.box.y0>=note->pub.box.y0 || body->pub.section&PD_REFERENCES)continue;
                for(int k=0;k<body->n;k++) {
                    int li=body->lines[k];Line *l=&p->lines[li];
                    for(int g=0;g<l->n;g++) {
                        if(l->glyph[g].ref || !raised_marker(l,g))continue;
                        char candidate[16];int end=label_at(l,g,candidate,1);
                        if(!end || strcmp(candidate,label) || !prose_callout(l,g,end))continue;
                        d->refs=pd_grow(d->ctx,d->refs,&d->cref,d->nref+1,sizeof(NoteRef));
                        NoteRef *ref=&d->refs[d->nref];memset(ref,0,sizeof(*ref));
                        ref->page=pi;ref->line=li;ref->first=g;ref->end=end;ref->note=note->pub.id;ref->omitted=body->pub.omitted;
                        l->glyph[g].ref=++d->nref;clear_marker_math(l,g,end);
                        kept|=!body->pub.omitted;section&=body->pub.section;g=end-1;
                    }
                }
            }
            if(d->nref==first_ref) {
                note->pub.review=1;
                snprintf(note->pub.reason,sizeof(note->pub.reason),"Footnote candidate without an unambiguous raised callout; source position retained");
                continue;
            }
            note->pub.note=note->pub.id;note->note_prefix=prefix;
            note->pub.omitted=!kept;note->pub.section=section;
            clear_marker_math(&p->lines[note->lines[0]],0,prefix);
            snprintf(note->pub.reason,sizeof(note->pub.reason),"Footnote marker %s matched to raised source callout; linked with return to text",label);
            /* Keep following paragraphs and small mathematical displays in
             * the same note, up to the next definition or other content. */
            Block *previous=note;
            for(int j=bi+1;j<p->nblock;j++) {
                Block *b=&p->blocks[j];
                if(b->pub.kind==PD_MARGIN || b->pub.kind==PD_DETAIL || b->pub.kind==PD_RULE)continue;
                char next[16];if(definition(p,b,next))break;
                int compatible=b->pub.kind==PD_NOTE || b->pub.kind==PD_EQUATION;
                int column=0,inline_tail=0;
                if(previous->n && b->n) {
                    Line *a=&p->lines[previous->lines[previous->n-1]],*t=&p->lines[b->lines[0]];
                    int same_size=fabsf(a->size-t->size)<a->size*.1f;
                    size_t end=strlen(a->text);while(end && isspace((unsigned char)a->text[end-1]))end--;
                    int unfinished=end && !strchr(".!?",a->text[end-1]);
                    column=same_size && (islower((unsigned char)*t->text) || unfinished) &&
                        b->pub.box.x0>previous->pub.box.x1 &&
                        b->pub.box.x0-previous->pub.box.x1<(p->box.x1-p->box.x0)*.3f &&
                        b->pub.box.y0>p->box.y0+(p->box.y1-p->box.y0)*.55f;
                    inline_tail=same_size && fabsf(t->baseline-a->baseline)<a->size*.2f &&
                        t->box.x0>=a->box.x1-a->size && t->box.x0<a->box.x1+a->size;
                }
                if(!compatible || (!column && !inline_tail &&
                   (b->pub.box.y0<previous->pub.box.y0 ||
                    (rect_x_overlap(previous->pub.box,b->pub.box)<d->body && fabsf(b->pub.box.x0-previous->pub.box.x0)>d->body*2) ||
                    b->pub.box.x0<previous->pub.box.x0-d->body*2)))break;
                b->pub.note=note->pub.id;b->pub.omitted=!kept;b->pub.section=section;
                if(column || inline_tail)b->pub.continues=previous->pub.id;
                previous=b;
            }
        }
        /* Move a separator only when all the material below it belongs to
         * linked notes. Keep the source rule itself in the notes section. */
        for(int bi=0;bi<p->nblock;bi++) {
            Block *rule=&p->blocks[bi];
            if(rule->pub.kind!=PD_RULE || rule->pub.box.y0<p->box.y0+(p->box.y1-p->box.y0)*.55f)continue;
            Block *first=NULL;int safe=1;
            for(int j=bi+1;j<p->nblock;j++) {
                Block *b=&p->blocks[j];if(b->pub.kind==PD_MARGIN)continue;
                if(!b->pub.note){safe=0;break;}if(!first)first=b;
            }
            if(!safe || !first || first->pub.box.y0-rule->pub.box.y1>d->body*2)continue;
            rule->pub.note=first->pub.note;rule->pub.section=first->pub.section;rule->pub.omitted=first->pub.omitted;
        }
    }
    /* A continued note has no new label on the next page. Require a full
     * preceding line and matching footnote typography/margins; never guess
     * across an omitted page or a fresh numbered definition. */
    for(int pi=1;pi<d->npage;pi++) {
        Page *p=&d->pages[pi-1],*q=&d->pages[pi];Block *last=NULL,*first=NULL;
        if(q->number!=p->number+1)continue;
        for(int j=0;j<p->nblock;j++)if(p->blocks[j].pub.kind==PD_NOTE)last=&p->blocks[j];
        for(int j=0;j<q->nblock;j++)if(q->blocks[j].pub.kind==PD_NOTE){first=&q->blocks[j];break;}
        if(!last || !first || !last->pub.note || first->pub.note || !last->n || !first->n)continue;
        char label[16];if(definition(q,first,label))continue;
        Line *a=&p->lines[last->lines[last->n-1]],*b=&q->lines[first->lines[0]];
        float right=last->pub.box.x1;
        for(int j=0;j<p->nblock;j++)if(p->blocks[j].pub.note==last->pub.note)right=fmaxf(right,p->blocks[j].pub.box.x1);
        size_t n=strlen(a->text);while(n && isspace((unsigned char)a->text[n-1]))n--;
        if(!n || strchr(".!?",a->text[n-1]) || !islower((unsigned char)*b->text) ||
           a->box.x1<right-d->body || fabsf(a->size-b->size)>d->body*.1f ||
           fabsf(last->pub.box.x0-first->pub.box.x0)>d->body*2)continue;
        first->pub.note=last->pub.note;first->pub.section=last->pub.section;first->pub.omitted=last->pub.omitted;
        first->pub.continues=last->pub.id;
        snprintf(first->pub.reason,sizeof(first->pub.reason),"Footnote continues from the preceding source page; typography and margins agree");
        for(int j=(int)(first-q->blocks)+1;j<q->nblock;j++) {
            Block *b=&q->blocks[j];if(b->pub.kind==PD_MARGIN)continue;
            char label[16];if(definition(q,b,label))break;
            if((b->pub.kind!=PD_NOTE && b->pub.kind!=PD_EQUATION) || b->pub.note ||
               (rect_x_overlap(first->pub.box,b->pub.box)<d->body && fabsf(b->pub.box.x0-first->pub.box.x0)>d->body*2))break;
            b->pub.note=last->pub.note;b->pub.section=last->pub.section;b->pub.omitted=last->pub.omitted;
        }
    }
}
