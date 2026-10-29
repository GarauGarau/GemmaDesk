#include "internal.h"
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void lower(char *out,size_t cap,const char *s,int digits) {
    size_t n=0;int gap=0;
    for(;*s && n+1<cap;s++) {
        unsigned char c=(unsigned char)*s;
        if(isspace(c) || (digits && isdigit(c))) {gap=n>0;continue;}
        if(gap && n+2<cap)out[n++]=' ';
        out[n++]=(char)tolower(c);gap=0;
    }
    out[n]=0;
}
int pd_publication_sheet(Page *p) {
    int source=0,rights=0,version=0,body=0,series=0,chars=0,frame=0;
    for(int i=0;i<p->nline;i++) {
        char s[2048];lower(s,sizeof(s),p->lines[i].text,0);
        chars+=p->lines[i].n;
        if(strstr(s,"working paper") || strstr(s,"discussion paper"))series=1;
        if(!strncmp(s,"abstract",8) || strstr(s,"1 introduction") || strstr(s,"1. introduction"))body=1;
        if(strstr(s,"stable url:") || strstr(s,"published by:") || strstr(s,"author(s):") || strstr(s,"repository"))source=1;
        if(strstr(s,"terms") || strstr(s,"copyright") || strstr(s,"rights") || strstr(s,"licence"))rights=1;
        if(strstr(s,"version:") || strstr(s,"accepted version") || strstr(s,"linked references") || strstr(s,"this document is"))version=1;
    }
    if(!body && source && rights && version)return 1;
    for(int i=0;i<p->nmark;i++)if(rect_area(p->marks[i].box)>rect_area(p->box)*.5f)frame=1;
    return !body && series && frame && chars<2000?2:0;
}
static int repeating(Document *d,Page *p,Line *l,int diagonal) {
    char key[2048];lower(key,sizeof(key),l->text,1);int matches=0;
    float y=(l->box.y0-p->box.y0)/(p->box.y1-p->box.y0);
    for(int pi=0;pi<d->npage;pi++) {
        Page *q=&d->pages[pi];if(q==p || q->front_sheet)continue;
        if(abs(q->number-p->number)>6)continue;
        for(int j=0;j<q->nline;j++) {
            Line *t=&q->lines[j];char other[2048];
            if(t->letters<6 || fabsf(t->size-l->size)>l->size*.15f || fabsf(t->dy-l->dy)>.05f)continue;
            float ty=(t->box.y0-q->box.y0)/(q->box.y1-q->box.y0);
            if(!diagonal && fabsf(ty-y)>.016f)continue;
            lower(other,sizeof(other),t->text,1);
            if(!strcmp(key,other)){matches++;break;}
        }
        if(matches>=(diagonal?2:1))return 1;
    }
    return 0;
}
void pd_page_details(Document *d,Page *p) {
    float w=p->box.x1-p->box.x0,h=p->box.y1-p->box.y0;
    float title_y=p->box.y0,title_size=0;
    for(int i=0;i<p->nline;i++)if(p->lines[i].front==1) {
        if(!title_size || p->lines[i].box.y0<title_y)title_y=p->lines[i].box.y0;
        title_size=fmaxf(title_size,p->lines[i].size);
    }
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];if(l->owner>=0 || l->margin || l->caption || l->front || l->bad || l->letters<6)continue;
        float y=(l->box.y0-p->box.y0)/h,x=((l->box.x0+l->box.x1)*.5f-p->box.x0)/w;
        char s[2048];lower(s,sizeof(s),l->text,0);
        int peers=0;
        for(int j=0;j<p->nline;j++) {
            Line *t=&p->lines[j];
            if(t!=l && t->n && !t->margin && fabsf(t->baseline-l->baseline)<p->body*.3f &&
               rect_x_overlap(t->box,l->box)<=0)peers++;
        }
        int header=peers<3 && fabsf(l->dy)<.05f && (y<.11f || y>.88f) && l->size<=p->body*1.2f && repeating(d,p,l,0);
        int masthead=title_size && l->box.y1<title_y && l->size<title_size*.8f && fabsf(l->dy)<.05f;
        int imprint=y>.85f && l->size<p->body*.95f &&
            (!strncmp(s,"doi:",4) || strstr(s,"issn") || strstr(s,"©") || !strncmp(s,"copyright",9) ||
             (strstr(s,"vol.") && (strstr(s,"journal") || strstr(s,"review"))));
        int vertical=fabsf(l->dy)>.95f && (x<.08f || x>.92f) && l->letters>8 && l->n>24 &&
            l->box.x1-l->box.x0<p->body*2.5f &&
            (strstr(s,"http") || strstr(s,"arxiv:") || strstr(s,"disclosure") || strstr(s,"downloaded"));
        int watermark=fabsf(l->dy)>.25f && fabsf(l->dy)<.95f && l->size>p->body*2 &&
            l->box.x1-l->box.x0>w*.4f && l->box.y1-l->box.y0>h*.4f && repeating(d,p,l,1);
        if(!header && !vertical && !watermark && !masthead && !imprint)continue;
        Block *b=pd_add_block(d,p,PD_DETAIL,l->box,watermark?
            "Repeated diagonal publication watermark; text retained in publication details":vertical?
            "Marginal publication or download notice; retained in publication details":
            masthead || imprint?"Journal imprint or publication masthead; retained in publication details":
            "Repeated running header or footer; retained in publication details");
        l->heading=0;pd_claim_line(d,p,b,i);
        if(imprint) {
            Line *last=l;
            for(int j=i+1;j<p->nline;j++) {
                Line *t=&p->lines[j];
                if(t->box.y0>last->box.y1+p->body)break;
                if(t->owner>=0 || t->margin || t->caption || t->front || t->bad || !t->letters ||
                   fabsf(t->size-l->size)>.4f || rect_x_overlap(t->box,l->box)<=0)continue;
                char label[16];if(pd_note_label(t,label))break;
                t->heading=0;pd_claim_line(d,p,b,j);last=t;
            }
        }
    }
}
