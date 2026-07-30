/*
 * it8951_diff.c — Regional differential update for IT8951 e-paper displays.
 *
 * Compares a new image against the last displayed image (stored on disk),
 * finds changed regions, expands them by a configurable border, and sends
 * only the changed region to the display. The border zone keeps the OLD
 * content so the partial refresh only visibly updates the inner changed
 * area (no dithering).
 *
 * Refresh modes:
 *   --soft   Draw the changed region with GL16 (no pre-clear, no blink, no
 *            flash). The border preserves the old state, so soft updates
 *            "consider the old state before the partial refresh". Best for
 *            small changes (time-line movement).
 *   --hard   White-flash the inner changed region first (GL16), then draw
 *            the full expanded region (inner new + old border) with GL16.
 *            The flash is confined to the changed area; the border keeps the
 *            old content.
 *   --fullscreen  Full-screen GC16 clean refresh (removes ghosting). Used for
 *            day change / interval / manual full refresh, not regional updates.
 *
 * --border-smooth N   Expand the changed region by N pixels on each side
 *                      (default: 20). The border zone keeps the old pixels so
 *            only the inner changed area is visually updated. This is a
 *            partial-refresh area expansion, not dithering.
 *
 * Image storage: /tmp/it8951_last.png (PNG, 8bpp grayscale).
 */
#include "it8951_driver.h"

/* stb_image is already implemented in it8951_image.c — just include headers */
#include "stb_image.h"
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "stb_image_write.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <sys/stat.h>

#define DEFAULT_BORDER 20
#define LAST_IMG_PATH "/tmp/it8951_last.png"

/* ---- Border expansion (no dithering) ---- */

/*
 * Build the region buffer for a partial refresh.
 * Inner region (dist >= border): copy the NEW pixel value directly (sharp
 * updated content).
 * Border zone (dist < border): copy the OLD pixel value. This keeps the
 * previously-displayed content at the edges so the partial refresh only
 * visibly updates the actually-changed inner area. No dithering is applied —
 * the border simply preserves the old state, so soft updates "consider the
 * old state before the partial refresh".
 */
static void apply_border_blend(uint8_t *region_data,
                               const uint8_t *old_full,
                               const uint8_t *new_full,
                               int rx, int ry, int rw, int rh,
                               int screen_w, int screen_h,
                               int border)
{
    for (int y = 0; y < rh; y++) {
        for (int x = 0; x < rw; x++) {
            int gx = rx + x;
            int gy = ry + y;
            if (gx < 0 || gx >= screen_w || gy < 0 || gy >= screen_h) {
                region_data[y * rw + x] = 255;
                continue;
            }

            /* Distance from region edge (0 = outer edge, increases inward) */
            int dx = (x < rw - 1 - x) ? x : (rw - 1 - x);
            int dy = (y < rh - 1 - y) ? y : (rh - 1 - y);
            int dist = (dx < dy) ? dx : dy;

            if (dist >= border) {
                /* Inner region: new content */
                region_data[y * rw + x] = new_full[gy * screen_w + gx];
            } else {
                /* Border zone: keep old content (preserve prior state) */
                region_data[y * rw + x] = old_full[gy * screen_w + gx];
            }
        }
    }
}

/* ---- Find bounding box of changed region ---- */

static int find_changed_region(const uint8_t *old_img, const uint8_t *new_img,
                               int w, int h, int threshold,
                               int *out_x, int *out_y, int *out_w, int *out_h)
{
    int min_x = w, min_y = h, max_x = -1, max_y = -1;
    int diff_count = 0;

    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            int diff = abs((int)old_img[y * w + x] - (int)new_img[y * w + x]);
            if (diff > threshold) {
                if (x < min_x) min_x = x;
                if (x > max_x) max_x = x;
                if (y < min_y) min_y = y;
                if (y > max_y) max_y = y;
                diff_count++;
            }
        }
    }

    if (max_x < 0) {
        printf("diff: no changes detected (threshold=%d)\n", threshold);
        return -1; /* No changes */
    }

    printf("diff: %d pixels differ (threshold=%d), region %d,%d %dx%d\n",
           diff_count, threshold, min_x, min_y, max_x - min_x + 1, max_y - min_y + 1);

    *out_x = min_x;
    *out_y = min_y;
    *out_w = max_x - min_x + 1;
    *out_h = max_y - min_y + 1;
    return 0;
}

/* ---- Main API: display with differential regional update ---- */

/*
 * it8951_display_diff — Compare new image to stored last image,
 * find changed region, expand by border, build the region (new inner + old
 * border, no dithering), and send to display.
 *
 * Parameters:
 *   dev         — IT8951 device handle
 *   new_data    — New full-screen 8bpp grayscale image (w*h bytes)
 *   w, h        — Image dimensions (should match panel size)
 *   mode        — DIFF_MODE_SOFT (GL16, no flash), DIFF_MODE_HARD (flash inner +
 *                GL16), DIFF_MODE_FULLSCREEN (GC16 full clean refresh)
 *   border      — Border expansion in pixels (old content kept in the border
 *                zone; only the inner changed area is visually updated)
 *   threshold   — Pixel difference threshold to detect changes
 *
 * Returns 0 on success, -1 on error or no changes.
 */
int it8951_display_diff(it8951_t *dev, const uint8_t *new_data,
                        int w, int h, int mode, int border, int threshold)
{
    int screen_w = dev->info.panel_w;
    int screen_h = dev->info.panel_h;

    if (w != screen_w || h != screen_h) {
        fprintf(stderr, "diff: image size %dx%d doesn't match panel %dx%d\n",
                w, h, screen_w, screen_h);
        return -1;
    }

    /* Load last image from disk (if exists) */
    uint8_t *old_data = NULL;
    int old_w, old_h, old_ch;
    old_data = stbi_load(LAST_IMG_PATH, &old_w, &old_h, &old_ch, 1);

    if (!old_data || old_w != screen_w || old_h != screen_h) {
        /* No previous image — full clean refresh with GC16 (full grayscale
           clear cycle, removes ghosting). GL16 would only update without a
           clean cycle, leaving ghosting. Save current as last for next diff. */
        if (old_data) stbi_image_free(old_data);
        printf("diff: no previous image, doing full GC16 clean refresh\n");
        it8951_display_8bpp(dev, new_data, 0, 0, screen_w, screen_h, GC16_MODE);
        /* Save current as last */
        stbi_write_png(LAST_IMG_PATH, screen_w, screen_h, 1, new_data, screen_w);
        return 0;
    }

    /* Fullscreen mode: ignore the diff and do a full-screen GC16 clean
       refresh regardless of what changed. Saves the cache for next time. */
    if (mode == DIFF_MODE_FULLSCREEN) {
        stbi_image_free(old_data);
        printf("diff: fullscreen GC16 clean refresh\n");
        it8951_display_8bpp(dev, new_data, 0, 0, screen_w, screen_h, GC16_MODE);
        stbi_write_png(LAST_IMG_PATH, screen_w, screen_h, 1, new_data, screen_w);
        return 0;
    }

    /* Find changed region */
    int cx, cy, cw, ch;
    if (find_changed_region(old_data, new_data, screen_w, screen_h, threshold,
                            &cx, &cy, &cw, &ch) < 0) {
        printf("diff: no changes detected\n");
        stbi_image_free(old_data);
        return 0; /* No changes — nothing to do */
    }

    /* Expand region by border, clamped to screen bounds */
    int rx = cx - border;
    int ry = cy - border;
    int rw = cw + 2 * border;
    int rh = ch + 2 * border;
    if (rx < 0) rx = 0;
    if (ry < 0) ry = 0;
    if (rx + rw > screen_w) rw = screen_w - rx;
    if (ry + rh > screen_h) rh = screen_h - ry;

    /* IT8951 8bpp requires even-width regions (data sent as 16-bit words).
       Align x to even and round width up to even. */
    if (rx % 2 != 0) { rx--; rw++; }
    if (rw % 2 != 0) { rw++; }
    if (rx + rw > screen_w) rw = screen_w - rx;  /* re-clamp */
    if (rw % 2 != 0) rw--;  /* must stay even */

    printf("diff: changed region %d,%d %dx%d → expanded %d,%d %dx%d (border=%d, even-aligned)\n",
           cx, cy, cw, ch, rx, ry, rw, rh, border);

    /* Build region data: new content in the inner changed area, old content
       kept in the border zone (no dithering). */
    uint8_t *region = malloc(rw * rh);
    if (!region) {
        stbi_image_free(old_data);
        return -1;
    }

    apply_border_blend(region, old_data, new_data,
                        rx, ry, rw, rh,
                        screen_w, screen_h, border);

    /* Send to display — mode-specific */
    if (mode == DIFF_MODE_HARD) {
        /* Hard refresh: white-flash the INNER changed region only (no border),
           then draw the full expanded region (inner new + old border) with
           GL16. The flash is confined to the changed area; the border keeps
           the old content so only the inner area visibly updates. */
        printf("diff: hard refresh (flash inner %d,%d %dx%d + GL16 border)\n",
               cx, cy, cw, ch);

        /* Clamp inner region to the expanded region bounds */
        int inner_x = cx, inner_y = cy, inner_w = cw, inner_h = ch;
        if (inner_x < rx) { inner_w += (inner_x - rx); inner_x = rx; }
        if (inner_y < ry) { inner_h += (inner_y - ry); inner_y = ry; }
        if (inner_x + inner_w > rx + rw) inner_w = rx + rw - inner_x;
        if (inner_y + inner_h > ry + rh) inner_h = ry + rh - inner_y;
        /* Even-align inner region */
        if (inner_x % 2 != 0) { inner_x--; inner_w++; }
        if (inner_w % 2 != 0) inner_w++;
        if (inner_x + inner_w > rx + rw) inner_w = (rx + rw - inner_x);
        if (inner_w % 2 != 0) inner_w--;
        if (inner_w < 2) inner_w = 2;

        /* White-flash inner changed region */
        uint8_t *white = malloc(inner_w * inner_h);
        memset(white, 255, inner_w * inner_h);
        it8951_display_8bpp(dev, white, inner_x, inner_y, inner_w, inner_h, GL16_MODE);
        free(white);

        /* Draw full expanded region (inner new + old border) with GL16 */
        it8951_display_8bpp(dev, region, rx, ry, rw, rh, GL16_MODE);
    } else {
        /* Soft refresh: GL16 mode, 16-level grayscale, NO blink, NO flash.
           The border keeps the old content, so only the inner changed area
           visibly updates. */
        printf("diff: soft refresh (GL16, no blink)\n");
        it8951_display_8bpp(dev, region, rx, ry, rw, rh, GL16_MODE);
    }

    /* Save current image as last */
    stbi_write_png(LAST_IMG_PATH, screen_w, screen_h, 1, new_data, screen_w);

    free(region);
    stbi_image_free(old_data);
    return 0;
}

/* ---- Convenience: display image file with diff ---- */

int it8951_display_image_diff(it8951_t *dev, const char *path,
                              uint8_t bg_color, float brightness,
                              int mode, int border)
{
    int img_w, img_h, img_ch;
    unsigned char *img = stbi_load(path, &img_w, &img_h, &img_ch, 1);
    if (!img) {
        fprintf(stderr, "Failed to load image: %s\n", path);
        return -1;
    }

    int screen_w = dev->info.panel_w;
    int screen_h = dev->info.panel_h;

    /* Auto-scale to fit screen */
    float scale = fminf((float)screen_w / img_w, (float)screen_h / img_h);
    int new_w, new_h;
    if (scale < 1.0) {
        new_w = (int)(img_w * scale);
        new_h = (int)(img_h * scale);
    } else {
        new_w = img_w;
        new_h = img_h;
    }

    /* Resize */
    uint8_t *resized = malloc(new_w * new_h);
    if (new_w != img_w || new_h != img_h) {
        /* Bilinear resize */
        for (int dy = 0; dy < new_h; dy++) {
            float sy = (float)dy * img_h / new_h;
            int sy0 = (int)sy;
            int sy1 = (sy0 + 1 < img_h) ? sy0 + 1 : sy0;
            float fy = sy - sy0;
            for (int dx = 0; dx < new_w; dx++) {
                float sx = (float)dx * img_w / new_w;
                int sx0 = (int)sx;
                int sx1 = (sx0 + 1 < img_w) ? sx0 + 1 : sx0;
                float fx = sx - sx0;
                float v = (1-fx)*(1-fy) * img[sy0*img_w+sx0] +
                          fx*(1-fy)     * img[sy0*img_w+sx1] +
                          (1-fx)*fy     * img[sy1*img_w+sx0] +
                          fx*fy         * img[sy1*img_w+sx1];
                resized[dy*new_w+dx] = (uint8_t)(v + 0.5);
            }
        }
    } else {
        memcpy(resized, img, new_w * new_h);
    }

    /* Create full-screen canvas */
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

    /* Differential update — threshold 50 skips small color changes (dim_past
       fill difference is ~16) but catches time-line movement (255) and
       outline changes (200). This keeps the diff region small for smooth
       minute updates even when the last full refresh had dim_past enabled. */
    int ret = it8951_display_diff(dev, canvas, screen_w, screen_h,
                                  mode, border, 50);

    free(canvas);
    free(resized);
    stbi_image_free(img);
    return ret;
}