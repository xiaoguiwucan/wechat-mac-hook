#!/usr/bin/env python3
"""Add one LC_LOAD_DYLIB command to every 64-bit slice of a Mach-O file."""

import pathlib
import struct
import sys

FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
MH_MAGIC_64 = 0xFEEDFACF
LC_LOAD_DYLIB = 0xC
LC_SEGMENT_64 = 0x19


def align8(value: int) -> int:
    return (value + 7) & ~7


def arch_slices(data: bytes):
    if len(data) < 4:
        raise SystemExit("file too small")
    magic_be = struct.unpack_from(">I", data, 0)[0]
    magic_le = struct.unpack_from("<I", data, 0)[0]
    if magic_be in (FAT_MAGIC, FAT_MAGIC_64):
        nfat = struct.unpack_from(">I", data, 4)[0]
        cursor = 8
        result = []
        for _ in range(nfat):
            if magic_be == FAT_MAGIC:
                _, _, offset, size, _ = struct.unpack_from(">iiIII", data, cursor)
                cursor += 20
            else:
                _, _, offset, size, _, _, _ = struct.unpack_from(
                    ">iiQQIII", data, cursor
                )
                cursor += 32
            result.append((offset, size))
        return result
    if magic_le == MH_MAGIC_64:
        return [(0, len(data))]
    raise SystemExit("unsupported Mach-O magic")


def min_section_offset(data: bytes, base: int, ncmds: int):
    cursor = base + 32
    minimum = None
    for _ in range(ncmds):
        command, command_size = struct.unpack_from("<II", data, cursor)
        if command == LC_SEGMENT_64:
            section_count = struct.unpack_from("<I", data, cursor + 64)[0]
            section = cursor + 72
            for _ in range(section_count):
                offset = struct.unpack_from("<I", data, section + 48)[0]
                if offset and (minimum is None or offset < minimum):
                    minimum = offset
                section += 80
        cursor += command_size
    return minimum


def has_load(data: bytes, base: int, ncmds: int, dylib: str) -> bool:
    cursor = base + 32
    for _ in range(ncmds):
        command, command_size = struct.unpack_from("<II", data, cursor)
        if command == LC_LOAD_DYLIB:
            name_offset = struct.unpack_from("<I", data, cursor + 8)[0]
            raw = data[cursor + name_offset : cursor + command_size]
            if raw.split(b"\0", 1)[0] == dylib.encode():
                return True
        cursor += command_size
    return False


def inject_one(buffer: bytearray, base: int, dylib: str) -> bool:
    if struct.unpack_from("<I", buffer, base)[0] != MH_MAGIC_64:
        raise SystemExit(f"slice at {base} is not a 64-bit Mach-O")
    _, _, _, _, ncmds, sizeofcmds, _, _ = struct.unpack_from(
        "<IiiIIIII", buffer, base
    )
    if has_load(buffer, base, ncmds, dylib):
        return False
    name = dylib.encode() + b"\0"
    command_size = align8(24 + len(name))
    command = struct.pack("<IIIIII", LC_LOAD_DYLIB, command_size, 24, 2, 0, 0)
    command += name
    command += b"\0" * (command_size - len(command))
    insert_at = base + 32 + sizeofcmds
    minimum = min_section_offset(buffer, base, ncmds)
    if minimum is None:
        raise SystemExit(f"cannot find a section offset for slice at {base}")
    slack = base + minimum - insert_at
    if slack < command_size:
        raise SystemExit(
            f"not enough Mach-O header padding at slice {base}: "
            f"need {command_size}, have {slack}"
        )
    if any(buffer[insert_at : insert_at + command_size]):
        raise SystemExit(f"Mach-O header padding is not empty at slice {base}")
    buffer[insert_at : insert_at + command_size] = command
    struct.pack_into("<II", buffer, base + 16, ncmds + 1, sizeofcmds + command_size)
    return True


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} MACHO DYLIB_LOAD_PATH", file=sys.stderr)
        raise SystemExit(2)
    path = pathlib.Path(sys.argv[1])
    dylib = sys.argv[2]
    original = path.read_bytes()
    buffer = bytearray(original)
    changed = False
    for base, _ in arch_slices(original):
        changed |= inject_one(buffer, base, dylib)
    if changed:
        path.write_bytes(buffer)
        print(f"Injected {dylib} into {path}")
    else:
        print(f"Already injected: {dylib}")


if __name__ == "__main__":
    main()
