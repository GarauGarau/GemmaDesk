#ifndef PAPERDESK_H
#define PAPERDESK_H

#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif

#define PD_VERSION "0.4.3"
typedef struct { float x0, y0, x1, y1; } PD_Rect;
typedef enum {
    PD_TEXT, PD_HEADING, PD_TABLE, PD_FIGURE, PD_EQUATION,
    PD_NOTE, PD_MARGIN, PD_PAGE_IMAGE, PD_RULE, PD_DETAIL
} PD_Kind;
enum { PD_REFERENCES=1, PD_APPENDIX=2 };
enum { PD_READER_COMPATIBLE, PD_READER_KOREADER, PD_READER_KOBO };

typedef struct {
    int requested, pages, applied, failed;
    double seconds;
    char backend[160], status[240];
} PD_ModelStats;

typedef struct {
    int id, page, level, rotation, page_rotation, glyphs, review;
    PD_Kind kind;
    int section, omitted; /* section bitmask; excluded content stays inspectable */
    int continues; /* preceding source block in the same paragraph, or zero */
    int note; /* linked footnote group id, or zero */
    int front; /* 1 article title, 2 author/affiliation, 3 abstract */
    int native; /* reconstruction index + 1 on source page; zero when absent */
    PD_Rect box;
    char label[200], reason[240], asset[96], reader_asset[96], anchor[96];
} PD_Block;

typedef struct {
    int page,index,block,task,accepted,visible,manual,skipped;
    PD_Rect box;
    char *raw,*markup;
    char reason[192];
} PD_Recognition;
typedef struct {
    int page,task,action; /* 1: use inspected content; 2: retain original */
    PD_Rect box;
    const char *text;
} PD_Correction;

typedef struct {
    char title[512], source[4096], output[4096];
    char author[512];
    int pages, block_count, glyphs, assigned_glyphs, image_pages, review_count, reader;
    int text_blocks, tables, figures, equations, footnotes;
    int omitted_blocks, omitted_glyphs;
    PD_Block *blocks;
    double seconds;
    PD_ModelStats model;
    int detected_math,native_math,recognized_tables,native_tables,recognized_text,unresolved,cache_hits;
    int visible_math,visible_tables,manual_count,recognition_count,reconstruction_skips;
    PD_Recognition *recognitions;
    char source_digest[65];
} PD_Result;

/* The callback returns nonzero to cancel. Called only on the worker thread. */
typedef int (*PD_Progress)(void *user, const char *stage, int done, int total);
typedef struct { int pages, text_pages, image_pages, glyphs; } PD_InputInfo;
/* Called on the worker after extraction, before inference or export.
 * Return nonzero to stop conversion of unsupported input. */
typedef int (*PD_InputCheck)(void *user, const PD_InputInfo *info);
typedef struct {
    int first_page, last_page; /* 1-based inclusive; zero means full document */
    int rotate_wide; /* portrait reader, rotation lock; turn wide tables/figures */
    int exclude_references, exclude_appendices; /* zero preserves all content */
    int model; /* 0: algorithms only, 1: bundled model on CPU, 2: on GPU */
    int reader; /* PD_READER_*; Kobo uses the .kepub.epub extension */
    const PD_Correction *corrections;
    int correction_count;
    const char *correction_digest; /* SHA256 of the exact PDF inspected */
    const char *model_dir; /* optional explicit companion pack; NULL: bundled */
    const char *book_title, *book_author; /* NULL: source metadata; empty author is allowed */
    const unsigned char *keep_pages; /* optional, indexed by physical PDF page */
    int keep_count;
    PD_Progress progress;
    PD_InputCheck check_input; /* optional; NULL retains original-page export */
    void *user;
} PD_Options;

int pd_convert(const char *input, const char *output, const PD_Options *options,
               PD_Result **result, char *error, size_t error_size);
void pd_free_result(PD_Result *result);
int pd_check_recognition(int task,const char *text,char *error,size_t size);
int pd_load_corrections(const char *source,PD_Correction **edits,int *count,char digest[65],char *error,size_t size);
int pd_save_corrections(const char *source,const char *digest,const PD_Correction *edits,int count,char *error,size_t size);
void pd_free_corrections(PD_Correction *edits,int count);
const char *pd_kind_name(PD_Kind kind);
int pd_drop_path(const char *text,size_t length,char *path,size_t capacity);
typedef struct {char title[512],author[512],reference[160];} PD_BookInfo;
int pd_book_info(const char *path,PD_BookInfo *info,char *error,size_t error_size);

typedef struct PD_View PD_View;
typedef struct { unsigned char *samples; int w, h, stride; float scale; } PD_Bitmap;
PD_View *pd_view_open(const char *path, char *error, size_t error_size);
PD_View *pd_view_asset(const char *epub, const char *entry, char *error, size_t error_size);
void pd_view_close(PD_View *view);
int pd_view_pages(PD_View *view);
int pd_view_reflow(PD_View *view, float width, float height, float font_size);
int pd_view_is_reflowable(PD_View *view);
PD_Rect pd_view_bounds(PD_View *view, int page);
int pd_view_resolve(PD_View *view, const char *anchor);
int pd_view_link_at(PD_View *view,int page,float x,float y); /* internal links only; -1 if absent */
int pd_view_render(PD_View *view, int page, float scale, int rotation,
                   PD_Bitmap *bitmap, char *error, size_t error_size);
void pd_bitmap_free(PD_Bitmap *bitmap);
int pd_bitmap_save(const PD_Bitmap *bitmap,const char *path,char *error,size_t error_size);
int pd_view_snapshot(PD_View *view, int page, const char *path, float scale,
                     char *error, size_t error_size);
int pd_view_text(PD_View *view, int page, const char *path, char *error, size_t error_size);

typedef struct PD_Send PD_Send;
PD_Send *pd_send_start(const char *epub, const char *download_name, int loopback, char *error, size_t error_size);
void pd_send_stop(PD_Send *send);
const char *pd_send_address(PD_Send *send);
const char *pd_send_code(PD_Send *send);
int pd_send_downloads(PD_Send *send);
int pd_send_active(PD_Send *send);

#ifdef __cplusplus
}
#endif
#endif
