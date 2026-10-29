/* GemmaVideo native Linux launcher. AGPL-3.0-or-later. */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char executable[PATH_MAX], app[PATH_MAX];
    ssize_t n=readlink("/proc/self/exe",executable,sizeof(executable)-1);
    if(n<0 || n>=(ssize_t)sizeof(executable)-1){perror("GemmaVideo: percorso eseguibile");return 1;}
    executable[n]='\0';
    char *slash=strrchr(executable,'/');
    if(!slash){fprintf(stderr,"GemmaVideo: cartella non trovata\n");return 1;}
    *slash='\0';
    int length=snprintf(app,sizeof(app),"%s/video_gemma/app.py",executable);
    if(length<0 || length>=(int)sizeof(app)){fprintf(stderr,"GemmaVideo: percorso troppo lungo\n");return 1;}
    char runner[PATH_MAX];
    length=snprintf(runner,sizeof(runner),"%s/scripts/python.sh",executable);
    if(length<0 || length>=(int)sizeof(runner))return 1;
    const char *python=runner;
    if(access(python,X_OK) || access(app,R_OK)){perror("GemmaVideo: componente necessario non disponibile");return 1;}
    if(setenv("OMP_NUM_THREADS","4",1) || setenv("OPENBLAS_NUM_THREADS","4",1) || setenv("PYTHONNOUSERSITE","1",1)){
        perror("GemmaVideo: configurazione ambiente");return 1;
    }
    char **args=calloc((size_t)argc+2,sizeof(*args));
    if(!args){perror("GemmaVideo");return 1;}
    args[0]=(char *)python;args[1]=app;
    for(int i=1;i<argc;i++)args[i+1]=argv[i];
    execv(python,args);
    perror("GemmaVideo: avvio fallito");free(args);return 1;
}
