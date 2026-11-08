#!/usr/bin/env python3
"""qemu_run.py -- boot a raw floppy image headless and prove it really ran.

Why not just "make run": the Makefiles say `kvm -drive ...`, which needs a
display, a PTY and KVM; on a CI runner there is none of that, and a timeout on
a GUI window proves nothing. This harness asks the machine instead:

  * QMP over a unix socket, so commands go in after the guest has booted;
  * `pmemsave 0xB8000` -> the real VGA text framebuffer, decoded back to ASCII.
    That is what a human would read off the screen, and it cannot be faked by
    a compiler that merely exited 0;
  * `info registers` -> EIP/CS, which distinguishes "stuck in the bootloader"
    from "reached the kernel" (16-bit real mode vs 32-bit protected mode).

Exit codes: 0 all expected strings found, 1 something missing, 2 harness error.
"""
import json, os, re, socket, subprocess, sys, time, argparse

VGA_ADDR, VGA_LEN = 0xB8000, 4000          # 80x25 text mode, char+attr pairs


class QMP:
    def __init__(self, path, timeout=25.0):
        self.deadline = time.time() + timeout
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        while True:
            try:
                self.s.connect(path)
                break
            except OSError:
                if time.time() > self.deadline:
                    raise
                time.sleep(0.2)
        self.s.settimeout(max(1.0, self.deadline - time.time()))
        self._read()                                   # greeting line
        self.cmd({"execute": "qmp_capabilities"})

    def cmd(self, obj):
        self.s.sendall((json.dumps(obj) + "\n").encode())
        while True:
            got = self._read()
            if got.get("return") is not None or got.get("error"):
                return got

    def _read(self):
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = self.s.recv(65536)
            if not chunk:
                raise RuntimeError("QMP closed")
            buf += chunk
        return json.loads(buf)

    def hmp(self, line):
        return self.cmd({"execute": "human-monitor-command",
                         "arguments": {"command-line": line}})

    def close(self):
        try:
            self.cmd({"execute": "quit"})
        except Exception:
            pass
        self.s.close()


def vga_text(blob):
    """Decode a VGA text-mode dump: every even byte is the ASCII char."""
    out, line = [], []
    for i in range(0, len(blob), 2):
        c = blob[i]
        out.append(chr(c) if 32 <= c < 127 else " ")
        if len(out) == 80:
            line.append("".join(out).rstrip())
            out = []
    if out:
        line.append("".join(out).rstrip())
    return [l for l in line if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="raw 1.44MB floppy image")
    ap.add_argument("--expect", nargs="*", default=[],
                    help="strings that must appear on the VGA screen")
    ap.add_argument("--expect-attr", default="",
                    help="hex attribute byte that a guest wrote, e.g. 0f for "
                         "SC_Kernel clear_screen()'s 0x0F20 blank; a cleared "
                         "screen is invisible but its attributes prove the C "
                         "code ran (BIOS leaves 07)")
    ap.add_argument("--min-attr-cells", type=int, default=1900)
    ap.add_argument("--expect-eip", default="",
                    help="exact EIP the guest must be parked at, e.g. 00001005")
    ap.add_argument("--eip-range", default="",
                    help="lo-hi hex range EIP must sit in, e.g. 0x1000-0x7000 "
                         "for a kernel linked at 0x1000; survives a code "
                         "reshuffle that --expect-eip would not")
    ap.add_argument("--parked", action="store_true",
                    help="require EIP to be identical across two samples "
                         "0.6s apart, i.e. the guest is in its halt loop "
                         "rather than crashing or still booting")
    ap.add_argument("--wait", type=float, default=3.0,
                    help="seconds to let the guest boot before probing")
    ap.add_argument("--out", default="qemu-report.json")
    ap.add_argument("--binary", default="qemu-system-i386")
    args = ap.parse_args()

    sock = os.path.abspath("qmp.sock")
    if os.path.exists(sock):
        os.remove(sock)
    vga = os.path.abspath("vga.bin")
    if os.path.exists(vga):
        os.remove(vga)

    # -no-reboot turns a triple-fault reboot loop into a clean shutdown, which
    # is the difference between "hung" and "crashed" in the report.
    # (No -nographic: it force-redirects the serial console and suppresses the
    # display device we need to read VGA memory from.)
    cmd = [args.binary, "-machine", "pc", "-m", "32",
           "-display", "none",
           "-serial", "stdio", "-parallel", "none",
           "-monitor", "none", "-boot", "order=a", "-no-reboot",
           # if=floppy is what both projects ask for: SC_Kernel's bochsrc says
           # `floppya: 1_44=os-image, boot: a`, and vineetp6's stage 1 reads
           # cylinder 0 / head 0 / sector 2 -- CHS geometry only a floppy has.
           "-drive", "if=floppy,format=raw,file=%s" % args.image,
           "-qmp", "unix:%s,server,nowait" % sock]
    log = open("qemu-stdout.log", "wb")
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    report = {"cmd": cmd, "guest": None, "screen": [], "expect": args.expect,
              "missing": list(args.expect), "registers": "", "status": ""}
    rc = 2
    try:
        q = QMP(sock)
        time.sleep(args.wait)
        st = q.cmd({"execute": "query-status"})
        report["status"] = json.dumps(st.get("return", st))
        report["guest"] = st.get("return", {}).get("status", "?")
        # Two independent reads of the framebuffer. HMP numbers must carry a
        # 0x prefix and the filename must be *quoted*: `pmemsave 0xb8000 4000
        # /tmp/vga.bin` fails with "invalid char 't' in expression" because
        # the unquoted argument is parsed as an expression. Both failures
        # arrived as text inside the QMP `return` payload -- human-monitor-
        # command never sets an error object -- so the reply is sniffed for
        # trouble words instead of trusting `bool(r.get("error"))`.
        r = q.hmp("pmemsave 0x%x 0x%x \"%s\"" % (VGA_ADDR, VGA_LEN, vga))
        out = (r.get("return") or "").strip()
        report["pmemsave"] = out
        report["pmemsave_error"] = bool(r.get("error")) or "invalid" in out.lower() \
            or "error" in out.lower() or not os.path.exists(vga)
        r = q.hmp("x/%dxb 0x%x" % (VGA_LEN, VGA_ADDR))
        report["x_bytes"] = (r.get("return") or "").strip()
        r = q.hmp("info registers")
        report["registers"] = (r.get("return") or "").strip()
        # Sample twice: a guest parked in `jmp $` and one making progress both
        # report "running", so the delta in EIP is what says "it is executing".
        eip1 = (re.search(r"EIP=(\S+)", report["registers"]) or [None, "?"])[1]
        time.sleep(0.6)
        r = q.hmp("info registers")
        regs2 = (r.get("return") or "").strip()
        eip2 = (re.search(r"EIP=(\S+)", regs2) or [None, "?"])[1]
        report["eip_delta"] = "%s -> %s" % (eip1, eip2)
        report["advancing"] = eip1 != eip2
        r = q.hmp("x/2i $eip")
        report["at_eip"] = (r.get("return") or "").strip()
        q.close()
    except Exception as e:
        report["error"] = "%s: %s" % (type(e).__name__, e)
        # A guest that triple-faults under -no-reboot powers off; the QMP
        # socket is then gone and the traceback alone would say nothing about
        # *why*. Record the exit status and whatever QEMU printed.
        report["qemu_exit_code"] = p.poll()
    finally:
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
            p.wait()
        log.close()
    report["qemu_exit_code"] = p.poll()
    try:
        tail = open("qemu-stdout.log", "r", errors="replace").read().strip().splitlines()[-6:]
    except Exception:
        tail = []
    report["qemu_log_tail"] = tail

    blob = open(vga, "rb").read() if os.path.exists(vga) else b""
    report["vga_file_bytes"] = len(blob)
    # `x/Nxb` prints 8 bytes per line as "0x000b8000: 0x48 0x07 0x65 ...".
    # Only the tokens AFTER the colon are data -- the address itself is 0xNNNN
    # and a naive regex over the whole line would eat its digits.
    vals = []
    for ln in report.get("x_bytes", "").splitlines():
        if ":" not in ln:
            continue
        head, tail = ln.split(":", 1)
        vals += [int(t, 16) for t in
                 (x.strip() for x in tail.split()) if re.fullmatch(r"0x[0-9a-fA-F]{2}", t)]
    report["x_bytes_len"] = len(vals)
    # Cross-check: the file dump and the text dump are different code paths in
    # QEMU, so agreeing byte for byte is what makes "the screen said X" a
    # measurement rather than a parsing artifact.
    report["vga_sources_agree"] = bool(blob) and len(vals) == len(blob) and \
        blob == bytes(vals)
    if not blob or not any(blob):
        if len(vals) > len(blob):
            blob = bytes(vals)
            report["vga_source"] = "x_bytes"
        else:
            report["vga_source"] = "pmemsave"
    else:
        report["vga_source"] = "pmemsave"
    if blob:
        joined = " | ".join(vga_text(blob))
        report["screen"] = vga_text(blob)
        report["missing"] = [s for s in args.expect if s not in joined]
        report["vga_bytes"] = len(blob)
        report["nonblank_cells"] = sum(1 for i in range(0, len(blob), 2)
                                       if blob[i] not in (0, 32))
        # Attribute histogram: this is how "the kernel cleared the screen" is
        # told apart from "nothing ever happened", since both look blank.
        hist = {}
        for i in range(1, len(blob), 2):
            hist["%02x" % blob[i]] = hist.get("%02x" % blob[i], 0) + 1
        report["attr_hist"] = sorted(hist.items(), key=lambda kv: -kv[1])[:5]
        want = args.expect_attr.lower().replace("0x", "")
        if want:
            report["attr_cells"] = hist.get(want, 0)
            if hist.get(want, 0) < args.min_attr_cells:
                report["missing"].append("attr 0x%s in >=%d cells (saw %d)"
                                         % (want, args.min_attr_cells, hist.get(want, 0)))
    eip = (re.search(r"EIP=(\S+)", report["registers"]) or [None, "?"])[1]
    if args.expect_eip:
        want = args.expect_eip.lower().replace("0x", "")
        if eip.lower().replace("0x", "") != want:
            report["missing"].append("EIP %s != %s" % (eip, want))
    if args.eip_range:
        lo, hi = [int(x, 16) for x in args.eip_range.split("-")]
        try:
            now = int(eip, 16)
        except ValueError:
            now = -1
        report["eip_in_range"] = lo <= now <= hi
        if not report["eip_in_range"]:
            report["missing"].append("EIP %s outside %s" % (eip, args.eip_range))
    if args.parked:
        report["parked"] = report.get("advancing") is False
        if not report["parked"]:
            report["missing"].append("EIP still moving (%s)" % report.get("eip_delta"))

    json.dump(report, open(args.out, "w"), indent=1)

    print("guest status : %s" % report["status"])
    print("vga source   : %s (%d bytes, pmemsave_error=%s, file=%d, text=%d, agree=%s)" %
          (report.get("vga_source"), report.get("vga_bytes", 0),
           report.get("pmemsave_error"), report.get("vga_file_bytes", 0),
           report.get("x_bytes_len", 0), report.get("vga_sources_agree")))
    print("screen (%d non-blank cells):" % report.get("nonblank_cells", 0))
    for l in report["screen"][:12]:
        print("   | %s" % l)
    print("EIP          : %s  (%s; advancing=%s)" %
          ((re.search(r"EIP=(\S+)", report["registers"]) or [None, "?"])[1],
           report.get("eip_delta", "?"), report.get("advancing")))
    if report.get("at_eip"):
        print("at EIP       : %s" % report["at_eip"].replace("\n", "  ")[:140])
    print("registers    : %s" % " ".join(report["registers"].split())[:200])
    if report.get("error"):
        print("harness error: %s (qemu exit %s)" % (report["error"],
                                                    report.get("qemu_exit_code")))
    if report.get("qemu_log_tail"):
        print("qemu said    : %s" % " / ".join(report["qemu_log_tail"])[:400])
    # A pass needs evidence, not silence: "missing" is empty both when the
    # screen really showed the string and when nothing was captured at all, so
    # gate on the framebuffer dump first. A blank screen is still a pass if it
    # is blank *because the kernel cleared it* (--expect-attr), which is the
    # SC_Kernel case -- its prints are commented out upstream.
    if report.get("missing"):
        print("MISSING      : %s" % ", ".join(report["missing"]))
        rc = 1
    elif blob and (report["screen"] or args.expect_attr or args.expect_eip
                   or args.eip_range or args.parked):
        print("all expectations met (screen lines=%d, attr cells=%s, eip=%s)" %
              (len(report["screen"]), report.get("attr_cells", "-"), eip))
        rc = 0
    else:
        print("no VGA content captured (%s)" % report.get("error", ""))
        rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
