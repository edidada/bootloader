#!/usr/bin/env bash
# Package the two stages the way stage 1 expects to find them on disk, boot the
# result, and read the answer off the emulated machine instead of off the
# compiler's exit status.
set -euo pipefail

for f in boot.asm.bin loader.bin boot.bin; do
  if [ ! -f "$f" ]; then
    echo "::error::make all did not produce $f"
    exit 1
  fi
done

# Stage 1 loads exactly one sector, so the sector must be *exactly* 512 bytes
# long: `cat boot.asm.bin loader.bin` shifts the loader by however many bytes
# the first file is over, and the loader then starts mid-instruction. This is
# one of the two upstream defects the edidada/main commit fixes; assert it here
# so a regression cannot come back silently.
SECTOR=$(stat -c%s boot.asm.bin)
LOADER=$(stat -c%s loader.bin)
IMAGE=$(stat -c%s boot.bin)
echo "boot sector   : $SECTOR bytes (must be 512)"
echo "loader.bin    : $LOADER bytes flat binary -- $(file -b loader.bin | cut -c1-40)"
echo "boot.bin      : $IMAGE bytes = 512 + $LOADER"
[ "$SECTOR" = 512 ] || { echo "::error::boot sector is $SECTOR bytes, not 512"; exit 1; }
[ "$IMAGE" = "$((512 + LOADER))" ] || { echo "::error::cat misaligned the loader"; exit 1; }
# `data` and not `ELF`: if the linker ever goes back to emitting an ELF for a
# flat-only consumer, stage 1 would execute the 7f 45 4c 46 header as code.
if file -b loader.bin | grep -q ELF; then
  echo "::error::loader.bin is an ELF; stage 1 jumps to it as raw bytes"
  exit 1
fi

FLOPPY=/tmp/bootloader.floppy
dd if=/dev/zero of="$FLOPPY" bs=1024 count=1440 status=none
dd if=boot.bin of="$FLOPPY" conv=notrunc bs=512 status=none
echo "floppy        : $(stat -c%s "$FLOPPY") bytes -> $FLOPPY"

# What is proved here:
#
#   --expect          loader.c writes "Hello from C bootloader!" byte-pair by
#                     byte into 0xB8000. Seeing the string in the VGA text
#                     buffer means the C loop ran -- not merely that gcc and
#                     nasm exited 0.
#   --eip-range       stage 2 is linked at `. = 0x8000` (linker.ld), so an EIP
#                     inside 0x8000-0x8fff says the CPU is executing the
#                     loader itself, with the boot sector's 0x7C00 far behind.
#   --parked          kmain ends in `while (1) hlt`; two identical `info
#                     registers` samples 0.6s apart (with HLT=1 in the dump)
#                     are the idling kernel, not a reboot loop.
python3 edidada-ci/qemu_boot_check.py \
  --image "$FLOPPY" --wait 5 \
  --expect "Hello from C bootloader" \
  --eip-range 0x8000-0x8fff --parked \
  --out qemu-report.json
