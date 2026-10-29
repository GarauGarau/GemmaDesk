/* Source-symbol layout trees. Fraction/radical decomposition follows INFTY;
 * recursive baseline and script regions follow the BST approach of DRACULAE.
 * This is a bounded subset, not an OCR system or a trained relation model.
 * Every terminal retains its PDF glyph; every rule must be consumed. */
#include "internal.h"
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>

/* These TeX alphabets encode mathematical variants as ASCII capitals.
 * Match the actual family and numeric design size, not a name substring.
 * Unicode mathematical alphabet characters already carry their own style. */
int pd_math_font_variant(const char *name) {
    const char *plus=strchr(name,'+');
    if(plus && plus-name==6)name=plus+1;
    const char *families[]={"msbm","cmsy","rsfs"};
    for(int i=0;i<3;i++)if(!strncasecmp(name,families[i],4)) {
        const char *size=name+4;if(!isdigit((unsigned char)*size))continue;
        while(isdigit((unsigned char)*size))size++;
        if(!*size)return i==0?G_DOUBLE_STRUCK:G_SCRIPT;
    }
    return 0;
}
void pd_math_glyph(fz_context *ctx,fz_buffer *out,const Glyph *g) {
    int c=g->c,alpha=(c<128 && isalpha(c)) || (c>=0x370 && c<=0x3ff);
    const char *style=g->flags&G_DOUBLE_STRUCK?"\\mathbb{":g->flags&G_SCRIPT?"\\mathcal{":
        g->flags&G_BOLD?(g->flags&G_ITALIC?"\\boldsymbol{":"\\mathbf{"):
        alpha?(g->flags&G_ITALIC?"\\mathit{":"\\mathrm{"):NULL;
    if(style)fz_append_string(ctx,out,style);
    if(c<128 && strchr("{}%$&#_",c))fz_append_byte(ctx,out,'\\');
    pd_utf8(ctx,out,c);
    if(style)fz_append_byte(ctx,out,'}');
}

/* Unicode spacing and combining forms of the same mathematical accent.
 * This mapping is used only in an ABOVE relation, never for baseline text. */
int pd_math_accent(int c) {
    switch(c) {
    case '^':case 0x2c6:case 0x302:return '^';
    case '~':case 0x2dc:case 0x303:return '~';
    case 0xaf:case 0x304:case 0x305:return 0xaf;
    case 0x2d9:case 0x307:return 0x2d9;
    case 0xa8:case 0x308:return 0xa8;
    case 0x2c7:case 0x30c:return 0x2c7;
    case 0x2d8:case 0x306:return 0x2d8;
    case 0x20d7:return 0x2192;
    default:return 0;
    }
}

enum { LEAF, BAR, ROW, FRAC, ROOT, SCRIPTS, ACCENT, TABLE, TABLE_ROW, FENCED };
enum { SYMBOL_LIMIT=1024, NODE_LIMIT=4096, DEPTH_LIMIT=32 };
typedef struct {
    int kind,child,next,sub,sup,limits,spaced;
    const Glyph *glyph;
    PD_Rect box;
    float size,baseline;
} Node;
typedef struct {
    fz_context *ctx;
    Node *nodes;
    const Glyph **order;
    int n,ok,nglyph,norder;
    unsigned work;
} Tree;

static int budget(Tree *t) {
    if(++t->work>2000000)t->ok=0;
    return t->ok;
}
static int add(Tree *t,Node node) {
    if(t->n==NODE_LIMIT){t->ok=0;return 0;}
    t->nodes[t->n]=node;return t->n++;
}
static float midx(PD_Rect b){return (b.x0+b.x1)*.5f;}
static float midy(PD_Rect b){return (b.y0+b.y1)*.5f;}
static int limit_symbol(Node *n) {
    int c=n->glyph?n->glyph->c:0;
    return c==0x2211 || c==0x220f || c==0x2210 || c==0x22c3 || c==0x22c2;
}
static int labelled_arrow(Node *n) {
    int c=n->glyph?n->glyph->c:0;
    return c>=0x2190 && c<=0x21ff;
}
static int large_operator(Node *n) {
    int c=n->glyph?n->glyph->c:0;
    return limit_symbol(n) || (c>=0x222b && c<=0x2233);
}
/* Stable geometric ordering, independent of PDF drawing order. */
static void sort_x(Tree *t,int *ids,int n) {
    for(int i=1;i<n;i++) {
        int id=ids[i],j=i;Node *a=&t->nodes[id];
        while(j>0 && budget(t)) {
            Node *b=&t->nodes[ids[j-1]];
            if(b->box.x0<a->box.x0 || (b->box.x0==a->box.x0 && b->baseline<=a->baseline))break;
            ids[j]=ids[j-1];j--;
        }
        ids[j]=id;
    }
}
static int parse(Tree *t,int *ids,int n,int depth);
static int accent_support(Node *a,Node *b) {
    return (midx(b->box)>=a->box.x0 && midx(b->box)<=a->box.x1) ||
        (a->kind==LEAF && midx(a->box)>=b->box.x0 && midx(a->box)<=b->box.x1);
}

/* Accents have a source glyph (or an explicit overline) and a nonempty
 * horizontal support below it. Keep the base baseline for later scripts. */
static int accents(Tree *t,int *ids,int *count,int depth) {
    int n=*count;
    for(int ai=0;ai<n && budget(t);ai++) {
        Node *a=&t->nodes[ids[ai]];
        int code=a->kind==BAR?0xaf:a->kind==LEAF?pd_math_accent(a->glyph->c):0;
        if(!code)continue;
        int inside[SYMBOL_LIMIT],outside[SYMBOL_LIMIT],ni=0,no=0,above=0;
        float closest=1e20f;
        for(int i=0;i<n;i++)if(i!=ai) {
            Node *b=&t->nodes[ids[i]];
            if(b->kind==BAR || (b->kind==LEAF && pd_math_accent(b->glyph->c)))continue;
            if(!accent_support(a,b))continue;
            if(midy(b->box)<=midy(a->box))above=1;
            else closest=fminf(closest,b->baseline);
        }
        /* A rule with content above it is a fraction, not an overline. */
        if(above || closest==1e20f)continue;
        for(int i=0;i<n;i++)if(i!=ai) {
            Node *b=&t->nodes[ids[i]];
            if(accent_support(a,b) &&
               midy(b->box)>midy(a->box) && fabsf(b->baseline-closest)<=b->size*.18f &&
               b->box.y0-a->box.y1<b->size*.5f && b->baseline-a->box.y1<=b->size*1.3f)
                inside[ni++]=ids[i];
            else outside[no++]=ids[i];
        }
        if(!ni)continue;
        int base=parse(t,inside,ni,depth+1);if(!base)return 0;
        Node item=t->nodes[base];item.kind=ACCENT;item.child=base;item.sub=code;
        item.glyph=a->glyph;item.box=rect_union(item.box,a->box);item.next=0;
        outside[no++]=add(t,item);memcpy(ids,outside,(size_t)no*sizeof(int));*count=no;
        return 1;
    }
    return 0;
}

/* A radical's vinculum defines the horizontal extent of CONTAINS. A bare
 * radical without that evidence is deliberately left unresolved. */
static int radical(Tree *t,int *ids,int *count,int depth) {
    int n=*count;
    for(int ri=0;ri<n && budget(t);ri++) {
        Node *root=&t->nodes[ids[ri]];
        if(root->kind!=LEAF || root->glyph->c!=0x221a)continue;
        int bar=-1;
        for(int bi=0;bi<n && budget(t);bi++) {
            Node *b=&t->nodes[ids[bi]];
            if(b->kind==BAR && b->box.x0>=root->box.x0 && b->box.x0<=root->box.x1+root->size*.2f &&
               b->box.y0<=midy(root->box) && b->box.y1>=root->box.y0-root->size*.3f) {
                if(bar>=0)return 0;
                bar=bi;
            }
        }
        if(bar<0)continue;
        Node *b=&t->nodes[ids[bar]];
        int inside[SYMBOL_LIMIT],outside[SYMBOL_LIMIT],ni=0,no=0;
        for(int i=0;i<n && budget(t);i++) {
            if(i==ri || i==bar)continue;
            PD_Rect box=t->nodes[ids[i]].box;
            if(midx(box)>=b->box.x0 && midx(box)<=b->box.x1 &&
               midy(box)>midy(b->box) && midy(box)<=root->box.y1)inside[ni++]=ids[i];
            else outside[no++]=ids[i];
        }
        if(!ni)continue;
        int content=parse(t,inside,ni,depth+1);if(!content)return 0;
        Node a=*root;a.kind=ROOT;a.child=content;
        a.box=rect_union(rect_union(root->box,b->box),t->nodes[content].box);
        a.size=t->nodes[content].size;a.baseline=t->nodes[content].baseline;
        outside[no++]=add(t,a);memcpy(ids,outside,(size_t)no*sizeof(int));*count=no;return 1;
    }
    return 0;
}

/* A fraction is an ABOVE / rule / BELOW partition. Recursing on the two
 * sets supports nested fractions without flattening their symbol order. */
static int fractions(Tree *t,int *ids,int *count,int depth) {
    int n=*count,bar=-1;float width=0;
    for(int i=0;i<n && budget(t);i++) {
        Node *a=&t->nodes[ids[i]];
        if(a->kind==BAR && a->box.x1-a->box.x0>width){width=a->box.x1-a->box.x0;bar=i;}
    }
    if(bar<0)return 0;
    Node *b=&t->nodes[ids[bar]];
    int above[SYMBOL_LIMIT],below[SYMBOL_LIMIT],outside[SYMBOL_LIMIT],na=0,nb=0,no=0;
    for(int i=0;i<n && budget(t);i++) {
        if(i==bar)continue;
        Node *a=&t->nodes[ids[i]];
        if(midx(a->box)>=b->box.x0 && midx(a->box)<=b->box.x1) {
            if(midy(a->box)<midy(b->box))above[na++]=ids[i];
            else below[nb++]=ids[i];
        } else outside[no++]=ids[i];
    }
    if(!na || !nb){t->ok=0;return 0;}
    int num=parse(t,above,na,depth+1),den=parse(t,below,nb,depth+1);
    if(!num || !den)return 0;
    Node *a=&t->nodes[num],*c=&t->nodes[den];
    float size=fmaxf(a->size,c->size);
    /* Separate equation rows cannot be collapsed into one fraction. */
    if(b->box.y0-a->box.y1>size || c->box.y0-b->box.y1>size){t->ok=0;return 0;}
    Node frac={.kind=FRAC,.child=num,.sub=den,.size=size,.baseline=midy(b->box)+size*.25f,
        .box=rect_union(b->box,rect_union(a->box,c->box))};
    outside[no++]=add(t,frac);memcpy(ids,outside,(size_t)no*sizeof(int));*count=no;return 1;
}
static int baseline(Tree *t,int *ids,int n,int depth) {
    sort_x(t,ids,n);
    int dominant=-1;
    for(int i=0;i<n;i++)if(!large_operator(&t->nodes[ids[i]]) &&
        (dominant<0 || t->nodes[ids[i]].size>t->nodes[ids[dominant]].size))dominant=i;
    if(dominant<0)dominant=0;
    Node *lead=&t->nodes[ids[dominant]];
    Line reference={.size=lead->size,.baseline=lead->baseline};
    int base[SYMBOL_LIMIT],owner[SYMBOL_LIMIT],side[SYMBOL_LIMIT],relation[SYMBOL_LIMIT],nb=0;
    for(int i=0;i<n;i++) {
        Node *a=&t->nodes[ids[i]];
        owner[i]=-1;side[i]=0;
        Glyph probe={.size=a->size,.baseline=a->baseline};
        /* A tall sum/integral has its own font origin. Its ink centre,
         * not that origin or font size, joins the surrounding baseline. */
        int operator_on_line=large_operator(a) && midy(a->box)>=lead->box.y0 && midy(a->box)<=lead->box.y1;
        if(operator_on_line || (!pd_glyph_script(&reference,&probe) &&
           fabsf(a->baseline-lead->baseline)<=lead->size*.18f))base[nb++]=i;
    }
    if(!nb){t->ok=0;return 0;}
    for(int i=0;i<n && budget(t);i++) {
        int main=0;for(int j=0;j<nb;j++)if(base[j]==i)main=1;
        if(main)continue;
        Node *a=&t->nodes[ids[i]];int parent=-1,limits=0;
        for(int j=0;j<nb;j++) {
            Node *b=&t->nodes[ids[base[j]]];
            float pad=labelled_arrow(b)?0:b->size*.2f;
            if((limit_symbol(b) || labelled_arrow(b)) && midx(a->box)>=b->box.x0-pad &&
               midx(a->box)<=b->box.x1+pad) {
                if(parent>=0){t->ok=0;return 0;}
                parent=j;limits=1;
            }
        }
        if(parent<0)for(int j=0;j<nb;j++) {
            Node *b=&t->nodes[ids[base[j]]];
            if(a->box.x0>=b->box.x0 && a->box.x0>=b->box.x1-b->size*.3f)parent=j;
        }
        if(parent<0){t->ok=0;return 0;}
        Node *b=&t->nodes[ids[base[parent]]];
        if(a->size>=b->size*.95f || fabsf(a->baseline-b->baseline)<b->size*(a->baseline>b->baseline?.12f:.16f) ||
           (!limits && a->box.x0-b->box.x1>b->size)) {t->ok=0;return 0;}
        owner[i]=parent;side[i]=a->baseline<b->baseline?1:-1;relation[i]=limits;
    }
    Node row={.kind=ROW,.size=lead->size,.baseline=lead->baseline};
    int result=add(t,row),last=0;
    for(int j=0;j<nb && budget(t);j++) {
        int sub[SYMBOL_LIMIT],sup[SYMBOL_LIMIT],ns=0,nu=0,id=ids[base[j]],limits=-1;
        for(int i=0;i<n;i++)if(owner[i]==j) {
            if(limits>=0 && limits!=relation[i]){t->ok=0;return 0;}
            limits=relation[i];if(side[i]<0)sub[ns++]=ids[i];else sup[nu++]=ids[i];
        }
        if(ns || nu) {
            int lower=ns?parse(t,sub,ns,depth+1):0,upper=nu?parse(t,sup,nu,depth+1):0;
            if((ns && !lower) || (nu && !upper))return 0;
            Node a=t->nodes[id];a.kind=SCRIPTS;a.child=id;a.sub=lower;a.sup=upper;
            a.limits=limits;
            if(lower)a.box=rect_union(a.box,t->nodes[lower].box);
            if(upper)a.box=rect_union(a.box,t->nodes[upper].box);
            id=add(t,a);
        }
        if(last)t->nodes[last].next=id;else t->nodes[result].child=id;
        t->nodes[result].box=last?rect_union(t->nodes[result].box,t->nodes[id].box):t->nodes[id].box;last=id;
    }
    return t->ok?result:0;
}
static int relation(Node *n) {
    if(n->kind!=LEAF)return 0;
    int c=n->glyph->c;
    return c=='=' || c==0x2264 || c==0x2265 || c==0x2260 || c==0x2248 || c==0x2261;
}
/* A system of displayed equations has full-size relation signs on distinct
 * baselines. Partition around those baselines before interpreting fraction
 * bars, so a bar on one line cannot absorb symbols from another line. */
static int equation_rows(Tree *t,int *ids,int n,int depth) {
    float size=0,ys[32];int nr=0;
    for(int i=0;i<n;i++)if(!large_operator(&t->nodes[ids[i]]))size=fmaxf(size,t->nodes[ids[i]].size);
    for(int i=0;i<n;i++) {
        Node *a=&t->nodes[ids[i]];
        if(!relation(a) || a->size<size*.9f)continue;
        int j;for(j=0;j<nr && fabsf(ys[j]-a->baseline)>size*.18f;j++){}
        if(j==nr){if(nr==32)return 0;ys[nr++]=a->baseline;}
    }
    if(nr<2)return 0;
    for(int i=1;i<nr;i++){float y=ys[i];int j=i;while(j>0 && ys[j-1]>y){ys[j]=ys[j-1];j--;}ys[j]=y;}
    for(int i=1;i<nr;i++)if(ys[i]-ys[i-1]<size*1.4f)return 0;
    int rows[32][SYMBOL_LIMIT],counts[32]={0};
    for(int i=0;i<n;i++) {
        Node *a=&t->nodes[ids[i]];float y=a->kind==BAR?midy(a->box)+size*.25f:a->baseline;
        int row=0;for(int j=1;j<nr;j++)if(fabsf(y-ys[j])<fabsf(y-ys[row]))row=j;
        float top=row?(ys[row-1]+ys[row])*.5f-size*.25f:-1e20f;
        float bottom=row+1<nr?(ys[row]+ys[row+1])*.5f+size*.25f:1e20f;
        if(midy(a->box)<top || midy(a->box)>bottom)return 0;
        rows[row][counts[row]++]=ids[i];
    }
    Node table={.kind=TABLE,.size=size,.baseline=ys[0]};
    int result=add(t,table),last=0;
    for(int row=0;row<nr;row++) {
        int content=parse(t,rows[row],counts[row],depth+1);if(!content)return 0;
        int tr=add(t,(Node){.kind=TABLE_ROW,.child=content});
        if(last)t->nodes[last].next=tr;else t->nodes[result].child=tr;last=tr;
        t->nodes[result].box=row?rect_union(t->nodes[result].box,t->nodes[content].box):t->nodes[content].box;
    }
    return result;
}
/* Whitespace projection (XY-cut) inside explicit, tall paired delimiters.
 * Every rectangular cell must contain source content; cell trees are parsed
 * recursively. No missing entries are filled or inferred. */
static int bands(Tree *t,int *ids,int n,int vertical,float gap,float *cuts) {
    int order[SYMBOL_LIMIT];memcpy(order,ids,(size_t)n*sizeof(int));
    for(int i=1;i<n;i++) {
        int id=order[i],j=i;PD_Rect a=t->nodes[id].box;
        while(j>0 && budget(t)) {
            PD_Rect b=t->nodes[order[j-1]].box;
            if((vertical?b.y0:b.x0)<=(vertical?a.y0:a.x0))break;
            order[j]=order[j-1];j--;
        }
        order[j]=id;
    }
    float end=-1e20f;int count=1;
    for(int i=0;i<n;i++) {
        PD_Rect b=t->nodes[order[i]].box;float lo=vertical?b.y0:b.x0,hi=vertical?b.y1:b.x1;
        if(i && lo-end>gap){if(count==32)return 0;cuts[count-1]=(lo+end)*.5f;count++;}
        end=fmaxf(end,hi);
    }
    return count;
}
static int matrices(Tree *t,int *ids,int *count,int depth) {
    int n=*count;
    for(int li=0;li<n;li++) {
        Node *left=&t->nodes[ids[li]];int c=left->kind==LEAF?left->glyph->c:0;
        if(c!='[' && c!='(' && c!='{')continue;
        int close=c=='['?']':c=='('?')':'}',right=-1;
        for(int ri=0;ri<n;ri++) {
            Node *r=&t->nodes[ids[ri]];
            if(r->kind==LEAF && r->glyph->c==close && r->box.x0>left->box.x1 &&
               fabsf(r->box.y0-left->box.y0)<left->size*.2f && fabsf(r->box.y1-left->box.y1)<left->size*.2f &&
               (right<0 || r->box.x0<t->nodes[ids[right]].box.x0))right=ri;
        }
        if(right<0)continue;
        Node *r=&t->nodes[ids[right]];
        int inside[SYMBOL_LIMIT],outside[SYMBOL_LIMIT],ni=0,no=0;float size=0;
        for(int i=0;i<n;i++)if(i!=li && i!=right) {
            Node *a=&t->nodes[ids[i]];
            if(midx(a->box)>left->box.x1 && midx(a->box)<r->box.x0 &&
               midy(a->box)>=left->box.y0 && midy(a->box)<=left->box.y1) {
                inside[ni++]=ids[i];size=fmaxf(size,a->size);
            } else outside[no++]=ids[i];
        }
        if(ni<2 || left->box.y1-left->box.y0<size*1.8f)continue;
        float ycuts[32],xcuts[32];int nr=bands(t,inside,ni,1,size*.25f,ycuts),nc=bands(t,inside,ni,0,size*.6f,xcuts);
        if(nr<2 || !nc || nr*nc>ni)continue;
        int table=add(t,(Node){.kind=TABLE,.size=size,.baseline=(left->baseline+r->baseline)*.5f,.box=rect_union(left->box,r->box)}),last=0;
        for(int y=0;y<nr;y++) {
            int tr=add(t,(Node){.kind=TABLE_ROW}),previous=0;
            if(last)t->nodes[last].next=tr;else t->nodes[table].child=tr;last=tr;
            for(int x=0;x<nc;x++) {
                int cell[SYMBOL_LIMIT],nn=0;
                for(int i=0;i<ni;i++) {
                    PD_Rect b=t->nodes[inside[i]].box;int row=0,col=0;
                    while(row+1<nr && midy(b)>ycuts[row])row++;
                    while(col+1<nc && midx(b)>xcuts[col])col++;
                    if(row==y && col==x)cell[nn++]=inside[i];
                }
                if(!nn){t->ok=0;return 0;}
                int child=parse(t,cell,nn,depth+1);if(!child)return 0;
                if(previous)t->nodes[previous].next=child;else t->nodes[tr].child=child;previous=child;
            }
        }
        Node fenced=t->nodes[table];fenced.kind=FENCED;fenced.child=table;fenced.sub=ids[li];fenced.sup=ids[right];
        outside[no++]=add(t,fenced);memcpy(ids,outside,(size_t)no*sizeof(int));*count=no;return 1;
    }
    return 0;
}
static int parse(Tree *t,int *ids,int n,int depth) {
    if(!n || depth>DEPTH_LIMIT || !budget(t)){t->ok=0;return 0;}
    while(t->ok && matrices(t,ids,&n,depth)){}
    int rows=t->ok?equation_rows(t,ids,n,depth):0;
    if(rows)return rows;
    while(t->ok && radical(t,ids,&n,depth)){}
    while(t->ok && accents(t,ids,&n,depth)){}
    while(t->ok && fractions(t,ids,&n,depth)){}
    if(!t->ok)return 0;
    for(int i=0;i<n;i++)if(t->nodes[ids[i]].kind==BAR ||
        (t->nodes[ids[i]].kind==LEAF && (t->nodes[ids[i]].glyph->c==0x221a || pd_math_accent(t->nodes[ids[i]].glyph->c)))){t->ok=0;return 0;}
    if(n==1)return ids[0];
    return baseline(t,ids,n,depth);
}
static void terminal(Tree *t,const Glyph *g,fz_buffer *out) {
    if(t->norder>=t->nglyph){t->ok=0;return;}
    t->order[t->norder++]=g;
    pd_math_glyph(t->ctx,out,g);
}
static void emit(Tree *t,int id,fz_buffer *out,int depth) {
    if(!id || !budget(t) || depth>DEPTH_LIMIT*3){t->ok=0;return;}
    Node *n=&t->nodes[id];
    switch(n->kind) {
    case LEAF:terminal(t,n->glyph,out);break;
    case ROW:
        for(int c=n->child;c;c=t->nodes[c].next) {
            if(c!=n->child && t->nodes[c].spaced)fz_append_string(t->ctx,out,"\\,");
            emit(t,c,out,depth+1);
        }
        break;
    case FRAC:
        fz_append_string(t->ctx,out,"\\frac{");emit(t,n->child,out,depth+1);
        fz_append_string(t->ctx,out,"}{");emit(t,n->sub,out,depth+1);fz_append_byte(t->ctx,out,'}');break;
    case ROOT:
        fz_append_string(t->ctx,out,"\\sqrt{");emit(t,n->child,out,depth+1);fz_append_byte(t->ctx,out,'}');
        if(t->norder>=t->nglyph)t->ok=0;else t->order[t->norder++]=n->glyph;
        break;
    case SCRIPTS:
        fz_append_byte(t->ctx,out,'{');emit(t,n->child,out,depth+1);fz_append_byte(t->ctx,out,'}');
        if(n->limits)fz_append_string(t->ctx,out,"\\limits");
        else if(limit_symbol(&t->nodes[n->child]))fz_append_string(t->ctx,out,"\\nolimits");
        if(n->sub){fz_append_string(t->ctx,out,"_{");emit(t,n->sub,out,depth+1);fz_append_byte(t->ctx,out,'}');}
        if(n->sup){fz_append_string(t->ctx,out,"^{");emit(t,n->sup,out,depth+1);fz_append_byte(t->ctx,out,'}');}
        break;
    case ACCENT: {
        const char *cmd=n->sub=='^'?"widehat":n->sub=='~'?"widetilde":n->sub==0xaf?"overline":
            n->sub==0x2d9?"dot":n->sub==0xa8?"ddot":n->sub==0x2c7?"check":n->sub==0x2d8?"breve":"vec";
        fz_append_printf(t->ctx,out,"\\%s{",cmd);emit(t,n->child,out,depth+1);fz_append_byte(t->ctx,out,'}');
        if(n->glyph){if(t->norder>=t->nglyph)t->ok=0;else t->order[t->norder++]=n->glyph;}
        break;
    }
    case FENCED:
        fz_append_string(t->ctx,out,"\\left");emit(t,n->sub,out,depth+1);
        emit(t,n->child,out,depth+1);fz_append_string(t->ctx,out,"\\right");emit(t,n->sup,out,depth+1);break;
    case TABLE:
        fz_append_string(t->ctx,out,"\\begin{matrix}");
        for(int row=n->child;row;row=t->nodes[row].next) {
            if(row!=n->child)fz_append_string(t->ctx,out,"\\\\");
            emit(t,row,out,depth+1);
        }
        fz_append_string(t->ctx,out,"\\end{matrix}");break;
    case TABLE_ROW:
        for(int c=n->child;c;c=t->nodes[c].next) {
            if(c!=n->child)fz_append_byte(t->ctx,out,'&');
            emit(t,c,out,depth+1);
        }
        break;
    default:t->ok=0;
    }
}
int pd_source_math(Document *d,Page *p,Reconstruction *r) {
    if(r->rotation)return 0;
    Tree t={.ctx=d->ctx,.n=1,.ok=1};fz_buffer *out=NULL;
    char *raw=NULL,*markup=NULL;int accepted=0;
    fz_var(t);fz_var(out);fz_var(raw);fz_var(markup);fz_var(accepted);
    fz_try(d->ctx) {
        t.nodes=fz_malloc_array(d->ctx,NODE_LIMIT,Node);t.order=fz_malloc_array(d->ctx,SYMBOL_LIMIT,const Glyph*);
        int ids[SYMBOL_LIMIT],n=0;
        for(int li=0;li<p->nline && t.ok;li++) {
            if(r->line>=0 && li!=r->line)continue;
            Line *l=&p->lines[li];int spaced=0;
            for(int gi=0;gi<l->n;gi++) {
                if(r->line>=0 && (gi<r->first || gi>=r->end))continue;
                Glyph *g=&l->glyph[gi];if(pd_math_space(g->c)){if(g->c<=32 || g->c==0xa0)spaced=1;continue;}
                float x=midx(g->box),y=midy(g->box);
                if(x<r->box.x0 || x>r->box.x1 || y<r->box.y0 || y>r->box.y1)continue;
                /* Accents and invisible formatting need their own layout
                 * productions. Printing them as baseline operators corrupts
                 * a formula even when a symbol-inventory check passes. */
                if(n==SYMBOL_LIMIT || g->flags&G_BAD || g->c=='\\' || fabsf(l->dy)>.05f || g->size<=0 ||
                   ((g->c>=0x300 && g->c<=0x36f) && !pd_math_accent(g->c)) || (g->c>=0x200b && g->c<=0x200f) ||
                   ((g->c>=0x2c6 && g->c<=0x2dd) && !pd_math_accent(g->c)) ||
                   g->c==0xb2 || g->c==0xb3 || g->c==0xb9 || (g->c>=0x2070 && g->c<=0x209f) ||
                   (g->box.x1<=g->box.x0 && !pd_math_accent(g->c)) ||
                   !isfinite(g->size) || !isfinite(g->baseline) || !isfinite(g->box.x0) ||
                   !isfinite(g->box.x1) || !isfinite(g->box.y0) || !isfinite(g->box.y1)){t.ok=0;break;}
                ids[n++]=add(&t,(Node){.kind=LEAF,.glyph=g,.box=g->box,.size=g->size,.baseline=g->baseline,.spaced=spaced || (g->flags&G_GAP)});t.nglyph++;spaced=0;
            }
        }
        float size=0;for(int i=0;i<n;i++)size=fmaxf(size,t.nodes[ids[i]].size);
        int nmarks;const Mark *marks=pd_math_marks(p,&nmarks);
        for(int i=0;i<nmarks && t.ok;i++)if(rect_overlap(marks[i].box,r->box)) {
            const Mark *m=&marks[i];PD_Rect b=m->box;
            if(n==SYMBOL_LIMIT || m->image || b.y1-b.y0>size*.2f || b.x1-b.x0<size*.3f){t.ok=0;break;}
            ids[n++]=add(&t,(Node){.kind=BAR,.box=b,.size=size,.baseline=midy(b)});
        }
        int root=t.ok?parse(&t,ids,n,0):0;
        /* An isolated inline exponent belongs to the surrounding line, not
         * to a new baseline of its own. Do not turn it into an ordinary x. */
        if(root && r->line>=0 && !r->display) {
            Line *line=&p->lines[r->line];
            Glyph base={.baseline=t.nodes[root].baseline,.size=t.nodes[root].size};
            if(pd_glyph_script(line,&base) || fabsf(base.baseline-line->baseline)>line->size*.18f)t.ok=0;
        }
        if(root && t.ok) {
            out=fz_new_buffer(d->ctx,256);emit(&t,root,out,0);
            if(t.ok && t.norder==t.nglyph) {
                raw=fz_strdup(d->ctx,fz_string_from_buffer(d->ctx,out));char why[192];
                markup=pd_mathml(d->ctx,raw,r->display,why,sizeof(why));
                Reconstruction check=*r;check.markup=markup;
                accepted=markup && pd_verify_ordered_math(d,p,&check,t.order,t.norder);
            }
        }
        if(accepted) {
            fz_free(d->ctx,r->raw);fz_free(d->ctx,r->markup);r->raw=raw;r->markup=markup;raw=markup=NULL;
            r->accepted=1;snprintf(r->reason,sizeof(r->reason),"Source layout tree; exact glyph coverage, rules and spatial checks");
        }
    }
    fz_always(d->ctx){fz_free(d->ctx,t.nodes);fz_free(d->ctx,t.order);fz_drop_buffer(d->ctx,out);fz_free(d->ctx,raw);fz_free(d->ctx,markup);}
    fz_catch(d->ctx)fz_rethrow(d->ctx);
    return accepted;
}
