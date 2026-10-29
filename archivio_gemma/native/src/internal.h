#ifndef PAPERDESK_INTERNAL_H
#define PAPERDESK_INTERNAL_H
#define _POSIX_C_SOURCE 200809L
#include "paper.h"
#include <mupdf/fitz.h>
#include <stdint.h>

enum { G_MATH=1, G_BOLD=2, G_ITALIC=4, G_BAD=8, G_INLINE=16, G_GAP=32, G_UNDERLINE=64, G_SYNTHETIC=128,
       G_DOUBLE_STRUCK=256, G_SCRIPT=512, G_MATH_VARIANT=G_DOUBLE_STRUCK|G_SCRIPT };
typedef struct {
    int c, flags;
    PD_Rect box;
    float size, baseline;
    int ref; /* index + 1 in Document.refs, zero when not a matched note callout */
    int native; /* reconstruction index + 1 for the beginning of a math span */
} Glyph;
typedef struct {
    Glyph *glyph;
    int n, cap, owner, source_block;
    PD_Rect box;
    float size, baseline, dx, dy;
    int math, bad, letters, digits, bold, caption, heading, margin, words, prose, front, dropcap, note_start;
    char *text;
} Line;
typedef struct { PD_Rect box; int image, owner; } Mark;
typedef struct {
    PD_Block pub;
    int *lines, n, cap;
    int indent, join_next, note_prefix; /* indent: 1 first-line, -1 hanging */
} Block;
typedef struct {
    PD_Rect box,raw_box;
    char role[32], decision[120];
    int rotation, applied;
} ModelHint;
typedef struct {
    PD_Rect box,retry_box;int task,block,line,first,end,accepted,cached,rotation,visible,manual,attempts,display,skipped;
    char *raw,*markup;char reason[192];double seconds;
} Reconstruction;
typedef struct PD_ModelProcess PD_ModelProcess;
typedef struct {
    int page,line,first,end,note,omitted;
    char anchor[96];
} NoteRef;
typedef struct {
    PD_Rect box;
    int number;
    Line *lines; int nline, cline;
    Mark *marks; int nmark, cmark;
    Mark *source_marks; int nsource_mark; /* immutable vector/image evidence, before layout joins */
    Block *blocks; int nblock, cblock;
    PD_Block *baseline; int nbaseline; /* independent algorithmic proposal, before model */
    float body, leading;
    int complex_graphics, exported, parts, front_sheet;
    char anchor[96];
    ModelHint *hints; int nhints;
    char *model_output;
    Reconstruction *recon;int nrecon,crecon;
    double model_seconds;
    int model_status;
} Page;
static inline const Mark *pd_math_marks(Page *p,int *count) {
    *count=p->source_marks?p->nsource_mark:p->nmark;
    return p->source_marks?p->source_marks:p->marks;
}
enum { PD_MIN_PAGE_GLYPHS=12 };
static inline int pd_page_glyphs(const Page *page) {
    int n=0;for(int i=0;i<page->nline;i++)n+=page->lines[i].n;
    return n;
}
typedef struct {
    fz_context *ctx;
    fz_document *pdf;
    Page *pages; int npage, physical_pages;
    float body;
    char title[512];
    char author[512],reference[160];
    char source_digest[65];
    const PD_Options *opt;
    PD_ModelProcess *model;
    PD_ModelStats model_stats;
    NoteRef *refs;int nref,cref;
} Document;

PD_Rect rect_union(PD_Rect a, PD_Rect b);
PD_Rect rect_pad(PD_Rect a, float pad);
int rect_overlap(PD_Rect a, PD_Rect b);
float rect_area(PD_Rect a);
float rect_x_overlap(PD_Rect a, PD_Rect b);
PD_Rect from_fz(fz_rect r);
fz_rect to_fz(PD_Rect r);
void *pd_grow(fz_context *ctx, void *ptr, int *cap, int need, size_t item);
void pd_xml(fz_context *ctx, fz_buffer *b, const char *s);
void pd_json(fz_context *ctx, fz_buffer *b, const char *s);
void pd_utf8(fz_context *ctx, fz_buffer *b, int c);
int pd_xml_scalar(int c);
void pd_source_digest(Document *doc,const char *path);
void pd_check(Document *doc, const char *stage, int done, int total);
void pd_extract(Document *doc);
void pd_layout(Document *doc);
int pd_glyph_script(Line *line,Glyph *glyph); /* 1 superscript, -1 subscript */
int pd_note_label(Line *line,char label[16]); /* leading marker, glyph count */
void pd_footnotes(Document *doc);
void pd_note_candidates(Page *page);
void pd_model_start(Document *doc);
void pd_model_page(Document *doc,Page *page);
void pd_model_close(Document *doc);
void pd_model_regions(Document *doc,Page *page);
void pd_model_compare(Document *doc,Page *page);
int pd_model_parse(Document *doc,Page *page,const char *text);
fz_buffer *pd_model_report(Document *doc);
char *pd_model_region(Document *doc,Page *page,PD_Rect box,int rotation,int task,int attempt,double *seconds,int *cached);
char *pd_mathml(fz_context *ctx,const char *latex,int display,char *reason,size_t capacity);
char *pd_math_flow(fz_context *ctx,const char *markup,int reader);
fz_buffer *pd_kepub_xhtml(fz_context *ctx,fz_buffer *input);
int pd_math_font_variant(const char *name);
void pd_math_glyph(fz_context *ctx,fz_buffer *out,const Glyph *glyph);
int pd_math_accent(int c); /* canonical printed accent, zero otherwise */
int pd_math_box_edited(Document *d,Page *p,PD_Rect box);
static inline int pd_math_space(int c) { return c<=32 || c==0xa0 || c==0x200b || c==0xfeff; }
int pd_source_math(Document *doc,Page *page,Reconstruction *region);
int pd_verify_ordered_math(Document *doc,Page *page,Reconstruction *region,const Glyph **order,int count);
void pd_reconstruct(Document *doc);
int pd_verify_reconstruction(Document *doc,Page *page,Reconstruction *region);
int pd_recognition_retry_box(Document *doc,Page *page,Reconstruction *region,PD_Rect *box);
fz_buffer *pd_reconstruction_report(Document *doc);
void pd_book_metadata(Document *doc);
int pd_publication_sheet(Page *page);
void pd_page_details(Document *doc,Page *page);
void pd_metadata_text(char *out,size_t capacity,const char *text);
fz_buffer *pd_cover_png(Document *doc);
void pd_write_epub(Document *doc, const char *out, PD_Result *result);
void pd_drop_document(Document *doc);
Block *pd_add_block(Document *doc, Page *page, PD_Kind kind, PD_Rect box, const char *why);
void pd_claim_line(Document *doc, Page *page, Block *block, int index);

typedef struct PD_Zip PD_Zip;
PD_Zip *pd_zip_open(fz_context *ctx,const char *path);
void pd_zip_entry(fz_context *ctx,PD_Zip *zip,const char *name,fz_buffer *data,int compress);
void pd_zip_finish(fz_context *ctx,PD_Zip *zip);
void pd_zip_drop(fz_context *ctx,PD_Zip *zip);

#endif
