/*
 * it8951_image.c — Image display for IT8951 using stb_image
 *
 * Loads an image, converts to grayscale, auto-scales to fit screen
 * preserving aspect ratio, centers on background, applies brightness,
 * Floyd-Steinberg dithers to 16 levels, sends as 8bpp.
 */
#include "it8951_driver.h"

#define STB_IMAGE_IMPLEMENTATION
#include "stb_image.h"
#include "stb_image_write.h"

#define LAST_IMG_PATH "/tmp/it8951_last.png"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

/* Bilinear resize */
static void resize_bilinear_gray(const uint8_t *src, int sw, int sh,
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

int it8951_display_image(it8951_t *dev, const char *path,
                         uint8_t bg_color, float brightness, uint16_t mode)
{
    int img_w, img_h, img_ch;
    unsigned char *img = stbi_load(path, &img_w, &img_h, &img_ch, 1); /* force grayscale */
    if (!img) {
        fprintf(stderr, "Failed to load image: %s\n", path);
        return -1;
    }

    printf("Original size: %d x %d\n", img_w, img_h);

    int screen_w = dev->info.panel_w;
    int screen_h = dev->info.panel_h;

    /* Auto-scale to fit screen, preserving aspect ratio */
    float scale = fminf((float)screen_w / img_w, (float)screen_h / img_h);
    int new_w, new_h;
    if (scale < 1.0) {
        new_w = (int)(img_w * scale);
        new_h = (int)(img_h * scale);
    } else {
        new_w = img_w;
        new_h = img_h;
    }

    /* Resize image */
    uint8_t *resized = malloc(new_w * new_h);
    if (new_w != img_w || new_h != img_h) {
        resize_bilinear_gray(img, img_w, img_h, resized, new_w, new_h);
    } else {
        memcpy(resized, img, new_w * new_h);
    }

    /* Create canvas with background color */
    uint8_t *canvas = malloc(screen_w * screen_h);
    memset(canvas, bg_color, screen_w * screen_h);

    /* Center image on canvas */
    int off_x = (screen_w - new_w) / 2;
    int off_y = (screen_h - new_h) / 2;

    /* Precompute gamma LUT once (instant lookup, no per-pixel pow) */
    uint8_t gamma_lut[256];
    if (brightness != 1.0f) {
        float inv_gamma = 1.0f / brightness;
        for (int i = 0; i < 256; i++) {
            float norm = (float)i / 255.0f;
            float corrected = powf(norm, inv_gamma);
            int v = (int)(corrected * 255.0f + 0.5f);
            gamma_lut[i] = (v < 0) ? 0 : (v > 255 ? 255 : (uint8_t)v);
        }
    } else {
        for (int i = 0; i < 256; i++) gamma_lut[i] = (uint8_t)i;
    }

    /* Paste and apply gamma boost to non-background pixels */
    for (int y = 0; y < new_h; y++) {
        int py = off_y + y;
        if (py < 0 || py >= screen_h) continue;
        for (int x = 0; x < new_w; x++) {
            int px = off_x + x;
            if (px < 0 || px >= screen_w) continue;
            uint8_t val = resized[y * new_w + x];
            if (abs(val - bg_color) > 2) {
                val = gamma_lut[val];
            }
            canvas[py * screen_w + px] = val;
        }
    }

    /* Display as 8bpp (no dithering — preserves antialiased edges).
       Previously used 4bpp packing (canvas/17) which quantized to 16 levels
       and destroyed font antialiasing. 8bpp keeps all 256 gray levels. */
    it8951_display_8bpp(dev, canvas, 0, 0, screen_w, screen_h, mode);

    free(canvas);
    free(resized);
    stbi_image_free(img);

    return 0;
}

/*
 * 4bpp image display — quantizes 8bpp grayscale to 16 levels (4-bit nibbles)
 * and sends via it8951_display_4bpp. The IT8951 4bpp pixel format packs two
 * pixels per byte. The hardware applies its own 16-level waveform which may
 * produce different rendering artifacts than 8bpp.
 */
int it8951_display_image_4bpp(it8951_t *dev, const char *path,
                              uint8_t bg_color, float brightness, uint16_t mode)
{
    int img_w, img_h, img_ch;
    unsigned char *img = stbi_load(path, &img_w, &img_h, &img_ch, 1);
    if (!img) {
        fprintf(stderr, "Failed to load image: %s\n", path);
        return -1;
    }

    printf("Original size: %d x %d (4bpp)\n", img_w, img_h);

    int screen_w = dev->info.panel_w;
    int screen_h = dev->info.panel_h;

    /* Auto-scale to fit screen, preserving aspect ratio */
    float scale = fminf((float)screen_w / img_w, (float)screen_h / img_h);
    int new_w, new_h;
    if (scale < 1.0) {
        new_w = (int)(img_w * scale);
        new_h = (int)(img_h * scale);
    } else {
        new_w = img_w;
        new_h = img_h;
    }

    /* Resize image */
    uint8_t *resized = malloc(new_w * new_h);
    if (new_w != img_w || new_h != img_h) {
        resize_bilinear_gray(img, img_w, img_h, resized, new_w, new_h);
    } else {
        memcpy(resized, img, new_w * new_h);
    }

    /* Create canvas with background color */
    uint8_t *canvas = malloc(screen_w * screen_h);
    memset(canvas, bg_color, screen_w * screen_h);

    int off_x = (screen_w - new_w) / 2;
    int off_y = (screen_h - new_h) / 2;

    /* Gamma LUT */
    uint8_t gamma_lut[256];
    if (brightness != 1.0f) {
        float inv_gamma = 1.0f / brightness;
        for (int i = 0; i < 256; i++) {
            float norm = (float)i / 255.0f;
            float corrected = powf(norm, inv_gamma);
            int v = (int)(corrected * 255.0f + 0.5f);
            gamma_lut[i] = (v < 0) ? 0 : (v > 255 ? 255 : (uint8_t)v);
        }
    } else {
        for (int i = 0; i < 256; i++) gamma_lut[i] = (uint8_t)i;
    }

    /* Paste and apply gamma boost to non-background pixels */
    for (int y = 0; y < new_h; y++) {
        int py = off_y + y;
        if (py < 0 || py >= screen_h) continue;
        for (int x = 0; x < new_w; x++) {
            int px = off_x + x;
            if (px < 0 || px >= screen_w) continue;
            uint8_t val = resized[y * new_w + x];
            if (abs(val - bg_color) > 2) {
                val = gamma_lut[val];
            }
            canvas[py * screen_w + px] = val;
        }
    }

    /* Convert 8bpp grayscale to 4bpp nibbles.
       8bpp: 0=black, 255=white (PIL convention).
       4bpp: 0=white, 15=black (IT8951 convention — display_4bpp inverts).
       So: nibble = (255 - gray) * 15 / 255 = (255 - gray) / 17 */
    int total_pixels = screen_w * screen_h;
    uint8_t *gray4 = malloc(total_pixels);
    for (int i = 0; i < total_pixels; i++) {
        int inverted = 255 - canvas[i];  /* 0=white, 255=black */
        int val = inverted * 15 / 255;   /* quantize to 0-15 */
        if (val > 15) val = 15;
        gray4[i] = (uint8_t)val;
    }

    /* Pack 4bpp: two nibbles per byte */
    int bpr = (screen_w * 4 + 7) / 8;   /* bytes per row (ceil(w/2)) */
    uint8_t *packed = malloc(bpr * screen_h);
    memset(packed, 0, bpr * screen_h);
    for (int row = 0; row < screen_h; row++) {
        for (int col = 0; col < screen_w; col++) {
            uint8_t g = gray4[row * screen_w + col] & 0x0F;
            int byte_idx = row * bpr + col / 2;
            if (col % 2 == 0)
                packed[byte_idx] |= g << 4;
            else
                packed[byte_idx] |= g;
        }
    }

    it8951_display_4bpp(dev, packed, 0, 0, screen_w, screen_h, mode);

    /* Save current as last image for preview endpoint. */
    stbi_write_png(LAST_IMG_PATH, screen_w, screen_h, 1, canvas, screen_w);

    free(packed);
    free(gray4);
    free(canvas);
    free(resized);
    stbi_image_free(img);

    return 0;
}

/*
 * Full-screen 1-bit direct update (DU mode).
 * Loads image, scales to screen, thresholds to pure black/white, and sends
 * the entire frame with DU_MODE (mode 1). This bypasses GC16 grayscale
 * waveform spreading that causes 1px lines to appear as 2px on the panel.
 * No flash, no regional diff, no ghosting accumulation. The whole screen is
 * refreshed in one DU update.
 */
int it8951_display_image_1bit_fullscreen(it8951_t *dev, const char *path,
                                         uint8_t bg_color, float brightness)
{
    int img_w, img_h, img_ch;
    unsigned char *img = stbi_load(path, &img_w, &img_h, &img_ch, 1);
    if (!img) {
        fprintf(stderr, "Failed to load image: %s\n", path);
        return -1;
    }

    printf("Original size: %d x %d (1-bit DU fullscreen)\n", img_w, img_h);

    int screen_w = dev->info.panel_w;
    int screen_h = dev->info.panel_h;

    /* Auto-scale to fit screen, preserving aspect ratio */
    float scale = fminf((float)screen_w / img_w, (float)screen_h / img_h);
    int new_w, new_h;
    if (scale < 1.0) {
        new_w = (int)(img_w * scale);
        new_h = (int)(img_h * scale);
    } else {
        new_w = img_w;
        new_h = img_h;
    }

    /* Resize image */
    uint8_t *resized = malloc(new_w * new_h);
    if (new_w != img_w || new_h != img_h) {
        resize_bilinear_gray(img, img_w, img_h, resized, new_w, new_h);
    } else {
        memcpy(resized, img, new_w * new_h);
    }

    /* Create canvas with background color */
    uint8_t *canvas = malloc(screen_w * screen_h);
    memset(canvas, bg_color, screen_w * screen_h);

    int off_x = (screen_w - new_w) / 2;
    int off_y = (screen_h - new_h) / 2;

    /* Gamma LUT */
    uint8_t gamma_lut[256];
    if (brightness != 1.0f) {
        float inv_gamma = 1.0f / brightness;
        for (int i = 0; i < 256; i++) {
            float norm = (float)i / 255.0f;
            float corrected = powf(norm, inv_gamma);
            int v = (int)(corrected * 255.0f + 0.5f);
            gamma_lut[i] = (v < 0) ? 0 : (v > 255 ? 255 : (uint8_t)v);
        }
    } else {
        for (int i = 0; i < 256; i++) gamma_lut[i] = (uint8_t)i;
    }

    /* Paste, apply gamma, and immediately threshold to 1-bit (0 or 255) */
    for (int y = 0; y < new_h; y++) {
        int py = off_y + y;
        if (py < 0 || py >= screen_h) continue;
        for (int x = 0; x < new_w; x++) {
            int px = off_x + x;
            if (px < 0 || px >= screen_w) continue;
            uint8_t val = resized[y * new_w + x];
            if (abs(val - bg_color) > 2) {
                val = gamma_lut[val];
            }
            /* Threshold at 128: anything darker than mid-gray = black (0),
               anything lighter = white (255). */
            canvas[py * screen_w + px] = (val < 128) ? 0 : 255;
        }
    }

    /* Send full screen as 8bpp 1-bit data with DU mode */
    it8951_display_8bpp(dev, canvas, 0, 0, screen_w, screen_h, DU_MODE);

    /* Save current as last image for preview endpoint */
    stbi_write_png(LAST_IMG_PATH, screen_w, screen_h, 1, canvas, screen_w);

    free(canvas);
    free(resized);
    stbi_image_free(img);

    return 0;
}