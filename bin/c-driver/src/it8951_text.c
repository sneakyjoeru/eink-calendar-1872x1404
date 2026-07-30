/*
 * it8951_text.c — Text rendering for IT8951 using FreeType
 *
 * Renders antialiased text with 3× supersampling, then downscales with
 * bilinear interpolation and sends as 8bpp to the display.
 */
#include "it8951_driver.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ft2build.h>
#include FT_FREETYPE_H
#include <math.h>

/* Bilinear downscale by factor (e.g. 3) */
static void downscale_bilinear(const uint8_t *src, int sw, int sh,
                               uint8_t *dst, int dw, int dh)
{
    for (int dy = 0; dy < dh; dy++) {
        float sy = (float)dy * sh / dh;
        int sy0 = (int)sy;
        int sy1 = (sy0 + 1 < sh) ? sy0 + 1 : sy0;
        float fy = sy - sy0;
        for (int dx = 0; dx < dw; dx++) {
            float sx = (float)dx * sw / dw;
            int sx0 = (int)sx;
            int sx1 = (sx0 + 1 < sw) ? sx0 + 1 : sx0;
            float fx = sx - sx0;
            float v = (1-fx)*(1-fy) * src[sy0*sw+sx0] +
                      fx*(1-fy)     * src[sy0*sw+sx1] +
                      (1-fx)*fy     * src[sy1*sw+sx0] +
                      fx*fy         * src[sy1*sw+sx1];
            dst[dy*dw+dx] = (uint8_t)(v + 0.5);
        }
    }
}

int it8951_display_text(it8951_t *dev, const char *text, int font_size,
                        const char *font_path, uint8_t bg_color, uint8_t fg_color,
                        uint16_t mode)
{
    FT_Library ft;
    FT_Face face;

    if (FT_Init_FreeType(&ft)) {
        fprintf(stderr, "Failed to init FreeType\n");
        return -1;
    }

    const char *font = font_path;
    if (!font || !*font) {
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf";
    }

    if (FT_New_Face(ft, font, 0, &face)) {
        fprintf(stderr, "Failed to load font: %s\n", font);
        FT_Done_FreeType(ft);
        return -1;
    }

    int screen_w = dev->info.panel_w;
    int screen_h = dev->info.panel_h;

    /* 3× supersampling */
    int scale = 3;
    int render_w = screen_w * scale;
    int render_h = screen_h * scale;

    /* Split text into lines */
    char *text_copy = strdup(text);
    char *lines[64];
    int nlines = 0;
    char *tok = strtok(text_copy, "\n");
    while (tok && nlines < 64) {
        lines[nlines++] = tok;
        tok = strtok(NULL, "\n");
    }

    /* Try decreasing font sizes until it fits */
    int try_size = font_size * scale;
    int total_h = 0, max_w = 0;
    int line_height = 0;

    for (; try_size > 8; try_size -= 2) {
        FT_Set_Pixel_Sizes(face, 0, try_size);
        max_w = 0;
        total_h = 0;
        line_height = (int)(try_size * 1.4);

        for (int i = 0; i < nlines; i++) {
            int lw = 0;
            for (const char *p = lines[i]; *p; ) {
                FT_UInt glyph_index = FT_Get_Char_Index(face, (FT_ULong)*p);
                FT_Load_Glyph(face, glyph_index, FT_LOAD_DEFAULT);
                lw += face->glyph->advance.x >> 6;
                p++;
            }
            if (lw > max_w) max_w = lw;
            total_h += line_height;
        }

        if (max_w <= render_w - 60 && total_h <= render_h - 60)
            break;
    }

    /* Render at high resolution */
    uint8_t *canvas = malloc(render_w * render_h);
    memset(canvas, bg_color, render_w * render_h);

    FT_Set_Pixel_Sizes(face, 0, try_size);
    int y_start = (render_h - total_h) / 2;

    for (int i = 0; i < nlines; i++) {
        /* Measure line width */
        int lw = 0;
        for (const char *p = lines[i]; *p; ) {
            FT_UInt glyph_index = FT_Get_Char_Index(face, (FT_ULong)*p);
            FT_Load_Glyph(face, glyph_index, FT_LOAD_DEFAULT);
            lw += face->glyph->advance.x >> 6;
            p++;
        }
        int x = (render_w - lw) / 2;

        /* Render each character */
        for (const char *p = lines[i]; *p; ) {
            FT_UInt glyph_index = FT_Get_Char_Index(face, (FT_ULong)*p);
            if (FT_Load_Glyph(face, glyph_index, FT_LOAD_RENDER) == 0) {
                FT_Bitmap *bmp = &face->glyph->bitmap;
                int bx = x + face->glyph->bitmap_left;
                int by = y_start + (try_size - face->glyph->bitmap_top);

                for (int row = 0; row < bmp->rows; row++) {
                    int py = by + row;
                    if (py < 0 || py >= render_h) continue;
                    for (int col = 0; col < bmp->width; col++) {
                        int px = bx + col;
                        if (px < 0 || px >= render_w) continue;
                        uint8_t alpha = bmp->buffer[row * bmp->pitch + col];
                        if (alpha == 0) continue;
                        float a = alpha / 255.0;
                        canvas[py * render_w + px] = (uint8_t)(bg_color * (1-a) + fg_color * a + 0.5);
                    }
                }
                x += face->glyph->advance.x >> 6;
            }
            p++;
        }
        y_start += line_height;
    }

    /* Downscale to screen resolution */
    uint8_t *screen = malloc(screen_w * screen_h);
    downscale_bilinear(canvas, render_w, render_h, screen, screen_w, screen_h);

    /* Display as 8bpp (no dithering for text — clean edges) */
    it8951_display_8bpp(dev, screen, 0, 0, screen_w, screen_h, mode);

    free(screen);
    free(canvas);
    free(text_copy);
    FT_Done_Face(face);
    FT_Done_FreeType(ft);

    return 0;
}