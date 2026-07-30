/*
 * it8951_driver.c — IT8951 e-paper display driver for Orange Pi Zero 2W
 *
 * Port of the Python driver (github.com/sneakyjoeru/it8951-epaper) to C.
 * Uses libgpiod for GPIO control and raw SPI ioctl for communication.
 *
 * Build: make
 * Deps: libgpiod-dev, libfreetype-dev, stb_image.h (vendored)
 */
#include "it8951_driver.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <linux/spi/spidev.h>
#include <linux/types.h>
#include <gpiod.h>
#include <math.h>

/* ---- SPI ioctl helper ---- */

static int spi_multi_transfer(int fd, const uint8_t **tx_bufs, uint8_t **rx_bufs,
                              int *lens, int n, uint32_t speed)
{
    struct spi_ioc_transfer *xfers = calloc(n, sizeof(struct spi_ioc_transfer));
    if (!xfers) return -1;

    /* Keep references to rx buffers so they survive the ioctl */
    uint8_t **rx_alloc = calloc(n, sizeof(uint8_t *));

    for (int i = 0; i < n; i++) {
        rx_alloc[i] = malloc(lens[i]);
        memset(rx_alloc[i], 0, lens[i]);

        xfers[i].tx_buf = (unsigned long)(tx_bufs[i] ? tx_bufs[i] : rx_alloc[i]);
        xfers[i].rx_buf = (unsigned long)rx_alloc[i];
        xfers[i].len = lens[i];
        xfers[i].speed_hz = speed;
        xfers[i].bits_per_word = 8;
        xfers[i].cs_change = 0;
        xfers[i].delay_usecs = 0;
    }

    /* Calculate SPI_IOC_MESSAGE(n) */
    /* _IOW(type, nr, size) = (1<<30) | (size<<16) | (type<<8) | nr */
    /* type='k' (SPI_IOC_MAGIC), nr=0, size = sizeof(spi_ioc_transfer)*n */
    uint32_t msg = (1u << 30) | ((uint32_t)(sizeof(struct spi_ioc_transfer) * n) << 16)
                   | ((uint32_t)'k' << 8) | 0;
    int ret = ioctl(fd, msg, xfers);

    /* Copy rx data if requested */
    if (rx_bufs) {
        for (int i = 0; i < n; i++) {
            if (rx_bufs[i])
                memcpy(rx_bufs[i], rx_alloc[i], lens[i]);
        }
    }

    for (int i = 0; i < n; i++)
        free(rx_alloc[i]);
    free(rx_alloc);
    free(xfers);

    return ret;
}

/* ---- GPIO helpers ---- */

static int wait_busy(it8951_t *dev, int timeout_ms)
{
    for (int i = 0; i < timeout_ms; i++) {
        if (gpiod_line_get_value(dev->busy) == 1)
            return 0;
        usleep(1000);
    }
    return -1;
}

static void cs_low(it8951_t *dev)  { gpiod_line_set_value(dev->cs, 0); }
static void cs_high(it8951_t *dev) { gpiod_line_set_value(dev->cs, 1); }

/* ---- IT8951 protocol ---- */

static void write_cmd(it8951_t *dev, uint16_t cmd)
{
    wait_busy(dev, 5000);
    cs_low(dev);

    const uint8_t *txs[2];
    int lens[2];
    uint8_t preamble[] = {0x60, 0x00};
    uint8_t cmdbuf[] = {(cmd >> 8) & 0xFF, cmd & 0xFF};
    txs[0] = preamble; lens[0] = 2;
    txs[1] = cmdbuf;   lens[1] = 2;
    spi_multi_transfer(dev->spi_fd, txs, NULL, lens, 2, dev->speed);

    cs_high(dev);
}

static void write_data(it8951_t *dev, uint16_t data)
{
    wait_busy(dev, 5000);
    cs_low(dev);

    const uint8_t *txs[2];
    int lens[2];
    uint8_t preamble[] = {0x00, 0x00};
    uint8_t databuf[] = {(data >> 8) & 0xFF, data & 0xFF};
    txs[0] = preamble; lens[0] = 2;
    txs[1] = databuf;  lens[1] = 2;
    spi_multi_transfer(dev->spi_fd, txs, NULL, lens, 2, dev->speed);

    cs_high(dev);
}

static void write_data_bytes(it8951_t *dev, const uint8_t *data, int len)
{
    wait_busy(dev, 5000);
    cs_low(dev);

    /* Send preamble + data in one ioctl call, no rx needed */
    /* For large data, do preamble separately then data in chunks to avoid
       allocating huge rx buffers in spi_multi_transfer */
    struct spi_ioc_transfer xfers[2];
    memset(xfers, 0, sizeof(xfers));

    uint8_t preamble[] = {0x00, 0x00};
    xfers[0].tx_buf = (unsigned long)preamble;
    xfers[0].rx_buf = 0;  /* no rx */
    xfers[0].len = 2;
    xfers[0].speed_hz = dev->speed;
    xfers[0].bits_per_word = 8;
    xfers[0].cs_change = 0;

    /* Send data in 4096-byte chunks via separate ioctls to avoid huge allocations */
    /* First chunk goes with preamble */
    int chunk = 4096;
    if (len <= chunk) {
        /* Small data: send preamble + data in one ioctl */
        xfers[1].tx_buf = (unsigned long)data;
        xfers[1].rx_buf = 0;
        xfers[1].len = len;
        xfers[1].speed_hz = dev->speed;
        xfers[1].bits_per_word = 8;
        xfers[1].cs_change = 0;

        uint32_t msg = (1u << 30) | ((uint32_t)(sizeof(struct spi_ioc_transfer) * 2) << 16)
                       | ((uint32_t)'k' << 8) | 0;
        ioctl(dev->spi_fd, msg, xfers);
    } else {
        /* Large data: send preamble first, then data in chunks */
        uint32_t msg1 = (1u << 30) | ((uint32_t)(sizeof(struct spi_ioc_transfer) * 1) << 16)
                        | ((uint32_t)'k' << 8) | 0;
        ioctl(dev->spi_fd, msg1, &xfers[0]);  /* preamble only */

        struct spi_ioc_transfer data_xfer;
        memset(&data_xfer, 0, sizeof(data_xfer));
        data_xfer.tx_buf = (unsigned long)data;
        data_xfer.rx_buf = 0;
        data_xfer.speed_hz = dev->speed;
        data_xfer.bits_per_word = 8;
        data_xfer.cs_change = 0;

        for (int off = 0; off < len; off += chunk) {
            data_xfer.tx_buf = (unsigned long)(data + off);
            data_xfer.len = (off + chunk <= len) ? chunk : (len - off);
            uint32_t msg = (1u << 30) | ((uint32_t)(sizeof(struct spi_ioc_transfer)) << 16)
                           | ((uint32_t)'k' << 8) | 0;
            ioctl(dev->spi_fd, msg, &data_xfer);
        }
    }

    cs_high(dev);
}

static uint16_t read_data(it8951_t *dev)
{
    wait_busy(dev, 5000);
    cs_low(dev);

    const uint8_t *txs[3];
    int lens[3];
    uint8_t rxs[3][2];
    uint8_t preamble[] = {0x10, 0x00};
    uint8_t dummy[]    = {0x00, 0x00};
    uint8_t readbuf[]  = {0x00, 0x00};
    txs[0] = preamble; lens[0] = 2;
    txs[1] = dummy;    lens[1] = 2;
    txs[2] = readbuf;  lens[2] = 2;

    uint8_t *rx_ptrs[3] = {rxs[0], rxs[1], rxs[2]};
    spi_multi_transfer(dev->spi_fd, txs, rx_ptrs, lens, 3, dev->speed);

    cs_high(dev);
    return (rxs[2][0] << 8) | rxs[2][1];
}

static void read_multi(it8951_t *dev, uint16_t *words, int count)
{
    wait_busy(dev, 5000);
    cs_low(dev);

    /* Build transfers: preamble + dummy + count reads */
    int n = 2 + count;
    const uint8_t **txs = calloc(n, sizeof(uint8_t *));
    int *lens = calloc(n, sizeof(int));
    uint8_t **rx_ptrs = calloc(n, sizeof(uint8_t *));

    static uint8_t preamble[] = {0x10, 0x00};
    static uint8_t dummy[]    = {0x00, 0x00};

    txs[0] = preamble; lens[0] = 2; rx_ptrs[0] = malloc(2);
    txs[1] = dummy;    lens[1] = 2; rx_ptrs[1] = malloc(2);

    for (int i = 0; i < count; i++) {
        txs[2 + i] = dummy;  /* reuse dummy as zero-fill tx */
        lens[2 + i] = 2;
        rx_ptrs[2 + i] = malloc(2);
    }

    spi_multi_transfer(dev->spi_fd, txs, rx_ptrs, lens, n, dev->speed);

    for (int i = 0; i < count; i++) {
        words[i] = (rx_ptrs[2 + i][0] << 8) | rx_ptrs[2 + i][1];
    }

    for (int i = 0; i < n; i++) free(rx_ptrs[i]);
    free(rx_ptrs);
    free(txs);
    free(lens);

    cs_high(dev);
}

/* ---- Register access ---- */

static void write_reg(it8951_t *dev, uint16_t addr, uint16_t val)
{
    write_cmd(dev, IT8951_TCON_REG_WR);
    write_data(dev, addr);
    write_data(dev, val);
}

static uint16_t read_reg(it8951_t *dev, uint16_t addr)
{
    write_cmd(dev, IT8951_TCON_REG_RD);
    write_data(dev, addr);
    return read_data(dev);
}

static void set_target_mem_addr(it8951_t *dev, uint32_t addr)
{
    write_reg(dev, LISAR + 2, (addr >> 16) & 0xFFFF);
    write_reg(dev, LISAR, addr & 0xFFFF);
}

static int wait_display_ready(it8951_t *dev)
{
    for (int i = 0; i < 30000; i++) {
        if (read_reg(dev, LUTAFSR) == 0)
            return 0;
        usleep(1000);
    }
    return -1;
}

static void load_img_area_start(it8951_t *dev, uint16_t pixel_format,
                                uint16_t x, uint16_t y, uint16_t w, uint16_t h)
{
    uint16_t args = (IT8951_LDIMG_L_ENDIAN << 8) | (pixel_format << 4) | IT8951_ROTATE_0;
    write_cmd(dev, IT8951_TCON_LD_IMG_AREA);
    write_data(dev, args);
    write_data(dev, x);
    write_data(dev, y);
    write_data(dev, w);
    write_data(dev, h);
}

static void load_img_end(it8951_t *dev)
{
    write_cmd(dev, IT8951_TCON_LD_IMG_END);
}

static void display_area(it8951_t *dev, uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t mode)
{
    write_cmd(dev, USDEF_I80_CMD_DPY_AREA);
    write_data(dev, x);
    write_data(dev, y);
    write_data(dev, w);
    write_data(dev, h);
    write_data(dev, mode);
}

/* ---- Public API ---- */

int it8951_init(it8951_t *dev, int spi_bus, int spi_cs, uint32_t speed)
{
    memset(dev, 0, sizeof(*dev));
    dev->speed = speed;

    /* Open GPIO chip */
    dev->chip = gpiod_chip_open("/dev/gpiochip0");
    if (!dev->chip) {
        perror("Failed to open gpiochip0");
        return -1;
    }

    dev->rst  = gpiod_chip_get_line(dev->chip, RST_PIN);
    dev->cs   = gpiod_chip_get_line(dev->chip, CS_PIN);
    dev->busy = gpiod_chip_get_line(dev->chip, BUSY_PIN);

    if (!dev->rst || !dev->cs || !dev->busy) {
        fprintf(stderr, "Failed to get GPIO lines\n");
        return -1;
    }

    if (gpiod_line_request_output(dev->rst, "epd", 1) < 0 ||
        gpiod_line_request_output(dev->cs, "epd", 1) < 0 ||
        gpiod_line_request_input(dev->busy, "epd") < 0) {
        fprintf(stderr, "Failed to request GPIO lines\n");
        return -1;
    }

    /* Open SPI */
    char spi_path[32];
    snprintf(spi_path, sizeof(spi_path), "/dev/spidev%d.%d", spi_bus, spi_cs);
    dev->spi_fd = open(spi_path, O_RDWR);
    if (dev->spi_fd < 0) {
        perror("Failed to open SPI device");
        return -1;
    }

    uint8_t mode = 0; /* SPI_MODE_0 */
    ioctl(dev->spi_fd, SPI_IOC_WR_MODE, &mode);
    ioctl(dev->spi_fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed);

    /* Reset sequence */
    gpiod_line_set_value(dev->rst, 1); usleep(200000);
    gpiod_line_set_value(dev->rst, 0); usleep(10000);
    gpiod_line_set_value(dev->rst, 1); usleep(200000);

    if (wait_busy(dev, 5000) < 0) {
        fprintf(stderr, "IT8951 busy not released after reset\n");
        return -1;
    }

    /* SYS_RUN */
    write_cmd(dev, IT8951_TCON_SYS_RUN);
    usleep(100000);

    /* GET_DEV_INFO */
    write_cmd(dev, USDEF_I80_CMD_GET_DEV_INFO);
    usleep(50000);
    wait_busy(dev, 5000);

    uint16_t words[20];
    read_multi(dev, words, 20);

    dev->info.panel_w = words[0];
    dev->info.panel_h = words[1];
    dev->info.mem_addr = words[2] | (words[3] << 16);

    /* Parse FW version (words[4..11], each word = 2 bytes big-endian) */
    int pos = 0;
    for (int j = 0; j < 8 && pos < 19; j++) {
        uint8_t hi = (words[4 + j] >> 8) & 0xFF;
        uint8_t lo = words[4 + j] & 0xFF;
        if (hi >= 0x20 && hi < 127) dev->info.fw_version[pos++] = hi;
        if (lo >= 0x20 && lo < 127) dev->info.fw_version[pos++] = lo;
    }
    dev->info.fw_version[pos] = 0;

    /* Parse LUT version (words[12..19]) */
    pos = 0;
    for (int j = 0; j < 8 && pos < 19; j++) {
        uint8_t hi = (words[12 + j] >> 8) & 0xFF;
        uint8_t lo = words[12 + j] & 0xFF;
        if (hi >= 0x20 && hi < 127) dev->info.lut_version[pos++] = hi;
        if (lo >= 0x20 && lo < 127) dev->info.lut_version[pos++] = lo;
    }
    dev->info.lut_version[pos] = 0;

    dev->info.a2_mode = (strstr(dev->info.lut_version, "M641")) ? 4 : 6;

    printf("Panel:   %d x %d\n", dev->info.panel_w, dev->info.panel_h);
    printf("Memory:  0x%08X\n", dev->info.mem_addr);
    printf("FW:      %s\n", dev->info.fw_version);
    printf("LUT:     %s\n", dev->info.lut_version);
    printf("A2 mode: %d\n", dev->info.a2_mode);

    return 0;
}

void it8951_close(it8951_t *dev)
{
    if (dev->spi_fd >= 0) close(dev->spi_fd);
    gpiod_line_set_value(dev->rst, 0);
    if (dev->rst)  gpiod_line_release(dev->rst);
    if (dev->cs)   gpiod_line_release(dev->cs);
    if (dev->busy) gpiod_line_release(dev->busy);
    if (dev->chip) gpiod_chip_close(dev->chip);
}

uint16_t it8951_get_vcom(it8951_t *dev)
{
    write_cmd(dev, USDEF_I80_CMD_VCOM);
    write_data(dev, 0x0000);
    return read_data(dev);
}

void it8951_set_vcom(it8951_t *dev, uint16_t vcom)
{
    write_cmd(dev, USDEF_I80_CMD_VCOM);
    write_data(dev, 0x0001);
    write_data(dev, vcom);
}

void it8951_sleep(it8951_t *dev)
{
    write_cmd(dev, IT8951_TCON_SLEEP);
    usleep(100000);
}

int it8951_clear(it8951_t *dev, uint16_t mode)
{
    uint16_t w = dev->info.panel_w;
    uint16_t h = dev->info.panel_h;

    /* 8bpp: send 0 (PIL black) → hardware shows white */
    int total = w * h;
    uint8_t *data = calloc(total, 1); /* all zeros = black → shows white */

    set_target_mem_addr(dev, dev->info.mem_addr);
    load_img_area_start(dev, IT8951_8BPP, 0, 0, w, h);
    write_data_bytes(dev, data, total);
    load_img_end(dev);
    display_area(dev, 0, 0, w, h, mode);
    wait_display_ready(dev);

    free(data);
    return 0;
}

int it8951_display_8bpp(it8951_t *dev, const uint8_t *data,
                        uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t mode)
{
    set_target_mem_addr(dev, dev->info.mem_addr);
    load_img_area_start(dev, IT8951_8BPP, x, y, w, h);
    write_data_bytes(dev, data, w * h);
    load_img_end(dev);
    display_area(dev, x, y, w, h, mode);
    wait_display_ready(dev);
    return 0;
}

int it8951_display_4bpp(it8951_t *dev, const uint8_t *data,
                        uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t mode)
{
    int bpr = (w * 4 + 7) / 8;
    int total = bpr * h;

    /* Global color inversion: invert each 4bpp nibble (gray → 15-gray) */
    uint8_t *inverted = malloc(total);
    for (int i = 0; i < total; i++) {
        uint8_t hi = (data[i] >> 4) & 0x0F;
        uint8_t lo = data[i] & 0x0F;
        inverted[i] = ((15 - hi) << 4) | (15 - lo);
    }

    set_target_mem_addr(dev, dev->info.mem_addr);
    load_img_area_start(dev, IT8951_4BPP, x, y, w, h);
    write_data_bytes(dev, inverted, total);
    load_img_end(dev);
    display_area(dev, x, y, w, h, mode);
    wait_display_ready(dev);

    free(inverted);
    return 0;
}

/* ---- A2 fast clear (overlapped with data load) — ported from Python ---- */

static void a2_fast_clear(it8951_t *dev)
{
    /* Fast ghosting clear using A2 mode — single black flash.
       Loads 1bpp all-black and triggers A2 refresh. ~1.1s.
       Overlaps with data loading — caller loads image data while A2 refreshes.
       Does NOT wait for A2 to finish — caller must call wait_display_ready(). */
    uint16_t w = dev->info.panel_w;
    uint16_t h = dev->info.panel_h;
    int bpr = w / 8;
    int total = bpr * h;
    if (total % 2) total++;

    uint16_t val = read_reg(dev, UP1SR + 2);
    write_reg(dev, UP1SR + 2, val | (1u << 2));
    write_reg(dev, BGVR, (0x00 << 8) | 0xFF);

    uint8_t *black = malloc(total);
    memset(black, 0xFF, total);

    set_target_mem_addr(dev, dev->info.mem_addr);
    load_img_area_start(dev, IT8951_8BPP, 0, 0, w / 8, h);
    write_data_bytes(dev, black, total);
    load_img_end(dev);
    display_area(dev, 0, 0, w, h, dev->info.a2_mode);
    /* DON'T wait — let caller load data during refresh */

    free(black);
}

static void a2_disable_1bpp(it8951_t *dev)
{
    uint16_t val = read_reg(dev, UP1SR + 2);
    write_reg(dev, UP1SR + 2, val & ~(1u << 2));
}

int it8951_clear_then_display_4bpp(it8951_t *dev, const uint8_t *data,
                                   uint16_t w, uint16_t h, uint16_t mode)
{
    /* Fast clear + 4bpp render: A2 black flash + 4bpp load overlapped + GC16.
       ~5.2s total (A2 clear + data load happen simultaneously).
       Ported from Python clear_then_display_4bpp. */
    int bpr = (w * 4 + 7) / 8;
    int total = bpr * h;

    /* Global color inversion: invert each 4bpp nibble */
    uint8_t *inverted = malloc(total);
    for (int i = 0; i < total; i++) {
        uint8_t hi = (data[i] >> 4) & 0x0F;
        uint8_t lo = data[i] & 0x0F;
        inverted[i] = ((15 - hi) << 4) | (15 - lo);
    }

    /* 1. A2 black flash (triggers refresh, doesn't wait) */
    a2_fast_clear(dev);

    /* 2. Load 4bpp data WHILE A2 refreshes (SPI is free during LUT refresh) */
    set_target_mem_addr(dev, dev->info.mem_addr);
    load_img_area_start(dev, IT8951_4BPP, 0, 0, w, h);
    write_data_bytes(dev, inverted, total);
    load_img_end(dev);

    /* 3. Wait for A2 to finish */
    wait_display_ready(dev);
    a2_disable_1bpp(dev);

    /* 4. GC16 render (data already loaded, ~0.5s) */
    display_area(dev, 0, 0, w, h, mode);
    wait_display_ready(dev);

    free(inverted);
    return 0;
}