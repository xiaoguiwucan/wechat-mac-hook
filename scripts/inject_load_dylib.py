#!/usr/bin/env python3
import pathlib
import struct
import sys

FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
MH_MAGIC_64 = 0xFEEDFACF
LC_LOAD_DYLIB = 0xC
LC_SEGMENT_64 = 0x19


def align8(n: int) -> int:
    return (n + 7) & ~7


def arch_slices(data: bytes):
    if len(data) < 4:
        raise SystemExit("file too small")
    magic_be = struct.unpack_from(">I", data, 0)[0]
    magic_le = struct.unpack_from("<I", data, 0)[0]
    if magic_be in (FAT_MAGIC, FAT_MAGIC_64):
        nfat = struct.unpack_from(">I", data, 4)[0]
        off = 8
        out = []
        for _ in range(nfat):
            if magic_be == FAT_MAGIC:
                cputype, cpusubtype, offset, size, align = struct.unpack_from(">iiIII", data, off)
                off += 20
            else:
                cputype, cpusubtype, offset, size, align, reserved1, reserved2 = struct.unpack_from(">iiQQIII", data, off)
                off += 32
            out.append((offset, size, cputype))
        return out
    if magic_le == MH_MAGIC_64:
        return [(0, len(data), None)]
    raise SystemExit("unsupported Mach-O magic")


def min_section_offset(data: bytes, base: int, ncmds: int):
    cur = base + 32
    min_off = None
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, cur)
        if cmd == LC_SEGMENT_64:
            nsects = struct.unpack_from("<I", data, cur + 64)[0]
            sec = cur + 72
            for _ in range(nsects):
                off = struct.unpack_from("<I", data, sec + 48)[0]
                if off and (min_off is None or off < min_off):
                    min_off = off
                sec += 80
        cur += cmdsize
    return min_off


def has_load(data: bytes, base: int, ncmds: int, dylib: str) -> bool:
    cur = base + 32
    needle = dylib.encode() + b"\0"
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, cur)
        if cmd == LC_LOAD_DYLIB:
            name_off = struct.unpack_from("<I", data, cur + 8)[0]
            raw = data[cur + name_off:cur + cmdsize]
            name = raw.split(b"\0", 1)[0]
            if name == dylib.encode():
                return True
        cur += cmdsize
    return False


def inject_one(buf: bytearray, base: int, size: int, dylib: str) -> bool:
    magic = struct.unpack_from("<I", buf, base)[0]
    if magic != MH_MAGIC_64:
        raise SystemExit(f"slice at {base} is not MH_MAGIC_64")
    magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved = struct.unpack_from("<IiiIIIII", buf, base)
    if has_load(buf, base, ncmds, dylib):
        return False
    name = dylib.encode("utf-8") + b"\0"
    cmdsize = align8(24 + len(name))
    cmd = struct.pack("<IIIIII", LC_LOAD_DYLIB, cmdsize, 24, 2, 0, 0) + name
    cmd += b"\0" * (cmdsize - len(cmd))
    insert_at = base + 32 + sizeofcmds
    min_off = min_section_offset(buf, base, ncmds)
    if min_off is None:
        raise SystemExit(f"cannot find section offset for slice at {base}")
    slack = base + min_off - insert_at
    if slack < cmdsize:
        raise SystemExit(f"not enough Mach-O header padding in slice at {base}: need {cmdsize}, have {slack}")
    if any(b != 0 for b in buf[insert_at:insert_at + cmdsize]):
        raise SystemExit(f"header padding is not zero at slice {base}")
    buf[insert_at:insert_at + cmdsize] = cmd
    struct.pack_into("<II", buf, base + 16, ncmds + 1, sizeofcmds + cmdsize)
    return True


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} MACHO DYLIB_LOAD_PATH", file=sys.stderr)
        sys.exit(2)
    path = pathlib.Path(sys.argv[1])
    dylib = sys.argv[2]
    data = path.read_bytes()
    buf = bytearray(data)
    changed = False
    for base, size, cputype in arch_slices(data):
        changed |= inject_one(buf, base, size, dylib)
    if changed:
        backup = path.with_suffix(path.suffix + ".before-wechat-second-hook")
        if not backup.exists():
            backup.write_bytes(data)
        path.write_bytes(buf)
        print(f"Injected {dylib} into {path}")
    else:
        print(f"Already injected: {dylib}")

if __name__ == "__main__":
    main()
