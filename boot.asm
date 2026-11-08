; ===================================================================
; boot.asm — Stage 1 boot sector (512 bytes total)
; Loads the second stage from disk into memory and jumps to it.
; ===================================================================

; Tell NASM: this code will be loaded by the BIOS at address 0x0000:0x7C00
[org 0x7C00]

start:
    cli                    ; Disable hardware interrupts while we set up
    xor ax, ax             ; Zero out AX register
    mov ds, ax             ; DS = 0
    mov es, ax             ; ES = 0

    ; ---------------------------------------------------------------
    ; edidada fix 1 of 4 — actually store the drive number.
    ; The comment on boot_drive below always said "BIOS puts drive number into
    ; DL before jumping to us", but no instruction ever wrote it, so the read
    ; used the 0 byte baked into the image. Correct for a floppy by luck only.
    ; ---------------------------------------------------------------
    mov [boot_drive], dl

    ; ---------------------------------------------------------------
    ; Use BIOS interrupt 0x13 to read sector #2 (our loader) from disk
    ; ---------------------------------------------------------------
    mov ah, 0x02           ; AH=2 → disk read function
    mov al, 1              ; AL=1 → number of sectors to read
    mov ch, 0              ; CH=0 → cylinder 0
    mov cl, 2              ; CL=2 → sector 2 (sector 1 is this boot sector)
    mov dh, 0              ; DH=0 → head 0
    mov dl, [boot_drive]   ; DL = BIOS drive number (floppy=0x00, HDD=0x80)

    ; ---------------------------------------------------------------
    ; edidada fix 2 of 4 — read the sector into the address the linker used.
    ; The three upstream lines were:
    ;     mov bx, 0x8000   ; BX=0x8000 → offset to load at
    ;     mov es, bx       ; ES = segment 0x0000, so ES:BX = 0x0000:0x8000
    ;     xor bx, bx       ; BX = 0
    ; which really means ES:BX = 0x8000:0x0000 = linear 0x80000, while both
    ; the jump below and linker.ld's `. = 0x8000` mean linear 0x8000. Measured
    ; in QEMU: `info registers` printed ES =8000 base 00080000 while the C
    ; string sat at 0x802c. ES is already 0 from start:, so BX alone carries
    ; the address.
    ; ---------------------------------------------------------------
    mov bx, 0x8000         ; ES:BX = 0x0000:0x8000 → linear 0x8000
    int 0x13               ; BIOS disk read
    jc disk_error          ; if carry flag set → error

    ; Upstream re-enabled interrupts here with `sti`.  Keep them masked: the
    ; IDT is still the BIOS one, whose entries are 16-bit call gates, so the
    ; first timer tick after CR0.PE raises #GP, then a triple fault, and with
    ; QEMU's -no-reboot the machine simply powers off (that is what this
    ; branch measured: the guest was gone before the harness could ask it
    ; anything).  loader.c halts in a loop anyway, so nothing needs IRQs yet.
    ; sti                    ; Re-enable hardware interrupts
    cli

    ; ---------------------------------------------------------------
    ; Set up a simple stack
    ; ---------------------------------------------------------------
    mov ax, 0x0000
    mov ss, ax             ; Stack segment = 0
    mov sp, 0x7C00         ; Stack pointer just below our code

    ; ---------------------------------------------------------------
    ; edidada fix 3 of 4 — switch to protected mode before running loader.c.
    ; Upstream jumped straight from real mode:
    ;     jmp 0x0000:0x8000
    ; loader.bin is 32-bit code (gcc -m32), but in real mode CS.D = 0, so the
    ; CPU decodes those bytes as 16-bit instructions and walks off into the
    ; zero fill. Measured with QEMU + QMP: EIP 0x0000b501 -> 0x0000f961 while
    ; VGA stayed on SeaBIOS text; nothing of "Hello from C bootloader" was
    ; ever written. So: turn on A20, describe two flat segments in a GDT,
    ; set CR0.PE, and take the far jump through a 32-bit code selector.
    ; ---------------------------------------------------------------
    in  al, 0x92           ; port 0x92 = "fast A20" gate
    or  al, 2              ; bit 1 enables the A20 line
    out 0x92, al

    lgdt [gdtr]
    mov eax, cr0
    or  eax, 1             ; CR0.PE = protected mode enable
    mov cr0, eax
    jmp 0x08:protect       ; far jump reloads CS with the 32-bit code selector

[bits 32]
protect:
    mov ax, 0x10           ; flat data selector, for every segment register
    mov ds, ax
    mov es, ax
    mov ss, ax
    mov fs, ax
    mov gs, ax
    mov eax, 0x8000        ; absolute address stage 2 was linked at
    jmp eax                ; (writing `jmp 0x8000` here would be *relative*)

[bits 16]
disk_error:
    cli                    ; On error, disable interrupts
.hang:
    hlt                    ; Halt CPU
    jmp .hang              ; Infinite loop

; ---------------------------------------------------------------
; GDT: null, code 0x08, data 0x10 — base 0, limit 4 GiB, i.e. flat, so a
; linear address equals a physical one and 0x8000 means 0x8000.
; ---------------------------------------------------------------
gdt_start:
gdt_null:                          ; selector 0x00 — required, never selected
    dd 0x0
    dd 0x0
gdt_code:                          ; selector 0x08
    dw 0xFFFF                      ; limit 0..15
    dw 0x0000                      ; base 0..15
    db 0x00                        ; base 16..23
    db 10011010b                   ; present, DPL0, code+accessed, D/B=1
    db 11001111b                   ; 4 KiB granularity, limit 16..19 = F
    db 0x00                        ; base 24..31
gdt_data:                          ; selector 0x10
    dw 0xFFFF
    dw 0x0000
    db 0x00
    db 10010010b                   ; present, DPL0, data+accessed, D/B=1
    db 11001111b
    db 0x00
gdt_end:

gdtr:
    dw gdt_end - gdt_start - 1     ; a byte count, not an index
    dd gdt_start                   ; lgdt in real mode takes a linear address

; ---------------------------------------------------------------
; One-byte storage to remember which drive BIOS loaded us from
; BIOS puts drive number into DL before jumping to us
;
; edidada fix 4 of 4 — moved above the padding. Upstream declared it *after*
; `dw 0xAA55`, so the assembled sector is 513 bytes:
;     boot_drive: db 0
; `cat boot.asm.bin loader.bin` then starts the loader at byte 513 and sector
; 2 begins one byte late — its first instruction is the tail of the signature.
; Measured with `stat -c%s boot.asm.bin`: 513 before, 512 after.
; ---------------------------------------------------------------
boot_drive: db 0

; ---------------------------------------------------------------
; BIOS signature and padding
; BIOS requires last two bytes of sector to be 0x55AA
; ---------------------------------------------------------------
times 510-($-$$) db 0     ; pad with zeros up to byte 510
dw 0xAA55                 ; 0xAA55 boot signature
