/*
 * it8951_driver.h — IT8951 e-paper display driver for Orange Pi Zero 2W
 *
 * Based on the Waveshare IT8951-ePaper C code (GPIOD mode) and the
 * Python driver at github.com/sneakyjoeru/it8951-epaper.
 *
 * Uses raw SPI ioctl for atomic multi-transfers with manual CS control.
 * The Waveshare HAT's CS is on GPIO 229 (SPI1 CS0), manually driven.
 * SPI device: /dev/spidev1.1 (kernel CS1, unused).
 *
 * Color conventions:
 *   8bpp: 0=black, 255=white (PIL convention, sent directly, hardware inverts)
 *   4bpp: 0=white, 15=black (nibble inversion applied in display_4bpp)
 */
#ifndef IT8951_DRIVER_H
#define IT8951_DRIVER_H

#include <stdint.h>
#include <stdbool.h>

/* ---- GPIO pins (Orange Pi Zero 2W, H616, port PH) ---- */
#define RST_PIN  226  /* PH2 */
#define CS_PIN   229  /* PH5 (physical SPI1 CS0 — manually driven) */
#define BUSY_PIN 228  /* PH4 */

/* ---- IT8951 I80 commands ---- */
#define IT8951_TCON_SYS_RUN       0x0001
#define IT8951_TCON_STANDBY       0x0002
#define IT8951_TCON_SLEEP         0x0003
#define IT8951_TCON_REG_RD        0x0010
#define IT8951_TCON_REG_WR        0x0011
#define IT8951_TCON_LD_IMG        0x0020
#define IT8951_TCON_LD_IMG_AREA   0x0021
#define IT8951_TCON_LD_IMG_END    0x0022

#define USDEF_I80_CMD_DPY_AREA       0x0034
#define USDEF_I80_CMD_GET_DEV_INFO   0x0302
#define USDEF_I80_CMD_DPY_BUF_AREA   0x0037
#define USDEF_I80_CMD_VCOM           0x0039

/* ---- Refresh modes ---- */
#define INIT_MODE 0
#define DU_MODE   1  /* Direct Update — 1-bit, no flash, no ghosting accumulation */
#define GLD16_MODE 1  /* alias of DU_MODE (same value) */
#define GC16_MODE 2
#define GL16_MODE 3   /* Gray Low 16 — less ghosting, good for text */
#define GLR16_MODE 4  /* Gray Low + Refresh */
#define A2_MODE   6  /* default for M841_TFA2812 (7.8") */

/* ---- Pixel formats ---- */
#define IT8951_2BPP 0
#define IT8951_3BPP 1
#define IT8951_4BPP 2
#define IT8951_8BPP 3

/* ---- Endian / Rotate ---- */
#define IT8951_LDIMG_L_ENDIAN 0
#define IT8951_LDIMG_B_ENDIAN 1
#define IT8951_ROTATE_0       0

/* ---- Registers ---- */
#define DISPLAY_REG_BASE 0x1000
#define UP1SR     (DISPLAY_REG_BASE + 0x138)
#define LUTAFSR   (DISPLAY_REG_BASE + 0x224)
#define BGVR      (DISPLAY_REG_BASE + 0x250)
#define MCSR_BASE 0x0200
#define LISAR     (MCSR_BASE + 0x0008)

/* ---- Device info structure ---- */
typedef struct {
    uint16_t panel_w;
    uint16_t panel_h;
    uint32_t mem_addr;
    char fw_version[20];
    char lut_version[20];
    uint8_t a2_mode;
} it8951_info_t;

/* ---- Driver context ---- */
typedef struct {
    int spi_fd;
    uint32_t speed;
    /* GPIO handles */
    struct gpiod_chip *chip;
    struct gpiod_line *rst;
    struct gpiod_line *cs;
    struct gpiod_line *busy;
    /* Device info */
    it8951_info_t info;
} it8951_t;

/* ---- API ---- */

int  it8951_init(it8951_t *dev, int spi_bus, int spi_cs, uint32_t speed);
void it8951_close(it8951_t *dev);

/* Display operations */
int it8951_clear(it8951_t *dev, uint16_t mode);
int it8951_display_8bpp(it8951_t *dev, const uint8_t *data,
                        uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t mode);
int it8951_display_4bpp(it8951_t *dev, const uint8_t *data,
                        uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t mode);

/* Fast clear + display: A2 black flash overlapped with 4bpp data load + GC16.
   ~5.2s total vs sequential ~7s. Ported from Python clear_then_display_4bpp. */
int it8951_clear_then_display_4bpp(it8951_t *dev, const uint8_t *data,
                                   uint16_t w, uint16_t h, uint16_t mode);

/* Text rendering (requires libfreetype) */
int it8951_display_text(it8951_t *dev, const char *text, int font_size,
                        const char *font_path, uint8_t bg_color, uint8_t fg_color,
                        uint16_t mode);

/* Image display (auto-scaled, centered; requires stb_image) */
int it8951_display_image(it8951_t *dev, const char *path,
                         uint8_t bg_color, float brightness, uint16_t mode);
int it8951_display_image_4bpp(it8951_t *dev, const char *path,
                              uint8_t bg_color, float brightness, uint16_t mode);
int it8951_display_image_1bit_fullscreen(it8951_t *dev, const char *path,
                                         uint8_t bg_color, float brightness);

/* VCOM */
uint16_t it8951_get_vcom(it8951_t *dev);
void it8951_set_vcom(it8951_t *dev, uint16_t vcom);

/* Sleep */
void it8951_sleep(it8951_t *dev);

/* ---- Regional differential update (it8951_diff.c) ---- */

/* Refresh modes for diff update */
#define DIFF_MODE_SOFT       0   /* GL16 only, no blinking, no flash */
#define DIFF_MODE_HARD       1   /* White-flash inner region then GL16 */
#define DIFF_MODE_DU         5   /* DU 1-bit, no flash, no ghosting accumulation */
#define DIFF_MODE_FULLSCREEN 3   /* GC16 full-screen clean refresh */

/* Display with differential regional update.
   Compares new_data against last displayed image (stored at /tmp/it8951_last.png).
   Only sends changed region + border to display.
   mode: DIFF_MODE_SOFT, DIFF_MODE_HARD, DIFF_MODE_DU, or DIFF_MODE_FULLSCREEN
   border: pixels to expand the changed region (old content kept in the border
           zone; only the inner changed area is visually updated; default 20)
   threshold: pixel difference to detect change (default 5) */
int it8951_display_diff(it8951_t *dev, const uint8_t *new_data,
                        int w, int h, int mode, int border, int threshold);

/* Convenience: load image file, scale, apply brightness, diff update. */
int it8951_display_image_diff(it8951_t *dev, const char *path,
                              uint8_t bg_color, float brightness,
                              int mode, int border);

#endif /* IT8951_DRIVER_H */