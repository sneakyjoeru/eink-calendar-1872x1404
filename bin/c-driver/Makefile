# Makefile for IT8951 e-paper display driver (C version)
# Target: Orange Pi Zero 2W (aarch64, H616)
#
# Builds the diff-capable binary with regional update modes:
#   --hard   (white-flash inner + GL16 dithered border)
#   --soft   (GL16 regional, no flash, 16-level dither visible)
#   --smooth (A2 1-bit, no flash, fastest — no dithering)
#   --border-smooth N (Floyd-Steinberg old→new blend border, px)

TARGET = it8951
SRC_DIR = src
INC_DIR = include

SRCS = $(SRC_DIR)/it8951_driver.c $(SRC_DIR)/it8951_text.c \
       $(SRC_DIR)/it8951_image.c $(SRC_DIR)/it8951_diff.c \
       $(SRC_DIR)/main.c

CC = gcc
CFLAGS = -Wall -O2 -I$(INC_DIR) -I$(SRC_DIR) -I/usr/include/freetype2
LDFLAGS = -lgpiod -lfreetype -lm

OBJ_DIR = obj
OBJS = $(patsubst $(SRC_DIR)/%.c,$(OBJ_DIR)/%.o,$(SRCS))

.PHONY: all clean install

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(CFLAGS) $^ -o $@ $(LDFLAGS)

$(OBJ_DIR)/%.o: $(SRC_DIR)/%.c | $(OBJ_DIR)
	$(CC) $(CFLAGS) -c $< -o $@

$(OBJ_DIR):
	mkdir -p $(OBJ_DIR)

# Build on the Orange Pi (native aarch64). Run from this dir on the Pi:
#   make build-pi && sudo cp it8951 /opt/eink-calendar/bin/it8951
build-pi:
	gcc -Wall -O2 -Iinclude -Isrc -I/usr/include/freetype2 \
		src/it8951_driver.c src/it8951_text.c src/it8951_image.c \
		src/it8951_diff.c src/main.c \
		-o $(TARGET) \
		-lgpiod -lfreetype -lm

clean:
	rm -rf $(OBJ_DIR) $(TARGET)

# Install to /opt/eink-calendar/bin (calendar deploy path)
install: $(TARGET)
	sudo mkdir -p /opt/eink-calendar/bin
	sudo cp $(TARGET) /opt/eink-calendar/bin/it8951