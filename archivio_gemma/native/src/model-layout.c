#include "internal.h"
#include <ctype.h>
#include <float.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

static float coverage(PD_Rect item,PD_Rect area) {
    // Rules may have zero height in PDF coordinates.
    if(item.x1-item.x0<.1f)item=rect_pad(item,.1f);
    if(item.y1-item.y0<.1f)item=rect_pad(item,.1f);
    float x=fminf(item.x1,area.x1)-fmaxf(item.x0,area.x0);
    float y=fminf(item.y1,area.y1)-fmaxf(item.y0,area.y0);
    return x>0 && y>0?x*y/rect_area(item):0;
}
static PD_Kind object_kind(const char *role) {
    if(!strcmp(role,"table"))return PD_TABLE;
    if(!strcmp(role,"image") || !strcmp(role,"image_block") || !strcmp(role,"chart"))return PD_FIGURE;
    if(!strcmp(role,"equation") || !strcmp(role,"equation_block"))return PD_EQUATION;
    return PD_TEXT;
}
static int protected_text(Page *p,Line *l,ModelHint *object) {
    for(int i=0;i<p->nhints;i++) {
        ModelHint *h=&p->hints[i];
        if(h==object || coverage(h->box,object->box)>.85f)continue;
        if(!strcmp(h->role,"text") || !strcmp(h->role,"paragraph_title") ||
           !strcmp(h->role,"doc_title") || !strcmp(h->role,"page_footnote"))
            if(coverage(l->box,h->box)>.8f)return 1;
    }
    return 0;
}
/* A common caption/connected mathematical display is a source relation,
 * independent of how many boxes a detector proposes. Preserve that relation
 * before the source is assigned. Do not extend table proposals across an
 * algorithmic container: it may incorrectly include a neighbouring chart. */
void pd_model_compare(Document *d,Page *p) {
    (void)d;
    for(int bi=0;bi<p->nblock;bi++) {
        Block *b=&p->blocks[bi];PD_Kind kind=b->pub.kind;
        if(kind!=PD_FIGURE && kind!=PD_EQUATION && kind!=PD_TABLE)continue;
        int captions=0,prose=0,math=0;
        for(int j=0;j<b->n;j++) {
            Line *l=&p->lines[b->lines[j]];
            captions+=l->caption==(kind==PD_TABLE?1:2);math+=l->math;
            if(!l->caption && l->words>8 && l->prose>0 && !l->math)prose++;
        }
        int drawing=0;
        if(kind==PD_TABLE)for(int j=0;j<p->nmark;j++) {
            Mark *mark=&p->marks[j];PD_Rect r=mark->box;
            if(coverage(r,b->pub.box)>.8f && (mark->image || (r.x1-r.x0>p->body && r.y1-r.y0>p->body)))drawing=1;
        }
        if(prose || ((kind==PD_FIGURE || kind==PD_TABLE) && captions!=1) ||
           (kind==PD_EQUATION && !math) || (kind==PD_TABLE && drawing))continue;
        int members[256],count=0;PD_Rect box=b->pub.box;
        for(int hi=0;hi<p->nhints;hi++) {
            ModelHint *h=&p->hints[hi];
            if(object_kind(h->role)==kind && coverage(h->box,rect_pad(box,p->body*.3f))>.85f)members[count++]=hi;
        }
        if(!count)continue;
        for(int j=0;j<count;j++)box=rect_union(box,p->hints[members[j]].box);
        /* A single complete source object may contain many visual panels.
         * Nested proposals then naturally lose to the containing proposal. */
        p->hints[members[0]].box=box;
        snprintf(p->hints[members[0]].decision,sizeof(p->hints[0].decision),"Compared with algorithmic object; shared caption/display preserved");
    }
}
void pd_model_regions(Document *d,Page *p) {
    /* A local visual folio prediction supplies the evidence that isolated
     * pages lack. Require an integer, a centred bottom margin and a clear
     * gap from body text; equations and decimal values do not qualify. */
    for(int hi=0;hi<p->nhints;hi++) {
        ModelHint *h=&p->hints[hi];if(strcmp(h->role,"page_number"))continue;
        float width=p->box.x1-p->box.x0,height=p->box.y1-p->box.y0;
        for(int i=0;i<p->nline;i++) {
            Line *l=&p->lines[i];
            if(l->owner>=0 || l->math || !l->text || coverage(l->box,rect_pad(h->box,2))<.9f ||
               l->box.y0<p->box.y0+height*.87f ||
               fabsf((l->box.x0+l->box.x1-p->box.x0-p->box.x1)/2)>width*.16f)continue;
            int digits=0,bad=0;
            for(const unsigned char *s=(const unsigned char*)l->text;*s;s++) {
                if(isdigit(*s))digits++;else if(!isspace(*s))bad=1;
            }
            if(bad || digits<1 || digits>4)continue;
            float gap=height;
            for(int j=0;j<p->nline;j++)if(j!=i && p->lines[j].box.y1<=l->box.y0)
                gap=fminf(gap,l->box.y0-p->lines[j].box.y1);
            if(gap<p->body*2.5f)continue;
            if(!l->margin){l->margin=1;l->heading=0;h->applied=1;d->model_stats.applied++;}
            snprintf(h->decision,sizeof(h->decision),"Source integer in separated bottom margin; kept in page navigation");
        }
    }
    /* Model coordinates are proposals. Only PDF glyphs and drawing commands
     * supply content. Large containers precede their nested panels/formulas. */
    unsigned char visited[256]={0};
    for(int step=0;step<p->nhints;step++) {
        int best=-1;float largest=-1;
        for(int i=0;i<p->nhints;i++)if(!visited[i] && rect_area(p->hints[i].box)>largest) {
            largest=rect_area(p->hints[i].box);best=i;
        }
        if(best<0)break;
        visited[best]=1;ModelHint *h=&p->hints[best];PD_Kind kind=object_kind(h->role);
        if(kind==PD_TEXT)continue;
        const char *reject=NULL;
        PD_Rect box=rect_pad(h->box,fmaxf(1.5f,p->body*.2f));
        int lines=0,digits=0,math=0,marks=0,drawings=0,occupied=0,quadrants[4]={0};
        if(rect_area(box)>.92f*rect_area(p->box))reject="Region is too broad; baseline retained";
        for(int i=0;i<p->nline;i++) {
            Line *l=&p->lines[i];float cover=coverage(l->box,box);
            if(cover<.55f)continue;
            if(l->owner>=0){occupied++;continue;}
            if(l->margin || protected_text(p,l,h) || (p->nbaseline && l->prose>0 && l->words>10 && !l->caption && !l->math)){reject="Conflicting prose or page number";continue;}
            lines++;digits+=l->digits;math+=l->math+l->bad;
            int q=((int)lroundf(atan2f(l->dy,l->dx)*2/3.14159265358979323846f)+4)%4;
            quadrants[q]+=l->n;
            box=rect_union(box,l->box);
        }
        for(int i=0;i<p->nmark;i++) {
            Mark *m=&p->marks[i];float cover=coverage(m->box,box);
            if(cover<.5f)continue;
            if(m->owner>=0){occupied++;continue;}
            if(rect_area(m->box)>.85f*rect_area(p->box)){reject="Full-page background";continue;}
            marks++;if(m->image || (m->box.x1-m->box.x0>p->body && m->box.y1-m->box.y0>p->body))drawings++;
            box=rect_union(box,m->box);
        }
        if(occupied)reject="Already covered by a containing source object";
        if(kind==PD_TABLE && (lines<4 || (digits<4 && marks<2)))reject="Insufficient source evidence for a table";
        if(kind==PD_FIGURE && !drawings)reject="No source image or drawing in region";
        if(kind==PD_EQUATION && !math)reject="No mathematical or unmapped source glyphs";
        // Expanding a crop must not cut through another text line. The source
        // representation wins when a region boundary cannot be resolved.
        for(int i=0;i<p->nline;i++) {
            Line *l=&p->lines[i];float cover=coverage(l->box,box);
            if(cover>.02f && cover<.55f)reject="Boundary intersects an unresolved text line";
            if(cover>=.55f && (l->margin || protected_text(p,l,h)))reject="Expanded boundary reaches prose";
        }
        if(reject){snprintf(h->decision,sizeof(h->decision),"%s",reject);continue;}
        Block *b=pd_add_block(d,p,kind,(PD_Rect){FLT_MAX,FLT_MAX,-FLT_MAX,-FLT_MAX},
            "Local model region; complete PDF glyphs and drawing bounds retained");
        b->pub.review=1;
        for(int i=0;i<p->nline;i++) {
            Line *l=&p->lines[i];
            if(l->owner<0 && coverage(l->box,box)>=.55f)pd_claim_line(d,p,b,i);
        }
        for(int i=0;i<p->nmark;i++) {
            Mark *m=&p->marks[i];
            if(m->owner<0 && coverage(m->box,box)>=.5f) {
                m->owner=(int)(b-p->blocks);b->pub.box=rect_union(b->pub.box,m->box);
            }
        }
        int all=quadrants[0]+quadrants[1]+quadrants[2]+quadrants[3];
        for(int q=1;q<4;q++)if(quadrants[q]>all*.8f)b->pub.rotation=(360-q*90)%360;
        b->pub.box=rect_pad(b->pub.box,1);
        h->applied=1;d->model_stats.applied++;
        snprintf(h->decision,sizeof(h->decision),"Accepted after source geometry checks; no generated content");
    }
}
