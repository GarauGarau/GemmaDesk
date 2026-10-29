#include "internal.h"
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

void pd_metadata_text(char *out,size_t cap,const char *text) {
    size_t n=0;int space=0;
    for(const char *s=text;*s;) {
        int rune;int bytes=fz_chartorune(&rune,s);
        if(s[0]=='&' && s[1]=='#') {
            const char *number=s+2;int base=10;if(*number=='x' || *number=='X'){base=16;number++;}
            char *end;unsigned long value=strtoul(number,&end,base);
            if(end>number && end-number<=8 && *end==';' && value<=0x10ffff && pd_xml_scalar((int)value)) {rune=(int)value;bytes=(int)(end-s)+1;}
        }
        s+=bytes;
        if(rune<=32){if(n)space=1;continue;}
        if(!pd_xml_scalar(rune))continue;
        char encoded[8];int count=fz_runetochar(encoded,rune);
        if(n+(size_t)count+space>=cap)break;
        if(space)out[n++]=' ';
        memcpy(out+n,encoded,(size_t)count);n+=(size_t)count;space=0;
    }
    out[n]=0;
}
static int all_caps(const char *text) {
    int upper=0,lower=0;
    for(const unsigned char *s=(const unsigned char*)text;*s;s++) {
        if(isupper(*s))upper++;
        if(islower(*s))lower++;
    }
    return upper>5 && !lower;
}
static int metadata_usable(const char *text) {
    char low[512];size_t i;
    for(i=0;text[i] && i<sizeof(low)-1;i++)low[i]=(char)tolower((unsigned char)text[i]);
    low[i]=0;
    const char *bad[]={"microsoft ","untitled",".doc",".tex",".pdf","unknown","anonymous","latex","iso 15930","electronic document file format"};
    for(i=0;i<sizeof(bad)/sizeof(*bad);i++)if(strstr(low,bad[i]))return 0;
    if(strstr(low," document") && strlen(low)<32)return 0;
    return strchr(text,' ') && strlen(text)>8;
}
static int title_line(Line *l,Page *p,float body) {
    const char *s=l->text;if(!s || l->letters<8 || l->words<2 || l->words>28 || l->bad || fabsf(l->dy)>.1f)return 0;
    if(l->box.y0<p->box.y0+(p->box.y1-p->box.y0)*.05f || l->box.y1>p->box.y0+(p->box.y1-p->box.y0)*.55f)return 0;
    if(l->box.y0<p->box.y0+(p->box.y1-p->box.y0)*.12f && l->size<body*1.3f)return 0;
    const char *skip[]={"Abstract","Introduction","Contents","Table ","Figure ","Working Paper ","Discussion Paper ","Keywords","JEL ","http", "©","Appendix","Appendices","References","Bibliography","Supplementary"};
    for(size_t i=0;i<sizeof(skip)/sizeof(*skip);i++)if(!strncasecmp(s,skip[i],strlen(skip[i])))return 0;
    if(isdigit((unsigned char)*s) || strchr(s,'@'))return 0;
    if(l->size<body*1.10f && !all_caps(s))return 0;
    /* A large table cell or later section is not an article title. The
     * title precedes captions, the abstract and ordinary sustained prose. */
    for(int i=0;i<p->nline;i++) {
        Line *t=&p->lines[i];if(t==l || t->box.y1>=l->box.y0 || fabsf(t->dy)>.1f)continue;
        if(!strncasecmp(t->text,"Abstract",8) || !strncasecmp(t->text,"Table ",6) || !strncasecmp(t->text,"Figure ",7))return 0;
        if(t->box.y0>p->box.y0+(p->box.y1-p->box.y0)*.12f &&
           t->letters>35 && t->words>=8 && t->size>=body*.8f && t->size<=body*1.2f &&
           t->bold<t->n*.5f && !all_caps(t->text) && t->box.x1-t->box.x0>(p->box.x1-p->box.x0)*.4f)return 0;
    }
    return 1;
}
static void append_line(char *out,size_t cap,const char *text,const char *separator) {
    size_t used=strlen(out);if(used && used+strlen(separator)<cap) {
        memcpy(out+used,separator,strlen(separator)+1);used+=strlen(separator);
    }
    if(used<cap)pd_metadata_text(out+used,cap-used,text);
}
static int date_line(Line *l) {
    if(l->digits<4 || l->words>5)return 0;
    if(!l->letters)return 1;
    const char *month[]={"January","February","March","April","May","June","July","August","September","October","November","December"};
    for(size_t i=0;i<sizeof(month)/sizeof(*month);i++)if(strstr(l->text,month[i]))return 1;
    return 0;
}
static int names_line(const char *s) {
    int names=0,other=0;
    while(*s) {
        while(isspace((unsigned char)*s))s++;
        if(!*s)break;
        const char *start=s;while(*s && !isspace((unsigned char)*s))s++;
        char word[32];size_t n=(size_t)(s-start);if(n>=sizeof(word))return 0;
        for(size_t i=0;i<n;i++)word[i]=(char)tolower((unsigned char)start[i]);
        word[n]=0;
        if(!strcmp(word,"and") || !strcmp(word,"by") || !strcmp(word,"de") || !strcmp(word,"del") ||
           !strcmp(word,"van") || !strcmp(word,"von") || !strcmp(word,"da") || !strcmp(word,"di"))continue;
        if(isupper((unsigned char)*start) || (unsigned char)*start>=128)names++;else other++;
    }
    return names>=2 && other<=1;
}
/* Metadata uses source glyph positions, not OCR guesses or publisher names.
 * Raised affiliation markers and synthetic kerning gaps are not name text. */
static void metadata_line(Line *l,char out[512]) {
    size_t n=0;
    for(int i=0;i<l->n && n+8<512;i++) {
        Glyph *g=&l->glyph[i];int c=g->c;
        if(c=='*' || c==0x2217 || c==0x2020 || c==0x2021 ||
           (g->size<l->size*.85f && g->baseline<l->baseline-l->size*.1f))continue;
        if(c==' ' && i>0 && i+1<l->n &&
           ((g->flags&G_SYNTHETIC) || g->box.x1>l->glyph[i+1].box.x0+l->size*.08f) &&
           l->glyph[i+1].box.x0-l->glyph[i-1].box.x1<l->size*.13f)continue;
        if(g->flags&G_GAP && n && out[n-1]!=' ')out[n++]=' ';
        n+=(size_t)fz_runetochar(out+n,c);
    }
    out[n]=0;char clean[512];pd_metadata_text(clean,sizeof(clean),out);
    const char *start=clean;while(*start && strchr(" ,;",*start))start++;
    snprintf(out,512,"%s",start);n=strlen(out);
    while(n && strchr(" ,;",out[n-1]))out[--n]=0;
}
static int institutional(const char *s) {
    char low[512];size_t n=0;for(;s[n] && n<sizeof(low)-1;n++)low[n]=(char)tolower((unsigned char)s[n]);low[n]=0;
    const char *roles[]={"universit","universidad","institute","institution","department","school","college","faculty","facultad","faculte","directorate","centre","center","laborator","commission","research group","research council","working paper","journal","editorial","received","accepted","version","http","@"};
    for(size_t i=0;i<sizeof(roles)/sizeof(*roles);i++)if(strstr(low,roles[i]))return 1;
    return 0;
}
static int abstract_heading(const char *s) {
    char key[32];int n=0;
    for(;*s && n<31;s++)if(isalpha((unsigned char)*s))key[n++]=(char)tolower((unsigned char)*s);
    key[n]=0;return !strcmp(key,"abstract");
}
static int author_text(Line *l,char out[512]) {
    if(l->front!=2 || l->letters<4 || l->words>18 || date_line(l))return 0;
    metadata_line(l,out);
    if(institutional(out))return 0;
    const char *s=out;if(!strncasecmp(s,"By ",3))s+=3;else if(!strncasecmp(s,"and ",4))s+=4;
    if(s!=out)memmove(out,s,strlen(s)+1);
    if(!all_caps(out)) {
        int words=0;
        for(char *start=out;*start;) {
            while(isspace((unsigned char)*start))start++;
            char *end=start;int capitals=0,other=0;
            while(*end && !isspace((unsigned char)*end)){if(isupper((unsigned char)*end))capitals++;else other++;end++;}
            if(words>=2 && capitals>=2 && capitals<=8 && !other && strncasecmp(start,"AND",3)){*start=0;break;}
            if(end>start)words++;
            start=end;
        }
    }
    if(institutional(out))return 0;
    for(const unsigned char *c=(const unsigned char*)out;*c;c++)if(isdigit(*c))return 0;
    int tokens=0,short_acronyms=0;const char *word=out;
    while(*word){while(isspace((unsigned char)*word))word++;const char *end=word;int upper=0,lower=0;while(*end && !isspace((unsigned char)*end)){upper+=isupper((unsigned char)*end)!=0;lower+=islower((unsigned char)*end)!=0;end++;}if(end>word){tokens++;if(!lower && upper>=2 && upper<=5)short_acronyms++;}word=end;}
    if(tokens && short_acronyms==tokens)return 0;
    return names_line(out);
}
static void front_authors(Document *d,Page *p) {
    Line *names[64];int count=0;
    for(int i=0;i<p->nline && count<64;i++) {
        char text[512];Line *l=&p->lines[i];if(!author_text(l,text))continue;
        int j=count++;
        while(j && (names[j-1]->baseline>l->baseline+l->size*.3f ||
              (fabsf(names[j-1]->baseline-l->baseline)<l->size*.3f && names[j-1]->box.x0>l->box.x0))) {
            names[j]=names[j-1];j--;
        }
        names[j]=l;
    }
    char authors[512]={0};
    float author_size=count?names[0]->size:0;
    int bold_names=0;for(int i=0;i<count;i++)bold_names+=names[i]->bold>names[i]->n*.6f;
    for(int i=0;i<count;i++) {
        char text[512];
        if(fabsf(names[i]->size-author_size)>author_size*.15f)continue;
        if(bold_names>=2 && names[i]->bold<names[i]->n*.5f)continue;
        if(author_text(names[i],text))append_line(authors,sizeof(authors),text,"; ");
    }
    /* Embedded authors may contain a complete, multi-page byline. Keep
     * them only when every name token is present near the article start. */
    int supported=d->author[0]!=0,words=0;char token[128];
    for(const char *s=d->author;*s && supported;) {
        while(*s && !isalpha((unsigned char)*s) && (unsigned char)*s<128)s++;
        int n=0;
        while(*s && (isalpha((unsigned char)*s) || (unsigned char)*s>=128)) {
            if(n<127)token[n++]=(char)tolower((unsigned char)*s);
            s++;
        }
        token[n]=0;if(n<2 || !strcmp(token,"and"))continue;words++;
        int found=0;
        for(int pi=(int)(p-d->pages);pi<d->npage && pi<=(int)(p-d->pages)+2 && !found;pi++)
            for(int li=0;li<d->pages[pi].nline;li++) {
                char text[512];metadata_line(&d->pages[pi].lines[li],text);
                for(char *c=text;*c;c++)*c=(char)tolower((unsigned char)*c);
                if(strstr(text,token)){found=1;break;}
            }
        supported=found;
    }
    int extracted_words=0;for(const char *s=authors;*s;s++)if(*s==' ' || *s==';')extracted_words++;
    if(authors[0] && (!supported || extracted_words>words+1))snprintf(d->author,sizeof(d->author),"%s",authors);
    else if(!supported)d->author[0]=0;
}
void pd_book_metadata(Document *d) {
    char cleaned[512];pd_metadata_text(cleaned,sizeof(cleaned),d->title);snprintf(d->title,sizeof(d->title),"%s",cleaned);
    if(!metadata_usable(d->title))d->title[0]=0;
    if(!metadata_usable(d->author))d->author[0]=0;
    if(d->npage<1 || d->pages[0].number!=0)return;
    Page *p=NULL;int best=-1;float score=-1;
    for(int pi=0;pi<d->npage;pi++) {
        Page *q=&d->pages[pi];q->front_sheet=pd_publication_sheet(q);
        if(p && q->number>p->number+2 && score>180)break;
        if(q->front_sheet==1)continue;
        for(int i=0;i<q->nline;i++) {
            Line *l=&q->lines[i];
            if(!title_line(l,q,d->body))continue;
            float centred=fabsf((l->box.x0+l->box.x1-q->box.x0-q->box.x1)/2)/(q->box.x1-q->box.x0);
            float value=l->size/d->body*50+(all_caps(l->text)?12:0)+(centred<.06f?8:0)-l->box.y0/(q->box.y1-q->box.y0)*30-pi*15;
            char line[512],title[512];pd_metadata_text(line,sizeof(line),l->text);pd_metadata_text(title,sizeof(title),d->title);
            for(char *c=line;*c;c++)*c=(char)tolower((unsigned char)*c);
            for(char *c=title;*c;c++)*c=(char)tolower((unsigned char)*c);
            size_t length=strlen(line);while(length && strchr(" *",line[length-1]))line[--length]=0;
            if(length>12 && strstr(title,line))value+=180;
            int has_abstract=0;for(int k=0;k<q->nline;k++)if(abstract_heading(q->lines[k].text))has_abstract=1;
            if(has_abstract)value+=25;
            /* Cover citations do not establish an article byline. */
            int later_names=0;
            for(int k=0;k<q->nline;k++){Line *t=&q->lines[k];if(t->box.y0>l->box.y1 && t->box.y0<l->box.y1+l->size*8 && t->words>=2 && t->words<12 && !institutional(t->text) && names_line(t->text))later_names++;}
            if(later_names)value+=30;else value-=60;
            if(value>score){score=value;best=i;p=q;}
        }
        int main_started=0;
        for(int i=0;i<q->nline;i++) {
            Line *l=&q->lines[i];const char *s=l->text;
            if(s && *s=='1' && (s[1]=='.' || isspace((unsigned char)s[1])) &&
               l->words>=2 && l->words<12 && (l->bold>l->n*.5f || l->size>d->body*1.1f))main_started=1;
        }
        if(main_started)break;
    }
    if(best<0)return;
    Line *first=&p->lines[best];PD_Rect title_box=first->box;
    int selected[16],count=1;selected[0]=best;
    // Join adjacent title lines of the same typography, in source order.
    for(int step=0;step<15;step++) {
        int next=-1;float gap=first->size*1.3f;
        for(int i=0;i<p->nline;i++) {
            Line *l=&p->lines[i];float distance=l->box.y0-title_box.y1;
            int subtitle=count==1 && distance>=0 && distance<first->size*.75f && l->words>=6 && l->words<=16 &&
                l->size>=first->size*.78f && l->size<first->size && !l->bad && !strchr(l->text,',') && strncasecmp(l->text,"By ",3) &&
                fabsf(l->box.x0+l->box.x1-title_box.x0-title_box.x1)<first->size*2;
            int tail=l->letters>=3 && l->words<=5 && !l->bad && l->size>d->body*1.25f &&
                distance<first->size*.7f && l->baseline>p->lines[selected[count-1]].baseline+first->size*.7f;
            int title_fragment=subtitle || tail || title_line(l,p,d->body) || (l->n<20 && l->words<4 && l->digits && !l->bad);
            if(distance<-first->size*.2f || distance>=gap || !title_fragment ||
               l->baseline<=p->lines[selected[count-1]].baseline+first->size*.7f ||
               (!subtitle && fabsf(l->size-first->size)>first->size*.08f) || (l->letters && all_caps(l->text)!=all_caps(first->text)))continue;
            next=i;gap=distance;
        }
        if(next<0)break;
        selected[count++]=next;title_box=rect_union(title_box,p->lines[next].box);
    }
    char candidate[512]={0};for(int i=0;i<count;i++)append_line(candidate,sizeof(candidate),p->lines[selected[i]].text," ");
    /* Raised note markers belong to the source title, not library metadata. */
    for(char *end=candidate+strlen(candidate);end>candidate;) {
        const char *start=end-1;while(start>candidate && ((unsigned char)*start&0xc0)==0x80)start--;
        int c;fz_chartorune(&c,start);
        if(c=='*' || c==0x2217 || c==0x2020 || c==0x2021 || c==' ') {*((char*)start)=0;end=(char*)start;}else break;
    }
    if(!metadata_usable(d->title))snprintf(d->title,sizeof(d->title),"%s",candidate);
    size_t title_length=strlen(d->title);
    while(title_length && (d->title[title_length-1]=='*' || d->title[title_length-1]==' '))d->title[--title_length]=0;
    for(int i=0;i<count;i++)p->lines[selected[i]].front=1;
    if(!metadata_usable(d->author))d->author[0]=0;
    float end_y=p->box.y1;
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];if(l->box.y0<=title_box.y1 || fabsf(l->dy)>.05f)continue;
        int prose=l->words>=12 && l->letters>=55 && !all_caps(l->text) && !names_line(l->text) && !institutional(l->text);
        if(abstract_heading(l->text) || prose)end_y=fminf(end_y,l->box.y0);
    }
    for(int i=0;i<p->nline;i++) {
        Line *l=&p->lines[i];
        if(l->box.y0>title_box.y1 && l->box.y1<end_y && fabsf(l->dy)<.05f)l->front=2;
    }
    front_authors(d,p);
    for(int i=0;i<p->nline;i++) {
        const char *s=p->lines[i].text;
        if(s && (!strncasecmp(s,"Working Paper ",14) || !strncasecmp(s,"Discussion Paper ",17)) && p->lines[i].digits)
            pd_metadata_text(d->reference,sizeof(d->reference),s);
    }
}
int pd_book_info(const char *path,PD_BookInfo *info,char *error,size_t size) {
    memset(info,0,sizeof(*info));Document d={0};PD_Options opt={0};opt.first_page=1;d.opt=&opt;fz_var(d);
    d.ctx=fz_new_context(NULL,NULL,32<<20);if(!d.ctx){snprintf(error,size,"Not enough memory");return 0;}
    int ok=0;fz_var(ok);fz_register_document_handlers(d.ctx);
    fz_try(d.ctx) {
        d.pdf=fz_open_document(d.ctx,path);d.physical_pages=fz_count_pages(d.ctx,d.pdf);
        opt.last_page=d.physical_pages;
        fz_lookup_metadata(d.ctx,d.pdf,FZ_META_INFO_TITLE,d.title,sizeof(d.title));
        fz_lookup_metadata(d.ctx,d.pdf,FZ_META_INFO_AUTHOR,d.author,sizeof(d.author));
        pd_extract(&d);pd_book_metadata(&d);
        if(!d.title[0]) {const char *name=strrchr(path,'/');pd_metadata_text(d.title,sizeof(d.title),name?name+1:path);char *dot=strrchr(d.title,'.');if(dot)*dot=0;}
        pd_metadata_text(info->title,sizeof(info->title),d.title);pd_metadata_text(info->author,sizeof(info->author),d.author);
        pd_metadata_text(info->reference,sizeof(info->reference),d.reference);ok=1;
    }
    fz_catch(d.ctx)snprintf(error,size,"%s",fz_caught_message(d.ctx));
    pd_drop_document(&d);fz_drop_context(d.ctx);return ok;
}
static float text_width(fz_context *ctx,fz_font *font,const char *s,float size) {
    return fz_measure_string(ctx,font,fz_scale(size,-size),s,0,0,FZ_BIDI_LTR,FZ_LANG_UNSET).e;
}
static int wrap(fz_context *ctx,fz_font *font,const char *s,float size,float width,char lines[24][512]) {
    int n=0;
    while(*s && n<24) {
        while(*s==' ')s++;
        if(!*s)break;
        const char *p=s,*end=s,*space=NULL;
        while(*p) {
            int rune;int bytes=fz_chartorune(&rune,p);
            if(p-s+bytes>=511)break;
            memcpy(lines[n],s,(size_t)(p-s+bytes));lines[n][p-s+bytes]=0;
            if(text_width(ctx,font,lines[n],size)>width && end>s)break;
            if(rune==' ')space=p;
            p+=bytes;end=p;
        }
        if(*p && space && space>s)end=space;
        size_t len=(size_t)(end-s);memcpy(lines[n],s,len);lines[n][len]=0;n++;s=end;
    }
    return n;
}
static void lettering(fz_context *ctx,fz_device *dev,fz_font *font,const char *s,float size,float x,float y) {
    fz_text *text=fz_new_text(ctx);
    fz_try(ctx) {
        fz_show_string(ctx,text,font,(fz_matrix){size,0,0,-size,x,y},s,0,0,FZ_BIDI_LTR,FZ_LANG_UNSET);
        float ink[3]={.08f,.08f,.08f};fz_fill_text(ctx,dev,text,fz_identity,fz_device_rgb(ctx),ink,1,fz_default_color_params);
    }
    fz_always(ctx)fz_drop_text(ctx,text);
    fz_catch(ctx)fz_rethrow(ctx);
}
static void bar(fz_context *ctx,fz_device *dev,float x,float y,float w,float h) {
    fz_path *path=fz_new_path(ctx);
    fz_try(ctx) {
        fz_rectto(ctx,path,x,y,x+w,y+h);float ink[3]={.12f,.12f,.12f};
        fz_fill_path(ctx,dev,path,0,fz_identity,fz_device_rgb(ctx),ink,1,fz_default_color_params);
    }
    fz_always(ctx)fz_drop_path(ctx,path);
    fz_catch(ctx)fz_rethrow(ctx);
}
fz_buffer *pd_cover_png(Document *d) {
    fz_context *ctx=d->ctx;fz_pixmap *pix=NULL;fz_device *dev=NULL;fz_font *title=NULL,*small=NULL;fz_buffer *png=NULL;
    fz_var(pix);fz_var(dev);fz_var(title);fz_var(small);fz_var(png);
    fz_try(ctx) {
        pix=fz_new_pixmap(ctx,fz_device_rgb(ctx),1200,1600,NULL,0);fz_clear_pixmap_with_value(ctx,pix,255);
        dev=fz_new_draw_device(ctx,fz_identity,pix);
        title=fz_new_base14_font(ctx,"Times-Bold");small=fz_new_base14_font(ctx,"Helvetica");
        bar(ctx,dev,106,125,988,9);bar(ctx,dev,106,140,150,3);
        if(d->reference[0])lettering(ctx,dev,small,d->reference,27,106,198);
        char lines[24][512];float size=84;int count;
        do {count=wrap(ctx,title,d->title,size,984,lines);if(count*size*1.12f<=740)break;size-=2;}while(size>28);
        float y=410;
        for(int i=0;i<count;i++){lettering(ctx,dev,title,lines[i],size,104,y);y+=size*1.12f;}
        if(d->author[0]) {
            float author_size=34;int authors;
            do {authors=wrap(ctx,small,d->author,author_size,984,lines);if(authors*author_size*1.4f<260)break;author_size-=2;}while(author_size>18);
            y=fmaxf(1190,y+85);
            for(int i=0;i<authors;i++){lettering(ctx,dev,small,lines[i],author_size,108,y);y+=author_size*1.4f;}
        }
        bar(ctx,dev,106,1480,988,2);fz_close_device(ctx,dev);
        png=fz_new_buffer_from_pixmap_as_png(ctx,pix,fz_default_color_params);
    }
    fz_always(ctx){fz_drop_font(ctx,title);fz_drop_font(ctx,small);fz_drop_device(ctx,dev);fz_drop_pixmap(ctx,pix);}
    fz_catch(ctx){fz_drop_buffer(ctx,png);fz_rethrow(ctx);}
    return png;
}
