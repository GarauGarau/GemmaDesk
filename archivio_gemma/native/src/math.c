/* Bounded mathematical markup parser. This is deliberately not a TeX
 * interpreter: unsupported commands fail the entire candidate. Nothing is
 * executed, expanded from a file, or silently discarded. */
#include "internal.h"
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { M_ROW, M_ID, M_NUM, M_OP, M_TEXT, M_FRAC, M_SQRT, M_ROOT,
       M_SUB, M_SUP, M_SUBSUP, M_OVER, M_UNDER, M_UNDEROVER,
       M_TABLE, M_TR, M_TD, M_STYLE, M_SPACE };
typedef struct { int type,child,next,limits; char text[512],attr[80]; } MathNode;
typedef struct {
    fz_context *ctx;const char *s;MathNode *nodes;int n,cap,depth,display;
    char error[160];
} Parser;
typedef struct { const char *name;int rune; } Symbol;
static const Symbol symbols[]={
    {"alpha",0x3b1},{"beta",0x3b2},{"gamma",0x3b3},{"delta",0x3b4},
    {"epsilon",0x3f5},{"varepsilon",0x3b5},{"zeta",0x3b6},{"eta",0x3b7},
    {"theta",0x3b8},{"vartheta",0x3d1},{"iota",0x3b9},{"kappa",0x3ba},
    {"lambda",0x3bb},{"mu",0x3bc},{"nu",0x3bd},{"xi",0x3be},{"pi",0x3c0},
    {"varpi",0x3d6},{"rho",0x3c1},{"varrho",0x3f1},{"sigma",0x3c3},
    {"varsigma",0x3c2},{"tau",0x3c4},{"upsilon",0x3c5},{"phi",0x3d5},
    {"varphi",0x3c6},{"chi",0x3c7},{"psi",0x3c8},{"omega",0x3c9},
    {"Gamma",0x393},{"Delta",0x394},{"Theta",0x398},{"Lambda",0x39b},
    {"Xi",0x39e},{"Pi",0x3a0},{"Sigma",0x3a3},{"Upsilon",0x3a5},
    {"Phi",0x3a6},{"Psi",0x3a8},{"Omega",0x3a9},
    {"sum",0x2211},{"prod",0x220f},{"coprod",0x2210},{"int",0x222b},
    {"iint",0x222c},{"iiint",0x222d},{"oint",0x222e},
    {"partial",0x2202},{"nabla",0x2207},{"infty",0x221e},{"ell",0x2113},
    {"hbar",0x210f},{"imath",0x131},{"jmath",0x237},
    {"times",0xd7},{"cdot",0x22c5},{"div",0xf7},{"pm",0xb1},{"mp",0x2213},
    {"le",0x2264},{"leq",0x2264},{"ge",0x2265},{"geq",0x2265},{"neq",0x2260},{"ne",0x2260},
    {"equiv",0x2261},{"approx",0x2248},{"sim",0x223c},{"simeq",0x2243},{"cong",0x2245},
    {"propto",0x221d},{"ll",0x226a},{"gg",0x226b},{"prec",0x227a},{"succ",0x227b},
    {"preceq",0x2aaf},{"succeq",0x2ab0},{"perp",0x22a5},{"parallel",0x2225},
    {"in",0x2208},{"notin",0x2209},{"ni",0x220b},{"subset",0x2282},{"supset",0x2283},
    {"subseteq",0x2286},{"supseteq",0x2287},{"cup",0x222a},{"cap",0x2229},
    {"bigcup",0x22c3},{"bigcap",0x22c2},{"setminus",0x2216},{"emptyset",0x2205},{"varnothing",0x2205},
    {"forall",0x2200},{"exists",0x2203},{"nexists",0x2204},{"neg",0xac},{"lnot",0xac},
    {"land",0x2227},{"wedge",0x2227},{"lor",0x2228},{"vee",0x2228},
    {"to",0x2192},{"rightarrow",0x2192},{"leftarrow",0x2190},{"leftrightarrow",0x2194},
    {"Rightarrow",0x21d2},{"Leftarrow",0x21d0},{"Leftrightarrow",0x21d4},
    {"implies",0x21d2},{"iff",0x21d4},{"mapsto",0x21a6},{"uparrow",0x2191},{"downarrow",0x2193},
    {"longrightarrow",0x27f6},{"longleftarrow",0x27f5},{"longleftrightarrow",0x27f7},
    {"ldots",0x2026},{"dots",0x2026},{"cdots",0x22ef},{"vdots",0x22ee},{"ddots",0x22f1},
    {"langle",0x27e8},{"rangle",0x27e9},{"lceil",0x2308},{"rceil",0x2309},
    {"lfloor",0x230a},{"rfloor",0x230b},{"lvert",'|'},{"rvert",'|'},{"vert",'|'},
    {"Vert",0x2016},{"lVert",0x2016},{"rVert",0x2016},{"lbrace",'{'},{"rbrace",'}'},
    {"prime",0x2032},{"star",0x22c6},{"ast",0x2217},{"circ",0x2218},{"bullet",0x2022},
    {"oplus",0x2295},{"otimes",0x2297},{"ominus",0x2296},{"odot",0x2299},
    {"dagger",0x2020},{"ddagger",0x2021},{"Re",0x211c},{"Im",0x2111},
    {"aleph",0x2135},{"wp",0x2118},{"angle",0x2220},{"top",0x22a4},{"bot",0x22a5},
    {"therefore",0x2234},{"because",0x2235},{"checkmark",0x2713},
};
static void fail(Parser *p,const char *message) {
    if(!p->error[0])snprintf(p->error,sizeof(p->error),"%s",message);
}
static void space(Parser *p){while(isspace((unsigned char)*p->s))p->s++;}
static int node(Parser *p,int type) {
    if(p->n>=8192){fail(p,"Formula exceeds the node limit");return 0;}
    p->nodes=pd_grow(p->ctx,p->nodes,&p->cap,p->n+1,sizeof(MathNode));
    int i=p->n++;memset(&p->nodes[i],0,sizeof(MathNode));p->nodes[i].type=type;return i;
}
static void child(Parser *p,int parent,int item) {
    if(!parent || !item)return;
    int *link=&p->nodes[parent].child;
    while(*link)link=&p->nodes[*link].next;
    *link=item;
}
static int token(Parser *p,int type,int c) {
    int i=node(p,type);p->nodes[i].text[fz_runetochar(p->nodes[i].text,c)]=0;
    if(type==M_OP && ((c<128 && strchr("()[]{}|",c)) || (c>=0x2190 && c<=0x21ff)))strcpy(p->nodes[i].attr,"stretchy=\"false\"");
    if(c==0x2211 || c==0x220f || c==0x2210 || c==0x22c3 || c==0x22c2)p->nodes[i].limits=1;
    return i;
}
static int command(Parser *p,char out[64]) {
    if(*p->s!='\\')return 0;
    p->s++;int n=0;
    if(!isalpha((unsigned char)*p->s)) {if(*p->s)out[n++]=*p->s++;}
    else while(isalpha((unsigned char)*p->s)) {
        if(n>=62){fail(p,"Command name too long");return 0;}out[n++]=*p->s++;
    }
    out[n]=0;return n;
}
static int row(Parser *p,int stop);
static int atom(Parser *p);
static int argument(Parser *p) {
    space(p);
    if(*p->s=='{'){p->s++;int a=row(p,'}');if(*p->s!='}')fail(p,"Unclosed group");else p->s++;return a;}
    return atom(p);
}
static int text_group(Parser *p,int type) {
    space(p);if(*p->s!='{'){fail(p,"Text needs a braced argument");return 0;}p->s++;
    int i=node(p,type),n=0,depth=1;
    while(*p->s && depth && !p->error[0]) {
        int c;int count=fz_chartorune(&c,p->s);p->s+=count;
        if(c=='{'){depth++;continue;}if(c=='}'){depth--;continue;}
        if(c=='\\') {
            if(strchr("{}%$&#_ ",*p->s) && *p->s)c=*p->s++;
            else {fail(p,"Unsupported command inside text");break;}
        }
        if(n+8>=512){fail(p,"Text argument too long");break;}
        n+=fz_runetochar(p->nodes[i].text+n,c);
    }
    p->nodes[i].text[n]=0;if(depth)fail(p,"Unclosed text argument");return i;
}
static int delimiter(Parser *p) {
    space(p);if(*p->s=='.'){p->s++;return 0;}
    int i=atom(p);
    int core=i;
    while(core && (p->nodes[core].type==M_STYLE || p->nodes[core].type==M_ROW) &&
          p->nodes[core].child && !p->nodes[p->nodes[core].child].next)core=p->nodes[core].child;
    if(core && p->nodes[core].type==M_OP)strcpy(p->nodes[core].attr,"stretchy=\"true\"");
    else fail(p,"Invalid delimiter");
    return i;
}
static int fence(Parser *p,int c) {
    int i=token(p,M_OP,c);strcpy(p->nodes[i].attr,"stretchy=\"true\"");return i;
}
static int environment(Parser *p) {
    int name=text_group(p,M_TEXT);if(!name)return 0;
    char env[64];snprintf(env,sizeof(env),"%.63s",p->nodes[name].text);
    const char *valid[]={"matrix","pmatrix","bmatrix","Bmatrix","vmatrix","Vmatrix","smallmatrix",
        "cases","aligned","alignedat","align","align*","gathered","gather","gather*","split","array","equation","equation*"};
    int known=0;for(size_t k=0;k<sizeof(valid)/sizeof(*valid);k++)known|=!strcmp(env,valid[k]);
    if(!known){fail(p,"Unsupported mathematical environment");return 0;}
    if(!strcmp(env,"array") || !strcmp(env,"alignedat")) {
        int spec=text_group(p,M_TEXT);
        for(const char *s=p->nodes[spec].text;*s;s++)if(!strchr("lcr|0123456789 ",*s))fail(p,"Unsupported array column specification");
        if(strchr(p->nodes[spec].text,'|'))fail(p,"Array rules require explicit reconstruction");
    }
    int wrap=node(p,M_ROW),left=0,right=0;
    if(!strcmp(env,"pmatrix")){left='(';right=')';}
    if(!strcmp(env,"bmatrix")){left='[';right=']';}
    if(!strcmp(env,"Bmatrix")){left='{';right='}';}
    if(!strcmp(env,"vmatrix")){left=right='|';}
    if(!strcmp(env,"Vmatrix")){left=right=0x2016;}
    if(!strcmp(env,"cases"))left='{';
    if(left)child(p,wrap,fence(p,left));
    int table=node(p,M_TABLE);child(p,wrap,table);
    if(!strcmp(env,"cases"))strcpy(p->nodes[table].attr,"columnalign=\"left left\"");
    if(strstr(env,"align") || !strcmp(env,"split"))strcpy(p->nodes[table].attr,"columnalign=\"right left\" columnspacing=\"0em\"");
    int tr=node(p,M_TR);child(p,table,tr);int done=0;
    while(*p->s && !p->error[0]) {
        int td=node(p,M_TD);child(p,tr,td);child(p,td,row(p,'&'));
        if(*p->s=='&'){p->s++;continue;}
        if(!strncmp(p->s,"\\\\",2)) {p->s+=2;tr=node(p,M_TR);child(p,table,tr);continue;}
        if(!strncmp(p->s,"\\end",4)) {
            p->s+=4;int end=text_group(p,M_TEXT);
            if(strcmp(env,p->nodes[end].text))fail(p,"Mismatched mathematical environment");
            done=1;break;
        }
        fail(p,"Unclosed mathematical environment");
    }
    if(!done)fail(p,"Unclosed mathematical environment");
    if(right)child(p,wrap,fence(p,right));
    return wrap;
}
static int atom(Parser *p) {
    space(p);if(p->error[0])return 0;
    if(++p->depth>96){fail(p,"Formula nesting limit exceeded");p->depth--;return 0;}
    int result=0;
    if(*p->s=='{') {p->s++;result=row(p,'}');if(*p->s=='}')p->s++;else fail(p,"Unclosed group");}
    else if(*p->s=='\\') {
        char cmd[64];command(p,cmd);
        if(!strcmp(cmd,"frac") || !strcmp(cmd,"dfrac") || !strcmp(cmd,"tfrac") || !strcmp(cmd,"binom")) {
            int a=argument(p),b=argument(p),frac=node(p,M_FRAC);child(p,frac,a);child(p,frac,b);result=frac;
            if(!strcmp(cmd,"binom")) {
                strcpy(p->nodes[frac].attr,"linethickness=\"0\"");result=node(p,M_ROW);
                child(p,result,fence(p,'('));child(p,result,frac);child(p,result,fence(p,')'));
            }
        } else if(!strcmp(cmd,"sqrt")) {
            space(p);int index=0;if(*p->s=='['){p->s++;index=row(p,']');if(*p->s==']')p->s++;else fail(p,"Unclosed root index");}
            int base=argument(p);result=node(p,index?M_ROOT:M_SQRT);child(p,result,base);if(index)child(p,result,index);
        } else if(!strcmp(cmd,"text") || !strcmp(cmd,"textrm") || !strcmp(cmd,"mbox") || !strcmp(cmd,"operatorname")) {
            result=text_group(p,!strcmp(cmd,"operatorname")?M_OP:M_TEXT);
        } else if(!strcmp(cmd,"begin"))result=environment(p);
        else if(!strcmp(cmd,"left")) {
            result=node(p,M_ROW);int open=delimiter(p);child(p,result,open);
            int inside=row(p,0);child(p,result,inside);
            if(strncmp(p->s,"\\right",6))fail(p,"Missing right delimiter");
            else {p->s+=6;child(p,result,delimiter(p));}
        } else if(!strcmp(cmd,"hat") || !strcmp(cmd,"widehat") || !strcmp(cmd,"bar") || !strcmp(cmd,"overline") ||
                  !strcmp(cmd,"vec") || !strcmp(cmd,"tilde") || !strcmp(cmd,"widetilde") ||
                  !strcmp(cmd,"dot") || !strcmp(cmd,"ddot") || !strcmp(cmd,"check") || !strcmp(cmd,"breve") ||
                  !strcmp(cmd,"underline") || !strcmp(cmd,"underbrace") || !strcmp(cmd,"overbrace")) {
            int under=!strcmp(cmd,"underline") || !strcmp(cmd,"underbrace");
            int c=strstr(cmd,"hat")?'^':strstr(cmd,"tilde")?'~':!strcmp(cmd,"vec")?0x2192:
                !strcmp(cmd,"dot")?0x2d9:!strcmp(cmd,"ddot")?0xa8:!strcmp(cmd,"check")?0x2c7:
                !strcmp(cmd,"breve")?0x2d8:strstr(cmd,"brace")?(under?0x23df:0x23de):0x00af;
            int base=argument(p),accent=token(p,M_OP,c);result=node(p,under?M_UNDER:M_OVER);child(p,result,base);child(p,result,accent);
            if(strstr(cmd,"brace"))p->nodes[result].limits=2;
            if(!strcmp(cmd,"vec"))strcpy(p->nodes[accent].attr,"stretchy=\"true\"");
            strcpy(p->nodes[result].attr,under?"accentunder=\"true\"":"accent=\"true\"");
        } else if(!strcmp(cmd,"overset") || !strcmp(cmd,"underset") || !strcmp(cmd,"stackrel")) {
            int label=argument(p),base=argument(p);result=node(p,!strcmp(cmd,"underset")?M_UNDER:M_OVER);child(p,result,base);child(p,result,label);
        } else if(!strcmp(cmd,"mathrm") || !strcmp(cmd,"mathit") || !strcmp(cmd,"mathbf") || !strcmp(cmd,"boldsymbol") ||
                  !strcmp(cmd,"mathbb") || !strcmp(cmd,"mathcal") || !strcmp(cmd,"mathscr") ||
                  !strcmp(cmd,"mathfrak") || !strcmp(cmd,"mathsf") || !strcmp(cmd,"mathtt") || !strcmp(cmd,"bm")) {
            const char *variant=!strcmp(cmd,"mathrm")?"normal":!strcmp(cmd,"mathit")?"italic":
                !strcmp(cmd,"mathbf")?"bold":(!strcmp(cmd,"boldsymbol") || !strcmp(cmd,"bm"))?"bold-italic":
                !strcmp(cmd,"mathbb")?"double-struck":!strcmp(cmd,"mathfrak")?"fraktur":
                !strcmp(cmd,"mathsf")?"sans-serif":!strcmp(cmd,"mathtt")?"monospace":"script";
            int arg=argument(p);result=node(p,M_STYLE);snprintf(p->nodes[result].attr,sizeof(p->nodes[result].attr),"mathvariant=\"%s\"",variant);child(p,result,arg);
        } else if(!strcmp(cmd,"tag")) {
            result=node(p,M_ROW);child(p,result,token(p,M_OP,'('));child(p,result,text_group(p,M_TEXT));child(p,result,token(p,M_OP,')'));
        } else if(!strcmp(cmd,"displaystyle") || !strcmp(cmd,"textstyle") || !strcmp(cmd,"scriptstyle") || !strcmp(cmd,"scriptscriptstyle")) {
            result=node(p,M_STYLE);snprintf(p->nodes[result].attr,sizeof(p->nodes[result].attr),"displaystyle=\"%s\" scriptlevel=\"%d\"",!strcmp(cmd,"displaystyle")?"true":"false",!strcmp(cmd,"scriptstyle")?1:!strcmp(cmd,"scriptscriptstyle")?2:0);
            child(p,result,row(p,0));
        } else if(!strcmp(cmd,",") || !strcmp(cmd,";") || !strcmp(cmd,":") || !strcmp(cmd,"!") ||
                  !strcmp(cmd,"quad") || !strcmp(cmd,"qquad") || !strcmp(cmd," ")) {
            result=node(p,M_SPACE);snprintf(p->nodes[result].attr,sizeof(p->nodes[result].attr),"width=\"%sem\"",!strcmp(cmd,"qquad")?"2":!strcmp(cmd,"quad")?"1":!strcmp(cmd,"!")?"0":"0.2");
        } else if(strlen(cmd)==1 && strchr("{}%$&#_|",cmd[0]))result=token(p,M_OP,cmd[0]);
        else {
            for(size_t i=0;i<sizeof(symbols)/sizeof(*symbols);i++)if(!strcmp(cmd,symbols[i].name)) {
                int c=symbols[i].rune;result=token(p,(c>=0x370 && c<=0x3ff)?M_ID:M_OP,c);break;
            }
            if(!result) {
                const char *names[]={"log","ln","exp","sin","cos","tan","cot","sec","csc","sinh","cosh","tanh",
                    "arcsin","arccos","arctan","min","max","argmin","argmax","lim","liminf","limsup","inf","sup","det","dim","ker","Pr","gcd"};
                for(size_t i=0;i<sizeof(names)/sizeof(*names);i++)if(!strcmp(cmd,names[i])) {
                    result=node(p,M_OP);strcpy(p->nodes[result].text,cmd);
                    if(!strncmp(cmd,"lim",3) || !strcmp(cmd,"min") || !strcmp(cmd,"max") || !strcmp(cmd,"sup") || !strcmp(cmd,"inf"))p->nodes[result].limits=1;
                    break;
                }
            }
            if(!result){char reason[128];snprintf(reason,sizeof(reason),"Unsupported command: \\%s",cmd);fail(p,reason);}
        }
    } else if(*p->s && !strchr("}^_&$",*p->s)) {
        int c;int n=fz_chartorune(&c,p->s);p->s+=n;
        if(c=='-')c=0x2212;
        if(!pd_xml_scalar(c))fail(p,"Invalid mathematical character");
        result=token(p,(c>='0' && c<='9')?M_NUM:((c<128 && isalpha(c)) || (c>=0x370 && c<=0x3ff) || c>=0x1d400)?M_ID:M_OP,c);
        if(c>='0' && c<='9') {
            int length=1,dot=0;
            while(isdigit((unsigned char)*p->s) || (*p->s=='.' && !dot && isdigit((unsigned char)p->s[1]))) {
                if(length>=500){fail(p,"Number exceeds the token limit");break;}
                if(*p->s=='.')dot=1;
                p->nodes[result].text[length++]=*p->s++;
            }
            p->nodes[result].text[length]=0;
        }
    } else fail(p,"Missing mathematical argument");
    p->depth--;return result;
}
static int row(Parser *p,int stop) {
    int result=node(p,M_ROW);
    while(*p->s && !p->error[0]) {
        space(p);if(!*p->s || *p->s=='}' || (*p->s==']' && stop==']') || *p->s==stop ||
            (!strncmp(p->s,"\\right",6) && !isalpha((unsigned char)p->s[6])) ||
            !strncmp(p->s,"\\end",4) || !strncmp(p->s,"\\\\",2) || *p->s=='&')break;
        int base=atom(p);if(!base)break;
        space(p);int sub=0,sup=0,limits=p->nodes[base].limits==2 || (p->display && p->nodes[base].limits==1);
        if(!strncmp(p->s,"\\limits",7)){p->s+=7;limits=1;space(p);}
        else if(!strncmp(p->s,"\\nolimits",9)){p->s+=9;limits=0;space(p);}
        while(*p->s=='_' || *p->s=='^' || *p->s=='\'') {
            char op=*p->s++;
            if(op=='\'') {
                if(sup){fail(p,"Ambiguous superscript");break;}
                sup=node(p,M_ROW);child(p,sup,token(p,M_OP,0x2032));
                while(*p->s=='\''){p->s++;child(p,sup,token(p,M_OP,0x2032));}
            } else {
                if((op=='_' && sub) || (op=='^' && sup)){fail(p,"Duplicate script");break;}
                int arg=argument(p);if(op=='_')sub=arg;else sup=arg;
            }
            space(p);
        }
        if(sub || sup) {
            int type=sub && sup?(limits?M_UNDEROVER:M_SUBSUP):sub?(limits?M_UNDER:M_SUB):(limits?M_OVER:M_SUP);
            int scripts=node(p,type);child(p,scripts,base);if(sub)child(p,scripts,sub);if(sup)child(p,scripts,sup);base=scripts;
        }
        child(p,result,base);
    }
    return result;
}
static void emit(Parser *p,int i,fz_buffer *out) {
    static const char *tags[]={"mrow","mi","mn","mo","mtext","mfrac","msqrt","mroot","msub","msup","msubsup",
        "mover","munder","munderover","mtable","mtr","mtd","mstyle","mspace"};
    MathNode *n=&p->nodes[i];const char *tag=tags[n->type];
    fz_append_printf(p->ctx,out,"<%s%s%s>",tag,n->attr[0]?" ":"",n->attr);pd_xml(p->ctx,out,n->text);
    for(int c=n->child;c;c=p->nodes[c].next)emit(p,c,out);
    fz_append_printf(p->ctx,out,"</%s>",tag);
}
char *pd_mathml(fz_context *ctx,const char *latex,int display,char *reason,size_t capacity) {
    Parser p={0};p.ctx=ctx;p.s=latex;p.display=display;char *result=NULL;fz_buffer *out=NULL;fz_var(p);fz_var(result);fz_var(out);
    fz_try(ctx) {
        if(strlen(latex)>65536)fail(&p,"Formula exceeds the input limit");
        /* Outer delimiters are transport syntax, never part of a formula. */
        space(&p);int dollars=0,slash=0;
        if(*p.s=='$'){p.s++;dollars=1;if(*p.s=='$'){p.s++;dollars=2;}}
        else if(!strncmp(p.s,"\\[",2) || !strncmp(p.s,"\\(",2)){slash=p.s[1];p.s+=2;}
        char *copy=fz_strdup(ctx,p.s);size_t len=strlen(copy);
        while(len && isspace((unsigned char)copy[len-1]))copy[--len]=0;
        if(dollars)for(int k=0;k<dollars;k++) {if(len && copy[len-1]=='$')copy[--len]=0;else fail(&p,"Unclosed formula delimiter");}
        if(slash) {if(len>=2 && copy[len-2]=='\\' && copy[len-1]==(slash=='['?']':')'))copy[len-=2]=0;else fail(&p,"Unclosed formula delimiter");}
        p.s=copy;
        fz_try(ctx) {
            node(&p,M_ROW);int root=row(&p,0);space(&p);
            if(*p.s)fail(&p,"Unconsumed mathematical markup");
            if(!p.nodes[root].child)fail(&p,"Empty formula");
            if(!p.error[0]) {
                out=fz_new_buffer(ctx,512);fz_append_printf(ctx,out,"<math xmlns=\"http://www.w3.org/1998/Math/MathML\" display=\"%s\">",display?"block":"inline");
                emit(&p,root,out);fz_append_string(ctx,out,"</math>");result=fz_strdup(ctx,fz_string_from_buffer(ctx,out));
            }
        }
        fz_always(ctx)fz_free(ctx,copy);
        fz_catch(ctx)fz_rethrow(ctx);
        if(reason && capacity)snprintf(reason,capacity,"%s",p.error[0]?p.error:"Complete mathematical syntax");
    }
    fz_always(ctx){fz_free(ctx,p.nodes);fz_drop_buffer(ctx,out);}
    fz_catch(ctx)fz_rethrow(ctx);
    return result;
}

/* Presentational grouping for narrow EPUB screens. Keep fractions, scripts,
 * matrices and delimited expressions atomic. A reader may wrap between
 * top-level additive/relational terms without changing their order or trees.
 * This is typesetting only: it never repairs or simplifies mathematics. */
static fz_xml *flow_operator(fz_xml *x) {
    while(x && (fz_xml_is_tag(x,"mrow") || fz_xml_is_tag(x,"mstyle"))) {
        fz_xml *c=fz_xml_down(x);if(!c || fz_xml_next(c))return NULL;x=c;
    }
    return x && fz_xml_is_tag(x,"mo")?x:NULL;
}
static int flow_rune(fz_xml *x) {
    x=flow_operator(x);if(!x)return 0;
    const char *s=fz_xml_text(fz_xml_down(x));int c=0;
    if(!s || s[fz_chartorune(&c,s)])return 0;
    return c;
}
static void flow_copy(fz_context *ctx,fz_xml *x,fz_buffer *out,fz_xml *infix) {
    const char *s=fz_xml_text(x);if(s){pd_xml(ctx,out,s);return;}
    const char *tag=fz_xml_tag(x);fz_append_printf(ctx,out,"<%s",tag);
    for(int i=0;;i++) {
        const char *key,*value=fz_dom_get_attribute(ctx,x,i,&key);if(!key)break;
        fz_append_printf(ctx,out," %s=\"",key);pd_xml(ctx,out,value);fz_append_byte(ctx,out,'"');
    }
    if(x==infix && !fz_xml_att(x,"form"))fz_append_string(ctx,out," form=\"infix\"");
    fz_append_byte(ctx,out,'>');
    for(fz_xml *c=fz_xml_down(x);c;c=fz_xml_next(c))flow_copy(ctx,c,out,infix);
    fz_append_printf(ctx,out,"</%s>",tag);
}
char *pd_math_flow(fz_context *ctx,const char *markup,int reader) {
    fz_buffer *input=NULL,*out=NULL;fz_xml *xml=NULL;char *result=NULL;
    fz_var(input);fz_var(out);fz_var(xml);fz_var(result);
    fz_try(ctx) {
        input=fz_new_buffer_from_copied_data(ctx,(const unsigned char*)markup,strlen(markup));xml=fz_parse_xml(ctx,input,1);
        fz_xml *math=fz_xml_root(xml),*row=fz_xml_down(math);
        if(!fz_xml_is_tag(math,"math") || !fz_xml_is_tag(row,"mrow") || fz_xml_next(row))fz_throw(ctx,FZ_ERROR_FORMAT,"Invalid native math tree");
        int depth=0,valid=1,cuts=0,has_term=0,previous=0;
        fz_xml *split[8192];int count=0;
        for(fz_xml *c=fz_xml_down(row);c;c=fz_xml_next(c)) {
            int op=flow_rune(c);
            if(op=='(' || op=='[' || op=='{')depth++;
            if(op==')' || op==']' || op=='}'){if(!depth)valid=0;else depth--;}
            if(op=='|' || op==0x2016){valid=0;}
            int binary=op=='=' || op=='+' || op==0x2212 || op==0x2264 || op==0x2265 || op==0x2260 || op==0x2248 || op==0x2261;
            if(binary && !depth && has_term && !previous){if(count<8192)split[count++]=c;cuts++;has_term=0;}
            else if(!fz_xml_is_tag(c,"mspace"))has_term=1;
            previous=binary;
        }
        if(!valid || depth || !cuts)result=fz_strdup(ctx,markup);
        else {
            out=fz_new_buffer(ctx,strlen(markup)+512);
            int kobo=reader==PD_READER_KOBO;
            const char *display=fz_xml_att(math,"display");
            const char *term=display && !strcmp(display,"block")?
                "<span class=\"math-term\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\" display=\"inline\" displaystyle=\"true\"><mrow>":
                "<span class=\"math-term\"><math xmlns=\"http://www.w3.org/1998/Math/MathML\" display=\"inline\"><mrow>";
            /* WebKit does not consistently implement MathML linebreaking.
             * Let XHTML wrap complete mathematical terms at the same legal
             * outer operators. Each term retains its intact native subtree.
             * reconstruction.json retains the complete unsplit MathML tree. */
            if(kobo){fz_append_string(ctx,out,"<span class=\"math-flow\" role=\"math\">");fz_append_string(ctx,out,term);}
            else fz_append_printf(ctx,out,"<math xmlns=\"http://www.w3.org/1998/Math/MathML\" display=\"%s\"><mrow class=\"math-flow\"><mrow class=\"math-term\">",display?display:"inline");
            int next=0;fz_xml *infix=NULL;
            for(fz_xml *c=fz_xml_down(row);c;c=fz_xml_next(c)) {
                if(next<count && c==split[next]) {
                    next++;
                    if(kobo){fz_append_string(ctx,out,"</mrow></math></span> ");fz_append_string(ctx,out,term);}
                    else fz_append_string(ctx,out,"</mrow><mspace width=\"0em\" linebreak=\"goodbreak\"/><mrow class=\"math-term\">");
                    infix=flow_operator(c);
                }
                flow_copy(ctx,c,out,infix);
            }
            fz_append_string(ctx,out,kobo?"</mrow></math></span></span>":"</mrow></mrow></math>");result=fz_strdup(ctx,fz_string_from_buffer(ctx,out));
        }
    }
    fz_always(ctx){fz_drop_xml(ctx,xml);fz_drop_buffer(ctx,input);fz_drop_buffer(ctx,out);}
    fz_catch(ctx)fz_rethrow(ctx);
    return result;
}
