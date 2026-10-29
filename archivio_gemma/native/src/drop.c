#include "paper.h"
#include <ctype.h>
#include <string.h>

static int hex(unsigned char c) {
    if(c>='0' && c<='9')return c-'0';
    c=(unsigned char)tolower(c);
    return c>='a' && c<='f'?c-'a'+10:-1;
}
/* One local PDF per drop. URI lists use CRLF and may contain comments.
 * Decode URIs exactly once; bare paths keep literal percent characters. */
int pd_drop_path(const char *text,size_t length,char *path,size_t capacity) {
    int files=0;if(!text || !path || !capacity)return 0;
    path[0]=0;
    for(size_t begin=0;begin<length;) {
        size_t end=begin;while(end<length && text[end]!='\r' && text[end]!='\n')end++;
        size_t next=end;while(next<length && (text[next]=='\r' || text[next]=='\n'))next++;
        if(end==begin || text[begin]=='#'){begin=next;continue;}
        if(files++)return 0;
        int uri=end-begin>=5 && !memcmp(text+begin,"file:",5);
        if(uri) {
            begin+=5;
            if(end-begin>=2 && !memcmp(text+begin,"//",2)) {
                begin+=2;size_t host=begin;while(begin<end && text[begin]!='/')begin++;
                if(begin!=host && (begin-host!=9 || memcmp(text+host,"localhost",9)))return 0;
            }
        }
        if(begin==end || text[begin]!='/')return 0;
        size_t n=0;
        while(begin<end) {
            unsigned char c=(unsigned char)text[begin++];
            if(uri && (c=='?' || c=='#'))return 0;
            if(uri && c=='%') {
                if(end-begin<2)return 0;
                int a=hex((unsigned char)text[begin]),b=hex((unsigned char)text[begin+1]);
                if(a<0 || b<0)return 0;
                c=(unsigned char)(a*16+b);begin+=2;
            }
            if(c<32 || c==127 || n+1>=capacity)return 0;
            path[n++]=(char)c;
        }
        path[n]=0;
        if(n<4 || path[n-4]!='.' || tolower((unsigned char)path[n-3])!='p' ||
           tolower((unsigned char)path[n-2])!='d' || tolower((unsigned char)path[n-1])!='f')return 0;
        begin=next;
    }
    return files==1;
}
