/* User decisions are local data, never extraction rules. The exact PDF hash
 * selects a bounded, length-framed file under the user's state directory. */
#include "internal.h"
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int identity(const char *source,char digest[65],char *error,size_t size) {
    Document d={0};int ok=0;fz_var(ok);d.ctx=fz_new_context(NULL,NULL,0);if(!d.ctx)return 0;
    fz_try(d.ctx){pd_source_digest(&d,source);memcpy(digest,d.source_digest,65);ok=1;}
    fz_catch(d.ctx)snprintf(error,size,"%s",fz_caught_message(d.ctx));
    fz_drop_context(d.ctx);return ok;
}
static int location(const char *digest,char path[4096],int create) {
    const char *base=getenv("XDG_STATE_HOME");int n;
    if(base && *base=='/')n=snprintf(path,4096,"%s/paperdesk/corrections/%s.txt",base,digest);
    else {
        base=getenv("HOME");if(!base || *base!='/')return 0;
        n=snprintf(path,4096,"%s/.local/state/paperdesk/corrections/%s.txt",base,digest);
    }
    if(n<0 || n>=4000)return 0;
    if(create)for(char *s=path+1;*s;s++)if(*s=='/') {
        *s=0;int rc=mkdir(path,0700);*s='/';if(rc && errno!=EEXIST)return 0;
    }
    return 1;
}
static int valid(const PD_Correction *c) {
    return c->page>0 && c->page<=1000000 && c->task>=1 && c->task<=3 && c->action>=1 && c->action<=2 &&
        isfinite(c->box.x0) && isfinite(c->box.x1) && isfinite(c->box.y0) && isfinite(c->box.y1) &&
        c->box.x1>c->box.x0 && c->box.y1>c->box.y0;
}
void pd_free_corrections(PD_Correction *edits,int count) {
    for(int i=0;i<count;i++)free((void*)edits[i].text);
    free(edits);
}
int pd_load_corrections(const char *source,PD_Correction **out,int *count,char digest[65],char *error,size_t size) {
    *out=NULL;*count=0;digest[0]=0;char path[4096],header[256],stored[65];int n=0;char extra;
    if(!identity(source,digest,error,size))return 0;
    if(!location(digest,path,0)){snprintf(error,size,"Cannot locate saved corrections");return 0;}
    FILE *file=fopen(path,"rb");if(!file){if(errno==ENOENT)return 1;snprintf(error,size,"Cannot read saved corrections");return 0;}
    int ok=fgets(header,sizeof(header),file) && sscanf(header,"PD-CORRECTIONS1 %64s %d %c",stored,&n,&extra)==2 && !strcmp(stored,digest) && n>=0 && n<=4096;
    PD_Correction *edits=ok?calloc(n?n:1,sizeof(*edits)):NULL;ok=ok && edits;size_t total=0;
    for(int i=0;i<n && ok;i++) {
        PD_Correction *c=&edits[i];size_t length=0;
        ok=fgets(header,sizeof(header),file) && sscanf(header,"%d %d %d %f %f %f %f %zu %c",&c->page,&c->task,&c->action,&c->box.x0,&c->box.y0,&c->box.x1,&c->box.y1,&length,&extra)==8;
        total+=length;ok=ok && valid(c) && length<=65536 && total<=16*1024*1024;
        if(!ok)break;
        char *text=malloc(length+1);c->text=text;if(!text){ok=0;break;}
        ok=fread(text,1,length,file)==length && fgetc(file)=='\n';text[length]=0;
        if(strlen(text)!=length)ok=0;
    }
    if(ok && fgetc(file)!=EOF)ok=0;
    if(ferror(file))ok=0;
    fclose(file);
    if(!ok){pd_free_corrections(edits,edits?n:0);snprintf(error,size,"Saved corrections are incomplete or invalid");return 0;}
    *out=edits;*count=n;return 1;
}
int pd_save_corrections(const char *source,const char *digest,const PD_Correction *edits,int count,char *error,size_t size) {
    char current[65],path[4096],temporary[4128];size_t total=0;
    if(!identity(source,current,error,size))return 0;
    if(!digest || strcmp(current,digest)){snprintf(error,size,"PDF changed since inspection; corrections were not saved");return 0;}
    if(count<0 || count>4096 || (count && !edits))return 0;
    for(int i=0;i<count;i++) {
        size_t length=edits[i].text?strlen(edits[i].text):0;total+=length;
        if(!valid(&edits[i]) || length>65536 || total>16*1024*1024){snprintf(error,size,"Correction exceeds the supported limits");return 0;}
    }
    if(!location(digest,path,1)){snprintf(error,size,"Cannot create correction folder");return 0;}
    snprintf(temporary,sizeof(temporary),"%s.part-XXXXXX",path);
    int fd=mkstemp(temporary);if(fd<0){snprintf(error,size,"Cannot save corrections: %s",strerror(errno));return 0;}
    FILE *file=fdopen(fd,"wb");if(!file){close(fd);unlink(temporary);return 0;}
    int ok=fprintf(file,"PD-CORRECTIONS1 %s %d\n",digest,count)>0;
    for(int i=0;i<count && ok;i++) {
        const PD_Correction *c=&edits[i];const char *text=c->text?c->text:"";size_t length=strlen(text);
        ok=fprintf(file,"%d %d %d %.9g %.9g %.9g %.9g %zu\n",c->page,c->task,c->action,c->box.x0,c->box.y0,c->box.x1,c->box.y1,length)>0 &&
            fwrite(text,1,length,file)==length && fputc('\n',file)!=EOF;
    }
    if(fflush(file) || fsync(fd))ok=0;
    if(fclose(file))ok=0;
    if(ok && rename(temporary,path))ok=0;
    if(!ok){unlink(temporary);snprintf(error,size,"Cannot finish saving corrections: %s",strerror(errno));}
    return ok;
}
