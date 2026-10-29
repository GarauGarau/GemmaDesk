#include "internal.h"
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern char **environ;
struct PD_ModelProcess {pid_t pid;int fd,page;double deadline,started;char stage[112],cache[3900],signature[65],backend[160];};
static void cache_identity(Document *d,const char *weights,const char *projector,const char *worker);
static double now(void) {
    struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;
}
static int transfer(Document *d,void *data,size_t n,int sending) {
    PD_ModelProcess *m=d->model;unsigned char *p=data;
    while(n) {
        char status[160];
        snprintf(status,sizeof(status),"%s | %.0f s",m->page<0?"Loading local model":m->stage,now()-m->started);
        pd_check(d,status,m->page<0?0:m->page+1,d->npage);
        if(now()>m->deadline){errno=ETIMEDOUT;return 0;}
        struct pollfd fd={m->fd,sending?POLLOUT:POLLIN,0};
        int ready=poll(&fd,1,100);
        if(ready<0 && errno==EINTR)continue;
        if(ready<0 || (ready && (fd.revents&(POLLERR|POLLNVAL))))return 0;
        if(!ready)continue;
        ssize_t count=sending?send(m->fd,p,n,MSG_NOSIGNAL|MSG_DONTWAIT):recv(m->fd,p,n,MSG_DONTWAIT);
        if(count<0 && (errno==EAGAIN || errno==EINTR))continue;
        if(count<=0)return 0;
        p+=count;n-=(size_t)count;
    }
    return 1;
}
static int line(Document *d,char *buf,size_t cap) {
    for(size_t i=0;i+1<cap;i++) {
        if(!transfer(d,buf+i,1,0))return 0;
        if(buf[i]=='\n'){buf[i]=0;return 1;}
    }
    return 0;
}
void pd_model_close(Document *d) {
    if(!d->model)return;
    if(d->model->fd>=0)close(d->model->fd);
    if(d->model->pid>0) {
        kill(d->model->pid,SIGKILL);
        while(waitpid(d->model->pid,NULL,0)<0 && errno==EINTR){}
    }
    fz_free(d->ctx,d->model);d->model=NULL;
}
static void failed(Document *d,const char *why) {
    d->model_stats.failed++;
    snprintf(d->model_stats.status,sizeof(d->model_stats.status),"%s; algorithms retained",why);
}
/* Layout keeps these pages intact and automatic recognition has no source
 * evidence to verify. Do not spend inference time on a known unsupported path.
 * This uses the existing original-page threshold, not a document identity. */
static int model_eligible(Document *d,Page *p) {
    if(d->opt && d->opt->keep_pages && p->number<d->opt->keep_count && d->opt->keep_pages[p->number])return 0;
    return pd_page_glyphs(p)>=PD_MIN_PAGE_GLYPHS;
}
void pd_model_start(Document *d) {
    if(!d->opt || !d->opt->model)return;
    d->model_stats.requested=d->opt->model;
    int eligible=0;
    for(int i=0;i<d->npage;i++)eligible+=model_eligible(d,&d->pages[i]);
    if(!eligible) {
        snprintf(d->model_stats.backend,sizeof(d->model_stats.backend),"Not run");
        snprintf(d->model_stats.status,sizeof(d->model_stats.status),"Model skipped: pages have no usable text or are kept as originals; OCR is not supported");
        return;
    }
    char root[4000],worker[4096],weights[4096],projector[4096];
    ssize_t size=readlink("/proc/self/exe",root,sizeof(root)-1);
    if(size<1){failed(d,"Cannot locate bundled engine");return;}root[size]=0;
    char *slash=strrchr(root,'/');if(!slash){failed(d,"Cannot locate application directory");return;}*slash=0;
    if(snprintf(worker,sizeof(worker),"%s/paperdesk-model",root)>=(int)sizeof(worker))return;
    char folder[4096];
    if(d->opt->model_dir)snprintf(folder,sizeof(folder),"%s",d->opt->model_dir);
    else {
        snprintf(folder,sizeof(folder),"%s/models",root);
        if(access(folder,R_OK) && !strcmp(strrchr(root,'/')?strrchr(root,'/')+1:root,"build"))
            snprintf(folder,sizeof(folder),"%s/../models",root);
    }
    if(snprintf(weights,sizeof(weights),"%s/MinerU2.5-Pro-2605-1.2B.f16.gguf",folder)>=(int)sizeof(weights) ||
       snprintf(projector,sizeof(projector),"%s/MinerU2.5-Pro-2605-1.2B.mmproj-f16.gguf",folder)>=(int)sizeof(projector)) {
        failed(d,"Model path is too long");return;
    }
    if(access(worker,X_OK) || access(weights,R_OK) || access(projector,R_OK)) {
        failed(d,"Bundled engine or model files are missing");return;
    }
    d->model=fz_calloc(d->ctx,1,sizeof(PD_ModelProcess));d->model->fd=-1;d->model->page=-1;
    int sockets[2];
    if(socketpair(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0,sockets)) {failed(d,"Cannot open model channel");pd_model_close(d);return;}
    posix_spawn_file_actions_t actions;posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions,sockets[1],STDIN_FILENO);
    posix_spawn_file_actions_adddup2(&actions,sockets[1],STDOUT_FILENO);
    // Diagnostics remain on stderr. Model output can never be a command.
    posix_spawn_file_actions_addclose(&actions,sockets[0]);
    posix_spawn_file_actions_addclose(&actions,sockets[1]);
    char *args[]={worker,weights,projector,d->opt->model==2?"gpu":"cpu","mineru",NULL};
    int error=posix_spawn(&d->model->pid,worker,&actions,NULL,args,environ);
    posix_spawn_file_actions_destroy(&actions);close(sockets[1]);d->model->fd=sockets[0];
    if(error) {failed(d,"Cannot start bundled model engine");pd_model_close(d);return;}
    d->model->started=now();d->model->deadline=d->model->started+180;
    char ready[256];
    if(!line(d,ready,sizeof(ready)) || strncmp(ready,"PDREADY1 ",9)) {
        failed(d,"Model unavailable on the selected device");pd_model_close(d);return;
    }
    snprintf(d->model_stats.backend,sizeof(d->model_stats.backend),"%.159s",ready+9);
    snprintf(d->model_stats.status,sizeof(d->model_stats.status),"MinerU 2.5 Pro: source geometry checked");
    snprintf(d->model->backend,sizeof(d->model->backend),"%s",d->model_stats.backend);
    cache_identity(d,weights,projector,worker);
}
int pd_model_parse(Document *d,Page *p,const char *text) {
    p->nhints=0;
    const char *s=text;int count=0;
    while((s=strstr(s,"<|box_start|>"))) {
        if(count>=256)return 0;
        int coords[4],n=0;char role[32];s+=13;
        for(int i=0;i<4;i++) {
            char *end;errno=0;long v=strtol(s,&end,10);
            if(errno || end==s || v<0 || v>1000 || (i<3 && !isspace((unsigned char)*end)))return 0;
            coords[i]=(int)v;s=end;
            if(i<3)while(isspace((unsigned char)*s))s++;
        }
        if(sscanf(s,"<|box_end|><|ref_start|>%31[a-z_]<|ref_end|>%n",role,&n)!=1 || !n)return 0;
        s+=n;int x0=coords[0],y0=coords[1],x1=coords[2],y1=coords[3];
        if(x0<0 || y0<0 || x1>1000 || y1>1000 || x0>=x1 || y0>=y1)return 0;
        int rotation;
        if(!strncmp(s,"<|rotate_up|>",13))rotation=0;
        else if(!strncmp(s,"<|rotate_right|>",16))rotation=90;
        else if(!strncmp(s,"<|rotate_down|>",15))rotation=180;
        else if(!strncmp(s,"<|rotate_left|>",15))rotation=270;
        else return 0;
        // A fixed upper bound also keeps corrupt or adversarial output cheap.
        if(!p->hints)p->hints=fz_calloc(d->ctx,256,sizeof(ModelHint));
        ModelHint *hint=&p->hints[count++];memset(hint,0,sizeof(*hint));
        float w=(p->box.x1-p->box.x0)/1000,h=(p->box.y1-p->box.y0)/1000;
        hint->box=(PD_Rect){p->box.x0+x0*w,p->box.y0+y0*h,p->box.x0+x1*w,p->box.y0+y1*h};
        hint->raw_box=hint->box;
        hint->rotation=rotation;snprintf(hint->role,sizeof(hint->role),"%s",role);
        snprintf(hint->decision,sizeof(hint->decision),"Role retained for inspection");
    }
    if(!count)return 0;
    p->nhints=count;return 1;
}
static void hash_file(Document *d,fz_sha256 *hash,const char *path) {
    FILE *f=fopen(path,"rb");if(!f)return;
    unsigned char bytes[65536];size_t n,checked=0;
    fz_try(d->ctx) {
        while((n=fread(bytes,1,sizeof(bytes),f))) {
            fz_sha256_update(hash,bytes,n);checked+=n;
            /* Keep Stop responsive while hashing the multi-gigabyte weights.
             * Every byte still contributes to the same cache identity. */
            if(checked>=8*1024*1024) {
                pd_check(d,"Checking local model",0,d->npage);checked=0;
            }
        }
    }
    fz_always(d->ctx)fclose(f);
    fz_catch(d->ctx)fz_rethrow(d->ctx);
}
static void cache_identity(Document *d,const char *weights,const char *projector,const char *worker) {
    PD_ModelProcess *m=d->model;fz_sha256 hash;unsigned char digest[32];fz_sha256_init(&hash);
    hash_file(d,&hash,weights);hash_file(d,&hash,projector);hash_file(d,&hash,worker);
    fz_sha256_update(&hash,(const unsigned char*)m->backend,strlen(m->backend));
    fz_sha256_final(&hash,digest);
    for(int i=0;i<32;i++)snprintf(m->signature+i*2,3,"%02x",digest[i]);
    const char *base=getenv("XDG_CACHE_HOME"),*home=getenv("HOME");char root[3800];
    if(base && *base)snprintf(root,sizeof(root),"%s",base);
    else if(home && *home)snprintf(root,sizeof(root),"%s/.cache",home);
    else return;
    if(mkdir(root,0700) && errno!=EEXIST)return;
    if(snprintf(m->cache,sizeof(m->cache),"%s/paperdesk-recognition-v1",root)>=(int)sizeof(m->cache)){m->cache[0]=0;return;}
    if(mkdir(m->cache,0700) && errno!=EEXIST)m->cache[0]=0;
}
char *pd_model_region(Document *d,Page *p,PD_Rect box,int rotation,int task,int attempt,double *seconds,int *cached) {
    if(seconds)*seconds=0;
    if(cached)*cached=0;
    if(!d->model || task<0 || task>3)return NULL;
    fz_context *ctx=d->ctx;fz_pixmap *pix=NULL;fz_page *page=NULL;fz_device *dev=NULL;char *result=NULL;
    fz_var(pix);fz_var(page);fz_var(dev);fz_var(result);
    fz_try(ctx) {
        PD_ModelProcess *m=d->model;m->page=(int)(p-d->pages);m->started=now();m->deadline=m->started+1800;
        if(task==0)snprintf(m->stage,sizeof(m->stage),"Model: page layout");
        else snprintf(m->stage,sizeof(m->stage),"Model: %s, region %d%s",task==1?"text":task==2?"formula":"table",p->nrecon,attempt?", retry":"");
        pd_check(d,m->stage,m->page+1,d->npage);
        page=fz_load_page(ctx,d->pdf,p->number);
        fz_matrix matrix;int width,height;
        if(task==0) {
            width=height=1036;matrix=fz_concat(fz_translate(-p->box.x0,-p->box.y0),fz_scale(1036/(p->box.x1-p->box.x0),1036/(p->box.y1-p->box.y0)));
        } else {
            box=from_fz(fz_intersect_rect(to_fz(rect_pad(box,.6f)),to_fz(p->box)));
            float w=box.x1-box.x0,h=box.y1-box.y0;
            if(w<.1f || h<.1f)fz_throw(ctx,FZ_ERROR_ARGUMENT,"Empty recognition region");
            matrix=fz_concat(fz_translate(-box.x0,-box.y0),fz_rotate(rotation));
            fz_rect bound=fz_transform_rect((fz_rect){0,0,w,h},fz_rotate(rotation));
            matrix=fz_concat(matrix,fz_translate(-bound.x0,-bound.y0));
            w=bound.x1-bound.x0;h=bound.y1-bound.y0;
            float scale=4.f;
            scale=fminf(scale,sqrtf(1600000.f/(w*h)));scale=fminf(scale,4000/fmaxf(w,h));
            int actualw=(int)ceilf(w*scale),actualh=(int)ceilf(h*scale);
            width=actualw;height=actualh;
            if(width>height*50)height=(width+49)/50;
            if(height>width*50)width=(height+49)/50;
            if(width<28)width=28;
            if(height<28)height=28;
            matrix=fz_concat(matrix,fz_scale(scale,scale));
            matrix=fz_concat(matrix,fz_translate((width-actualw)*.5f,(height-actualh)*.5f));
        }
        pix=fz_new_pixmap_with_bbox(ctx,fz_device_rgb(ctx),(fz_irect){0,0,width,height},NULL,0);
        fz_clear_pixmap_with_value(ctx,pix,255);dev=fz_new_draw_device(ctx,matrix,pix);
        fz_run_page(ctx,page,dev,fz_identity,NULL);fz_close_device(ctx,dev);
        unsigned bytes=(unsigned)(width*height*3);int limit=task==3?8192:4096;
        char header[160],path[4096]={0};
        int n=snprintf(header,sizeof(header),"PDREQ1 %d %d %u %d %d\n",width,height,bytes,task,limit);
        if(m->cache[0]) {
            fz_sha256 sha;unsigned char digest[32];char key[65];fz_sha256_init(&sha);
            fz_sha256_update(&sha,(const unsigned char*)m->signature,64);
            fz_sha256_update(&sha,(const unsigned char*)header,(size_t)n);
            fz_sha256_update(&sha,fz_pixmap_samples(ctx,pix),bytes);fz_sha256_final(&sha,digest);
            for(int i=0;i<32;i++)snprintf(key+2*i,3,"%02x",digest[i]);
            snprintf(path,sizeof(path),"%s/%s.txt",m->cache,key);
            FILE *cache=fopen(path,"rb");
            if(cache) {
                if(!fseek(cache,0,SEEK_END)) {
                    long length=ftell(cache);rewind(cache);
                    if(length>0 && length<=262144) {
                        result=fz_calloc(ctx,(size_t)length+1,1);
                        if(fread(result,1,(size_t)length,cache)!=(size_t)length){fz_free(ctx,result);result=NULL;}
                        else if(cached)*cached=1;
                    }
                }
                fclose(cache);
            }
        }
        if(!result) {
            if(!transfer(d,header,(size_t)n,1) || !transfer(d,fz_pixmap_samples(ctx,pix),bytes,1) || !line(d,header,sizeof(header))) {
                failed(d,"Model process stopped or timed out");pd_model_close(d);
            } else {
                int status,tokens;unsigned length;double elapsed;char extra;
                if(sscanf(header,"PDRES1 %d %u %d %lf %c",&status,&length,&tokens,&elapsed,&extra)!=4 ||
                   length>262656 || !isfinite(elapsed) || elapsed<0 || status<0 || status>3 || tokens<0 || tokens>limit) {
                    failed(d,"Invalid model response");pd_model_close(d);
                } else {
                    result=fz_calloc(ctx,length+1,1);
                    if(!transfer(d,result,length,0)) {failed(d,"Incomplete model response");pd_model_close(d);fz_free(ctx,result);result=NULL;}
                    else {
                        if(seconds)*seconds=elapsed;
                        d->model_stats.seconds+=elapsed;
                        if(status || !length || memchr(result,0,length)) {failed(d,status==2?"Model response truncated":"Unusable recognition response");fz_free(ctx,result);result=NULL;}
                        else if(path[0]) {
                            char temp[4096];snprintf(temp,sizeof(temp),"%.4000s.part-XXXXXX",path);int fd=mkstemp(temp);
                            if(fd>=0){ssize_t wrote=write(fd,result,length);close(fd);if(wrote==(ssize_t)length){if(rename(temp,path))unlink(temp);}else unlink(temp);}
                        }
                    }
                }
            }
        }
    }
    fz_always(ctx){fz_drop_device(ctx,dev);fz_drop_pixmap(ctx,pix);fz_drop_page(ctx,page);}
    fz_catch(ctx){fz_free(ctx,result);fz_rethrow(ctx);}
    return result;
}
void pd_model_page(Document *d,Page *p) {
    if(d->opt && d->opt->model && !model_eligible(d,p)){p->model_status=2;return;}
    if(!d->model)return;
    int cached=0;p->model_output=pd_model_region(d,p,p->box,0,0,0,&p->model_seconds,&cached);
    if(p->model_output && pd_model_parse(d,p,p->model_output)){p->model_status=0;d->model_stats.pages++;}
    else {p->nhints=0;p->model_status=1;if(p->model_output)failed(d,"Unusable model layout");}
}
fz_buffer *pd_model_report(Document *d) {
    fz_context *ctx=d->ctx;fz_buffer *out=fz_new_buffer(ctx,4096);
    fz_try(ctx) {
        fz_append_string(ctx,out,"{\"schema\":1,\"model\":\"MinerU2.5-Pro-2605-1.2B f16\",\"engine_commit\":\"50e3e3e480c80b9ee0ea067e475602d540dc62c8\",\"backend\":");
        pd_json(ctx,out,d->model_stats.backend);fz_append_string(ctx,out,",\"status\":");pd_json(ctx,out,d->model_stats.status);
        int skipped=0;for(int i=0;i<d->npage;i++)skipped+=d->pages[i].model_status==2;
        fz_append_printf(ctx,out,",\"requested\":%d,\"pages_evaluated\":%d,\"pages_skipped\":%d,\"accepted_regions\":%d,\"failures\":%d,\"inference_seconds\":%.3f,\"pages\":[",
            d->model_stats.requested,d->model_stats.pages,skipped,d->model_stats.applied,d->model_stats.failed,d->model_stats.seconds);
        for(int i=0;i<d->npage;i++) {
            Page *p=&d->pages[i];fz_append_printf(ctx,out,"%s{\"page\":%d,\"status\":%d,\"seconds\":%.3f,\"raw\":",i?",":"",p->number+1,p->model_status,p->model_seconds);
            /* Status 2 is a deliberate capability skip, not a model failure. */
            pd_json(ctx,out,p->model_output?p->model_output:"");fz_append_string(ctx,out,",\"regions\":[");
            for(int j=0;j<p->nhints;j++) {
                ModelHint *h=&p->hints[j];
                fz_append_printf(ctx,out,"%s{\"role\":",j?",":"");pd_json(ctx,out,h->role);
                fz_append_printf(ctx,out,",\"raw_bbox\":[%.3f,%.3f,%.3f,%.3f]",h->raw_box.x0,h->raw_box.y0,h->raw_box.x1,h->raw_box.y1);
                fz_append_printf(ctx,out,",\"bbox\":[%.3f,%.3f,%.3f,%.3f],\"accepted\":%s,\"decision\":",h->box.x0,h->box.y0,h->box.x1,h->box.y1,h->applied?"true":"false");
                pd_json(ctx,out,h->decision);fz_append_byte(ctx,out,'}');
            }
            fz_append_string(ctx,out,"],\"algorithmic_objects\":[");
            for(int j=0;j<p->nbaseline;j++) {
                PD_Block *b=&p->baseline[j];
                fz_append_printf(ctx,out,"%s{\"kind\":",j?",":"");pd_json(ctx,out,pd_kind_name(b->kind));
                fz_append_printf(ctx,out,",\"bbox\":[%.3f,%.3f,%.3f,%.3f],\"glyphs\":%d}",b->box.x0,b->box.y0,b->box.x1,b->box.y1,b->glyphs);
            }
            fz_append_string(ctx,out,"]}");
        }
        fz_append_string(ctx,out,"]}");
    }
    fz_catch(ctx){fz_drop_buffer(ctx,out);fz_rethrow(ctx);}
    return out;
}
