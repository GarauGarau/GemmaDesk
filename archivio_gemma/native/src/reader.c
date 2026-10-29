/* The EPUB renderer is a separately built, bundled command-line program.
 * Paths and anchor text are passed as arguments/framed bytes, never a shell. */
#define _POSIX_C_SOURCE 200809L
#include "reader.h"
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <sys/socket.h>
#include <unistd.h>
extern char **environ;
typedef struct {pid_t pid;int in,out,pages,w,h,font,failed;} Reader;
static int io(Reader *r,void *data,size_t length,int write_data) {
    if(r->failed)return 0;
    unsigned char *bytes=data;
    while(length) {
        struct pollfd fd={write_data?r->in:r->out,write_data?POLLOUT:POLLIN,0};
        int ready=poll(&fd,1,30000);
        if(ready<0 && errno==EINTR)continue;
        if(ready<1 || !(fd.revents&(write_data?POLLOUT:POLLIN))){r->failed=1;return 0;}
        ssize_t n=write_data?send(r->in,bytes,length,MSG_NOSIGNAL):read(r->out,bytes,length);
        if(n<0 && errno==EINTR)continue;
        if(n<=0){r->failed=1;return 0;}bytes+=n;length-=(size_t)n;
    }
    return 1;
}
static int request(Reader *r,const char *s){return io(r,(void*)s,strlen(s),1);}
static int line(Reader *r,char text[256]) {
    for(int i=0;i<255;i++){if(!io(r,text+i,1,0))return 0;if(text[i]=='\n'){text[i]=0;return 1;}}
    r->failed=1;return 0;
}
static int number(Reader *r) {
    char reply[256],extra;int n;
    if(line(r,reply) && sscanf(reply,"N %d %c",&n,&extra)==1)return n;
    r->failed=1;return -1;
}
void pd_reader_close(void *p) {
    Reader *r=p;if(!r)return;
    close(r->in);close(r->out);
    if(r->pid>0){kill(r->pid,SIGTERM);while(waitpid(r->pid,NULL,0)<0 && errno==EINTR){}}
    free(r);
}
void *pd_reader_open(const char *path,char *error,size_t size) {
    char program[4096];ssize_t n=readlink("/proc/self/exe",program,sizeof(program)-64);
    if(n<1){snprintf(error,size,"Cannot locate bundled EPUB reader");return NULL;}program[n]=0;
    char *slash=strrchr(program,'/');if(!slash)return NULL;strcpy(slash+1,"paperdesk-reader");
    int input[2],output[2];
    if(socketpair(AF_UNIX,SOCK_STREAM,0,input)){snprintf(error,size,"Cannot create reader channel");return NULL;}
    if(socketpair(AF_UNIX,SOCK_STREAM,0,output)){close(input[0]);close(input[1]);snprintf(error,size,"Cannot create reader channel");return NULL;}
    for(int i=0;i<2;i++){fcntl(input[i],F_SETFD,FD_CLOEXEC);fcntl(output[i],F_SETFD,FD_CLOEXEC);}
    posix_spawn_file_actions_t actions;posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions,input[0],STDIN_FILENO);posix_spawn_file_actions_adddup2(&actions,output[1],STDOUT_FILENO);
    pid_t pid=0;char *argv[]={program,(char*)path,NULL};
    int status=posix_spawn(&pid,program,&actions,NULL,argv,environ);posix_spawn_file_actions_destroy(&actions);
    close(input[0]);close(output[1]);
    if(status){close(input[1]);close(output[0]);snprintf(error,size,"Cannot start bundled EPUB reader: %s",strerror(status));return NULL;}
    Reader *r=calloc(1,sizeof(*r));
    if(!r){close(input[1]);close(output[0]);kill(pid,SIGTERM);waitpid(pid,NULL,0);snprintf(error,size,"Not enough memory");return NULL;}
    r->pid=pid;r->in=input[1];r->out=output[0];r->w=440;r->h=640;r->font=14;
    r->pages=number(r);
    if(r->pages<1){pd_reader_close(r);snprintf(error,size,"Bundled EPUB reader could not open this book");return NULL;}
    return r;
}
int pd_reader_layout(void *p,int w,int h,int font) {
    Reader *r=p;if(!r || w<80 || h<80 || w>10000 || h>10000 || font<6 || font>120)return 0;
    char command[100];snprintf(command,sizeof(command),"L %d %d %d\n",w,h,font);
    if(!request(r,command))return 0;
    int pages=number(r);if(pages<1)return 0;
    r->w=w;r->h=h;r->font=font;r->pages=pages;return 1;
}
int pd_reader_pages(void *p){return p?((Reader*)p)->pages:0;}
PD_Rect pd_reader_bounds(void *p){Reader *r=p;return (PD_Rect){0,0,r->w,r->h};}
int pd_reader_render(void *p,int page,float scale,int rotation,PD_Bitmap *bitmap,char *error,size_t size) {
    Reader *r=p;char command[100],reply[256],extra;size_t length;int w,h,stride;
    if(!r || !isfinite(scale) || scale<=0){snprintf(error,size,"Invalid native reader zoom");return 0;}
    snprintf(command,sizeof(command),"R %d %.6f %d\n",page,scale,rotation);
    if(request(r,command) && line(r,reply) && sscanf(reply,"B %d %d %d %zu %c",&w,&h,&stride,&length,&extra)==4 &&
       w>0 && h>0 && w<30000 && h<30000 && stride==w*3 && length==(size_t)stride*h && length<=240000000) {
        bitmap->samples=malloc(length);
        if(bitmap->samples && io(r,bitmap->samples,length,0)){bitmap->w=w;bitmap->h=h;bitmap->stride=stride;bitmap->scale=scale;return 1;}
        pd_bitmap_free(bitmap);
    }
    r->failed=1;snprintf(error,size,"Native EPUB preview unavailable");return 0;
}
int pd_reader_resolve(void *p,const char *anchor) {
    Reader *r=p;size_t n=strlen(anchor);if(n>=4096)return -1;
    char command[64];snprintf(command,sizeof(command),"F %zu\n",n);
    if(!request(r,command) || !io(r,(void*)anchor,n,1))return -1;
    return number(r);
}
int pd_reader_link(void *p,int page,float x,float y) {
    Reader *r=p;char command[128];if(!r || !isfinite(x) || !isfinite(y) || x<0 || y<0)return -1;
    snprintf(command,sizeof(command),"K %d %.3f %.3f\n",page,x,y);if(!request(r,command))return -1;return number(r);
}
int pd_reader_text(void *p,int page,const char *path) {
    Reader *r=p;char command[64],reply[256],extra;unsigned length;
    snprintf(command,sizeof(command),"T %d\n",page);
    if(!request(r,command) || !line(r,reply) || sscanf(reply,"D %u %c",&length,&extra)!=1 || length>4*1024*1024)return 0;
    char *text=malloc(length+1);if(!text)return 0;
    int ok=io(r,text,length,0);FILE *file=ok?fopen(path,"wb"):NULL;
    if(file){ok=fwrite(text,1,length,file)==length;if(fclose(file))ok=0;}else ok=0;
    free(text);return ok;
}
