# Makefile for building our two-stage bootloader

# ------------------------------------------------------
# Tool definitions
# ------------------------------------------------------
# CC: C compiler
# LD: Linker
# AS: Assembler
# We use the native gcc and ld in 32-bit mode (via -m32)
CC      = gcc                     
LD      = ld                      
AS      = nasm                    

# ------------------------------------------------------
# Compiler / Linker flags
# ------------------------------------------------------
# -m32             : produce 32-bit code
# -ffreestanding   : don’t assume standard libraries (no glibc)
# -O2              : optimize for speed
# -Wall -Wextra    : enable most warnings
# -fno-pic -no-pie : the linker below is GNU ld with a flat binary output and
#                  : no dynamic section; Ubuntu's gcc defaults to PIE, whose
#                  : GOT-relative calls fail to link ("undefined reference to
#                  : `_GLOBAL_OFFSET_TABLE_`"). Measured on gcc 13.3.0.
# CFLAGS  = -m32 -ffreestanding -O2 -Wall -Wextra          # upstream
CFLAGS  = -m32 -ffreestanding -O2 -Wall -Wextra -fno-pic -no-pie

# -m elf_i386      : produce 32-bit ELF binary
# -T linker.ld     : use our custom linker script
LDFLAGS = -m elf_i386 -T linker.ld

# ------------------------------------------------------
# Default target: build the final raw image boot.bin
# ------------------------------------------------------
all: boot.bin

# boot.bin is simply the 512-byte boot sector + the C “kernel” image
boot.bin: boot.asm.bin loader.bin
	cat $^ > $@               # concatenate both binaries into one file

# ------------------------------------------------------
# Assemble boot.asm into a flat 512-byte binary
# ------------------------------------------------------
boot.asm.bin: boot.asm
	$(AS) -f bin boot.asm -o $@  # NASM format=bin

# ------------------------------------------------------
# Compile and link the C loader
# ------------------------------------------------------
loader.bin: loader.c linker.ld
	$(CC) $(CFLAGS) -c loader.c -o loader.o   # compile to object .o
	# The comment on the upstream line promised "link into flat binary at
	# 0x8000", but the command itself only said -T linker.ld, so ld emitted
	# an ELF: `file loader.bin` = "ELF 32-bit LSB executable", and stage 1
	# jumped to the bytes 7f 45 4c 46 ("\x7fELF") as if they were code.
	# $(LD) $(LDFLAGS) loader.o -o loader.bin   # link into flat binary at 0x8000
	$(LD) $(LDFLAGS) -N --oformat binary -e kmain loader.o -o loader.bin   # really flat now

# ------------------------------------------------------
# Clean up all generated files
# ------------------------------------------------------
clean:
	rm -f *.bin *.o

.PHONY: all clean   # these names aren’t real files

