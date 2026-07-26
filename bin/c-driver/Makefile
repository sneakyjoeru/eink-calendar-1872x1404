# Makefile for IT8951 e-paper display driver (C version)
# Target: Orange Pi Zero 2W (aarch64, H616)

TARGET = it8951
SRC_DIR = src
INC_DIR = include

SRCS = $(SRC_DIR)/it8951_driver.c $(SRC_DIR)/it8951_text.c $(SRC_DIR)/it8951_image.c $(SRC_DIR)/main.c

CC = gcc
CFLAGS = -Wall -O2 -I$(INC_DIR) -I/usr/include/freetype2
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

# Build on the Orange Pi (native aarch64)
build-pi:
	gcc -Wall -O2 -Iinclude \
		src/it8951_driver.c src/it8951_text.c src/it8951_image.c src/main.c \
		-o $(TARGET) \
		-lgpiod -lfreetype -lm

clean:
	rm -rf $(OBJ_DIR) $(TARGET)

# Install to /opt
install: $(TARGET)
	sudo mkdir -p /opt/it8951-epaper-c
	sudo cp $(TARGET) /opt/it8951-epaper-c/
	sudo cp -r README.md /opt/it8951-epaper-c/