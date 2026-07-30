/*
 * main.c — CLI entry point for IT8951 e-paper display driver (C version)
 *
 * Usage:
 *   sudo ./it8951 --info                    Show device info + VCOM
 *   sudo ./it8951 --clear                   Clear screen to white
 *   sudo ./it8951 --text "Hello, World!"    Display text
 *   sudo ./it8951 --text "Hello" --font-size 96
 *   sudo ./it8951 --image photo.jpg         Display image (auto-scaled)
 *   sudo ./it8951 --gradient                Vertical gradient (dithered)
 *   sudo ./it8951 --checker 50              Checkerboard pattern
 *   sudo ./it8951 --cross 9                 Gradient cross (9px lines)
 *   sudo ./it8951 --quarter                 Top-left quarter black
 *   sudo ./it8951 --setup                   Configure OS overlay + packages
 *   sudo ./it8951 --server                  Start HTTP API server (port 8888)
 */
#include "it8951_driver.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <getopt.h>
#include <math.h>
#include <time.h>
#include <sys/types.h>
#include <sys/stat.h>

/* ---- Pattern generators ---- */

static int do_gradient(it8951_t *dev)
{
    int w = dev->info.panel_w;
    int h = dev->info.panel_h;
    int bpr = (w * 4 + 7) / 8;

    /* 4bpp: 0=white, 15=black. Random dithering with fractional levels. */
    float *gray = malloc(h * w * sizeof(float));
    srand(time(NULL));

    for (int row = 0; row < h; row++) {
        float cont = (float)row / (h - 1) * 15.0;
        for (int col = 0; col < w; col++)
            gray[row * w + col] = cont;
    }

    uint8_t *gray4 = malloc(h * w);
    for (int row = 0; row < h; row++) {
        for (int col = 0; col < w; col++) {
            float g = gray[row * w + col];
            int base = (int)g;
            float frac = g - base;
            uint8_t val = ((float)rand() / RAND_MAX < frac) ? base + 1 : base;
            if (val > 15) val = 15;
            gray4[row * w + col] = val;
        }
    }

    /* Pack 4bpp */
    uint8_t *packed = calloc(bpr * h, 1);
    for (int row = 0; row < h; row++) {
        for (int col = 0; col < w; col++) {
            uint8_t g = gray4[row * w + col] & 0x0F;
            int off = row * bpr + col / 2;
            if (col % 2 == 0)
                packed[off] |= (g << 4);
            else
                packed[off] |= g;
        }
    }

    printf("Displaying vertical gradient (dithered)...\n");
    it8951_display_4bpp(dev, packed, 0, 0, w, h, GC16_MODE);
    printf("Done.\n");

    free(packed);
    free(gray4);
    free(gray);
    return 0;
}

static int do_checker(it8951_t *dev, int cell)
{
    int w = dev->info.panel_w;
    int h = dev->info.panel_h;

    /* 8bpp: 0=black, 255=white */
    uint8_t *img = malloc(w * h);
    for (int row = 0; row < h; row++) {
        int cy = row / cell;
        for (int col = 0; col < w; col++) {
            int cx = col / cell;
            img[row * w + col] = ((cx + cy) % 2 == 0) ? 0 : 255;
        }
    }

    printf("Displaying checkerboard (%dpx cells)...\n", cell);
    it8951_display_8bpp(dev, img, 0, 0, w, h, GC16_MODE);
    printf("Done.\n");

    free(img);
    return 0;
}

static int do_cross(it8951_t *dev, int line_width, int invert, int vertical)
{
    int w = dev->info.panel_w;
    int h = dev->info.panel_h;
    int cx = w / 2, cy = h / 2;

    /* 4bpp: 0=white, 15=black */
    float *gray = malloc(h * w * sizeof(float));
    for (int i = 0; i < h * w; i++)
        gray[i] = invert ? 15.0 : 0.0;

    /* Draw 4 diagonal lines */
    int endpoints[4][4] = {
        {0, 0, cx, cy},
        {w-1, 0, cx, cy},
        {0, h-1, cx, cy},
        {w-1, h-1, cx, cy},
    };

    for (int l = 0; l < 4; l++) {
        int x0 = endpoints[l][0], y0 = endpoints[l][1];
        int x1 = endpoints[l][2], y1 = endpoints[l][3];
        float dx = x1 - x0, dy = y1 - y0;
        float length = sqrt(dx*dx + dy*dy);
        if (length == 0) continue;
        float ux = dx/length, uy = dy/length;
        float px_val = -uy, py_val = ux;
        int steps = (int)length;

        for (int i = 0; i <= steps; i++) {
            float cx_pt = x0 + ux * i;
            float cy_pt = y0 + uy * i;
            float t;
            if (vertical)
                t = cy_pt / (h - 1);
            else
                t = (float)i / steps;
            float line_gray = invert ? (15 - t * 15) : (t * 15);
            int half_w = line_width / 2;
            for (int j = -half_w; j <= half_w; j++) {
                int px = (int)(cx_pt + px_val * j + 0.5);
                int py = (int)(cy_pt + py_val * j + 0.5);
                if (px >= 0 && px < w && py >= 0 && py < h)
                    gray[py * w + px] = line_gray;
            }
        }
    }

    /* Random dithering */
    srand(time(NULL));
    uint8_t *gray4 = malloc(h * w);
    for (int row = 0; row < h; row++) {
        for (int col = 0; col < w; col++) {
            float g = gray[row * w + col];
            int base = (int)g;
            float frac = g - base;
            uint8_t val = ((float)rand() / RAND_MAX < frac) ? base + 1 : base;
            if (val > 15) val = 15;
            gray4[row * w + col] = val;
        }
    }

    /* Pack 4bpp */
    int bpr = (w * 4 + 7) / 8;
    uint8_t *packed = calloc(bpr * h, 1);
    for (int row = 0; row < h; row++) {
        for (int col = 0; col < w; col++) {
            uint8_t g = gray4[row * w + col] & 0x0F;
            int off = row * bpr + col / 2;
            if (col % 2 == 0)
                packed[off] |= (g << 4);
            else
                packed[off] |= g;
        }
    }

    printf("Displaying cross (%dpx, vertical=%d, invert=%d)...\n", line_width, vertical, invert);
    it8951_display_4bpp(dev, packed, 0, 0, w, h, GC16_MODE);
    printf("Done.\n");

    free(packed);
    free(gray4);
    free(gray);
    return 0;
}

static int do_quarter(it8951_t *dev)
{
    int w = dev->info.panel_w;
    int h = dev->info.panel_h;
    int hw = w / 2, hh = h / 2;

    /* 8bpp: 255=white bg, 0=black quarter */
    uint8_t *img = malloc(w * h);
    memset(img, 255, w * h);
    for (int row = 0; row < hh; row++)
        for (int col = 0; col < hw; col++)
            img[row * w + col] = 0;

    printf("Displaying quarter black...\n");
    it8951_display_8bpp(dev, img, 0, 0, w, h, GC16_MODE);
    printf("Done.\n");

    free(img);
    return 0;
}

/* ---- OS setup ---- */

static int do_setup()
{
    printf("=== IT8951 E-Paper Display OS Setup ===\n\n");

    /* 1. Install packages */
    printf("1. Installing packages...\n");
    system("sudo apt update && sudo apt install -y "
           "python3-pil python3-libgpiod python3-spidev python3-numpy "
           "libgpiod-dev libfreetype-dev gcc make");

    /* 2. Configure boot overlay */
    printf("\n2. Configuring boot overlay...\n");
    FILE *f = fopen("/boot/orangepiEnv.txt", "r");
    if (!f) {
        fprintf(stderr, "Cannot read /boot/orangepiEnv.txt\n");
        return -1;
    }

    char buf[1024];
    char content[4096] = "";
    int has_overlay = 0;
    while (fgets(buf, sizeof(buf), f)) {
        if (strncmp(buf, "overlays=", 9) == 0) {
            if (strstr(buf, "spi1-cs1-spidev")) {
                has_overlay = 1;
                strcat(content, buf);
            } else {
                strcat(content, "overlays=spi1-cs1-spidev\n");
                printf("   Changed overlay to: spi1-cs1-spidev\n");
            }
        } else {
            strcat(content, buf);
        }
    }
    fclose(f);

    if (!has_overlay) {
        f = fopen("/boot/orangepiEnv.txt", "w");
        if (f) {
            fputs(content, f);
            fclose(f);
            printf("   Updated /boot/orangepiEnv.txt\n");
        }
    } else {
        printf("   Overlay already correct (spi1-cs1-spidev)\n");
    }

    /* 3. Check status */
    printf("\n3. Checking current status...\n");
    if (access("/dev/spidev1.1", F_OK) == 0) {
        printf("   /dev/spidev1.1 exists ✓\n");
    } else {
        printf("   /dev/spidev1.1 NOT found — REBOOT required!\n");
        printf("   Run: sudo reboot\n");
    }

    /* 4. Verify GPIO lines are free for manual control (RST=226, BUSY=228, CS=229).
       The Waveshare HAT needs GPIO 229 (physical SPI1 CS0) driven manually via
       libgpiod, so the kernel must NOT have claimed it. With the spi1-cs1-spidev
       overlay, only CS1 is enabled and GPIO 229 stays free. */
    printf("\n4. Verifying GPIO lines (RST=226, BUSY=228, CS=229)...\n");
    int gpio_ok = 1;
    int found_226 = 0, found_228 = 0, found_229 = 0;
    {
        /* /sys/kernel/debug/gpio is only readable by root — use popen with sudo. */
        FILE *gf = popen("sudo cat /sys/kernel/debug/gpio 2>/dev/null", "r");
        if (gf) {
            char gline[256];
            while (fgets(gline, sizeof(gline), gf)) {
                if (strstr(gline, "gpio-226")) found_226 = 1;
                if (strstr(gline, "gpio-228")) found_228 = 1;
                if (strstr(gline, "gpio-229")) found_229 = 1;
            }
            pclose(gf);
        }
    }

    printf("   GPIO 226 (RST):  %s\n",   found_226 ? "kernel-claimed ✗ (reboot may free it)" : "free ✓");
    printf("   GPIO 228 (BUSY): %s\n",   found_228 ? "kernel-claimed ✗ (reboot may free it)" : "free ✓");
    printf("   GPIO 229 (CS):   %s\n",   found_229 ? "kernel-claimed ✗ — overlay not applied, reboot needed" : "free ✓ (manual CS ready)");
    if (found_226 || found_228 || found_229) gpio_ok = 0;

    printf("\n=== Setup complete! ===\n");
    int need_reboot = (access("/dev/spidev1.1", F_OK) != 0) || !gpio_ok;
    if (need_reboot) {
        printf("⚠️  Reboot required. Run: sudo reboot\n");
        printf("   After reboot, run: sudo ./it8951 --info\n");
    } else {
        printf("✓  Ready! Run: sudo ./it8951 --info\n");
    }
    return 0;
}

/* ---- Usage ---- */

static void usage(const char *prog)
{
    printf("IT8951 E-Paper Display Driver (C version)\n\n");
    printf("Usage: sudo %s [OPTIONS]\n\n", prog);
    printf("Options:\n");
    printf("  --info                Show device info + VCOM\n");
    printf("  --clear               Clear screen to white\n");
    printf("  --text STR            Display text (antialiased)\n");
    printf("  --font-size N         Font size in pixels (default: 48)\n");
    printf("  --font-path PATH      Path to .ttf font file\n");
    printf("  --image PATH          Display image (auto-scaled, centered)\n");
    printf("  --brightness F        Image brightness multiplier (default: 1.4)\n");
    printf("  --gradient            Vertical gradient (random dithered)\n");
    printf("  --checker N           Checkerboard (N px cells, default: 50)\n");
    printf("  --cross N             Gradient cross (N px line width, default: 9)\n");
    printf("  --cross-invert        Invert cross colors (black bg)\n");
    printf("  --cross-vertical      Cross gradient: bottom-to-top\n");
    printf("  --quarter             Top-left quarter black\n");
    printf("  --setup               Configure OS (overlay + packages)\n");
    printf("  --server              Start HTTP API server (port 8888)\n");
    printf("  --port N              Server port (default: 8888)\n");
    printf("  --set-vcom N          Set VCOM (millivolts, e.g. 2510 = -2.51V)\n");
    printf("  --hard                Regional hard refresh (white flash inner + GL16)\n");
    printf("  --soft                Regional soft refresh (GL16 only, no blink)\n");
    printf("  --fullscreen          Full-screen GC16 clean refresh (removes ghosting)\n");
    printf("  --border-smooth N     Border expansion in px for partial refresh (default: 20)\n");
    printf("\n");
    printf("Regional update modes (--hard/--soft) compare the new image against\n");
    printf("the last displayed image, find changed regions, expand by border-smooth\n");
    printf("pixels, keep the old content in the border zone (no dithering), and send\n");
    printf("only the changed region to the display. --fullscreen does a full GC16\n");
    printf("clean refresh. Use with --image.\n");
    printf("\n");
}

/* ---- Main ---- */

int main(int argc, char *argv[])
{
    int opt;
    int do_info = 0, do_clear = 0, want_gradient = 0, do_quarter_flag = 0;
    int do_setup_flag = 0, do_server = 0;
    int checker = 0, cross = 0;
    int cross_invert = 0, cross_vertical = 0;
    int font_size = 48, port = 8888;
    int set_vcom = -1;
    float brightness = 1.4;
    int diff_mode = -1;  /* -1 = none, 0 = soft, 1 = hard */
    int border_smooth = 20;
    const char *text = NULL, *font_path = NULL, *image_path = NULL;

    static struct option long_opts[] = {
        {"info",           no_argument,       0, 'i'},
        {"clear",          no_argument,       0, 'c'},
        {"text",           required_argument, 0, 't'},
        {"font-size",      required_argument, 0, 'f'},
        {"font-path",      required_argument, 0, 'F'},
        {"image",          required_argument, 0, 'm'},
        {"brightness",     required_argument, 0, 'b'},
        {"gradient",       no_argument,       0, 'g'},
        {"checker",        optional_argument, 0, 'k'},
        {"cross",          optional_argument, 0, 'x'},
        {"cross-invert",   no_argument,       0, 'X'},
        {"cross-vertical", no_argument,       0, 'V'},
        {"quarter",        no_argument,       0, 'q'},
        {"setup",          no_argument,       0, 's'},
        {"server",         no_argument,       0, 'S'},
        {"port",           required_argument, 0, 'p'},
        {"set-vcom",       required_argument, 0, 'v'},
        {"hard",           no_argument,       0, 'H'},
        {"soft",           no_argument,       0, 'O'},
        {"fullscreen",     no_argument,       0, 'A'},
        {"border-smooth",  required_argument, 0, 'B'},
        {"help",           no_argument,       0, 'h'},
        {0, 0, 0, 0}
    };

    while ((opt = getopt_long(argc, argv, "ict:f:F:m:b:gk::x::XVqsSp:v:HO B:h",
                              long_opts, NULL)) != -1) {
        switch (opt) {
            case 'i': do_info = 1; break;
            case 'c': do_clear = 1; break;
            case 't': text = optarg; break;
            case 'f': font_size = atoi(optarg); break;
            case 'F': font_path = optarg; break;
            case 'm': image_path = optarg; break;
            case 'b': brightness = atof(optarg); break;
            case 'g': want_gradient = 1; break;
            case 'k': checker = optarg ? atoi(optarg) : 50; break;
            case 'x': cross = optarg ? atoi(optarg) : 9; break;
            case 'X': cross_invert = 1; break;
            case 'V': cross_vertical = 1; break;
            case 'q': do_quarter_flag = 1; break;
            case 's': do_setup_flag = 1; break;
            case 'S': do_server = 1; break;
            case 'p': port = atoi(optarg); break;
            case 'v': set_vcom = atoi(optarg); break;
            case 'H': diff_mode = DIFF_MODE_HARD; break;
            case 'O': diff_mode = DIFF_MODE_SOFT; break;
            case 'A': diff_mode = DIFF_MODE_FULLSCREEN; break;
            case 'B': border_smooth = atoi(optarg); break;
            case 'h': usage(argv[0]); return 0;
            default: usage(argv[0]); return 1;
        }
    }

    /* --setup doesn't need hardware */
    if (do_setup_flag) {
        return do_setup();
    }

    /* Initialize driver */
    it8951_t dev;
    if (it8951_init(&dev, 1, 1, 12000000) < 0) {
        fprintf(stderr, "Failed to initialize IT8951\n");
        return 1;
    }

    int has_action = do_clear || text || image_path || want_gradient ||
                     checker || cross || do_quarter_flag || do_server;

    if (do_info || !has_action) {
        uint16_t vcom = it8951_get_vcom(&dev);
        printf("VCOM:    %d (=-%.2fV)\n", vcom, vcom / 1000.0);
        if (!has_action) {
            printf("\nNo action specified. Use --help for options.\n");
        }
    }

    if (set_vcom >= 0) {
        it8951_set_vcom(&dev, (uint16_t)set_vcom);
        printf("VCOM set to %d\n", set_vcom);
    }

    if (do_clear) {
        printf("Clearing...\n");
        it8951_clear(&dev, INIT_MODE);
        printf("Done.\n");
    }

    if (text) {
        printf("Displaying text: %.60s\n", text);
        it8951_display_text(&dev, text, font_size, font_path, 255, 0, GC16_MODE);
        printf("Done.\n");
    }

    if (image_path) {
        printf("Loading image: %s\n", image_path);
        if (diff_mode >= 0) {
            printf("%s refresh (border-smooth=%d)\n",
                   diff_mode == DIFF_MODE_HARD ? "hard"
                   : (diff_mode == DIFF_MODE_FULLSCREEN ? "fullscreen" : "soft"),
                   border_smooth);
            it8951_display_image_diff(&dev, image_path, 0, brightness,
                                      diff_mode, border_smooth);
        } else {
            it8951_display_image(&dev, image_path, 0, brightness, GC16_MODE);
        }
        printf("Displayed.\n");
    }

    if (want_gradient)
        do_gradient(&dev);

    if (checker)
        do_checker(&dev, checker);

    if (cross)
        do_cross(&dev, cross, cross_invert, cross_vertical);

    if (do_quarter_flag)
        do_quarter(&dev);

    if (do_server) {
        printf("HTTP server not yet implemented in C version.\n");
        printf("Use the Python version for the API server.\n");
    }

    it8951_close(&dev);
    return 0;
}