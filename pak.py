#!/usr/bin/env python3
"""
Structure:
  "NEKOPACK4A"
  uint32 index_size
  repeated index entries:
    uint32 name_size
    char[name_size] cp932 name, normally NUL-terminated
    uint32 data_offset XOR signed_byte_sum(name_bytes)
    uint32 stored_size XOR signed_byte_sum(name_bytes)
  repeated file data:
    zlib stream plus size footer with first 0x20 bytes XOR-masked
    uint32 unpacked_size footer
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable


MAGIC_A = b"NEKOPACK4A"
CHUNK_SIZE = 1024 * 1024
UINT32_MAX = 0xFFFFFFFF
DEFAULT_ENCODING = "cp932"
DEFAULT_COMPRESSION_LEVEL = 9
DEFAULT_PACK_WORKERS = max(1, min(4, os.cpu_count() or 1))


class PakError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    name_bytes: bytes
    offset: int
    stored_size: int


@dataclass(frozen=True)
class PreparedEntry:
    name: str
    name_bytes: bytes
    temp_path: Path
    compressed_size: int
    unpacked_size: int
    stored_size: int
    offset: int = 0


def read_u32(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<I", buf, offset)[0]


def write_u32(value: int) -> bytes:
    if not 0 <= value <= UINT32_MAX:
        raise PakError(f"value does not fit uint32: {value}")
    return struct.pack("<I", value)


def signed_byte_sum(data: bytes) -> int:
    total = sum(byte if byte < 0x80 else byte - 0x100 for byte in data)
    return total & UINT32_MAX


def xor_u32(value: int, key: int) -> int:
    return (value & UINT32_MAX) ^ (key & UINT32_MAX)


def mask_payload_header(header: bytearray, stored_size: int) -> None:
    key = stored_size // 8 + 0x22
    for index in range(min(0x20, len(header))):
        header[index] ^= key & 0xFF
        key = (key << 3) & UINT32_MAX


def display_name(name_bytes: bytes, encoding: str) -> str:
    raw = name_bytes.split(b"\0", 1)[0]
    return raw.decode(encoding)


def encoded_name(name: str, encoding: str) -> bytes:
    if "\0" in name:
        raise PakError(f"NUL byte is not allowed in entry name: {name!r}")
    return name.encode(encoding) + b"\0"


def parse_entries(archive_path: Path, encoding: str) -> list[Entry]:
    with archive_path.open("rb") as archive:
        header = archive.read(14)
        if len(header) < 14 or header[:10] != MAGIC_A:
            raise PakError("Invalid NekoSDK archive!")

        index_size = read_u32(header, 10)
        index = archive.read(index_size)
        if len(index) != index_size:
            raise PakError("truncated index")

        entries: list[Entry] = []
        pos = 0
        archive_size = archive_path.stat().st_size
        while pos < index_size:
            if pos + 4 > index_size:
                raise PakError("truncated name length in index")
            name_size = read_u32(index, pos)
            pos += 4
            if name_size == 0:
                break
            if name_size > 0x100:
                raise PakError(f"entry name is too long: {name_size} bytes")
            if pos + name_size + 8 > index_size:
                raise PakError("truncated entry in index")

            name_bytes = index[pos : pos + name_size]
            pos += name_size
            key = signed_byte_sum(name_bytes)
            offset = xor_u32(read_u32(index, pos), key)
            stored_size = xor_u32(read_u32(index, pos + 4), key)
            pos += 8

            if offset + stored_size > archive_size:
                name = display_name(name_bytes, encoding)
                raise PakError(f"entry points outside archive: {name}")
            if stored_size < 4:
                name = display_name(name_bytes, encoding)
                raise PakError(f"entry is too small to contain size footer: {name}")

            entries.append(
                Entry(
                    name=display_name(name_bytes, encoding),
                    name_bytes=name_bytes,
                    offset=offset,
                    stored_size=stored_size,
                )
            )

        if not entries:
            raise PakError("archive index is empty")
        return entries


def safe_output_path(root: Path, entry_name: str) -> Path:
    normalized = entry_name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise PakError(f"unsafe entry path: {entry_name!r}")

    output = root
    for part in pure.parts:
        if ":" in part:
            raise PakError(f"unsafe entry path: {entry_name!r}")
        output = output / part

    root_resolved = root.resolve()
    output_resolved = output.resolve(strict=False)
    try:
        output_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PakError(f"unsafe entry path: {entry_name!r}") from exc
    return output


def decrypt_zlib_stream(
    archive: BinaryIO, entry: Entry, output: BinaryIO, expected_size: int | None
) -> int:
    compressed_size = entry.stored_size - 4
    archive.seek(entry.offset)
    prefix_size = min(0x20, entry.stored_size)
    first = bytearray(archive.read(prefix_size))
    if len(first) != prefix_size:
        raise PakError(f"truncated data: {entry.name}")
    mask_payload_header(first, entry.stored_size)

    decompressor = zlib.decompressobj()
    written = 0

    def feed(chunk: bytes) -> None:
        nonlocal written
        data = decompressor.decompress(chunk)
        if data:
            output.write(data)
            written += len(data)

    compressed_prefix_size = min(prefix_size, compressed_size)
    if compressed_prefix_size:
        feed(bytes(first[:compressed_prefix_size]))

    remaining = compressed_size - compressed_prefix_size
    while remaining:
        chunk = archive.read(min(CHUNK_SIZE, remaining))
        if not chunk:
            raise PakError(f"truncated compressed stream: {entry.name}")
        remaining -= len(chunk)
        feed(chunk)

    tail = decompressor.flush()
    if tail:
        output.write(tail)
        written += len(tail)

    footer = bytearray()
    if prefix_size > compressed_size:
        footer += first[compressed_size:]
    footer += archive.read(4 - len(footer))
    if len(footer) != 4:
        raise PakError(f"missing size footer: {entry.name}")
    unpacked_size = read_u32(footer, 0)
    if expected_size is not None:
        unpacked_size = expected_size
    if written != unpacked_size:
        raise PakError(
            f"size mismatch for {entry.name}: got {written}, expected {unpacked_size}"
        )
    return written


def unpack_archive(archive_path: Path, output_dir: Path, encoding: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    entries = parse_entries(archive_path, encoding)
    with archive_path.open("rb") as archive:
        total = len(entries)
        for index, entry in enumerate(entries, 1):
            print(f"unpacking [{index}/{total}] {entry.name}")
            out_path = safe_output_path(output_dir, entry.name)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            expected_size = None
            archive.seek(entry.offset + entry.stored_size - 4)
            footer = archive.read(4)
            if len(footer) == 4:
                expected_size = read_u32(footer, 0)
            with out_path.open("wb") as output:
                decrypt_zlib_stream(archive, entry, output, expected_size)

    print(f"unpacked {len(entries)} file(s) to {output_dir}")


def iter_source_names(source_dir: Path, encoding: str) -> list[str]:
    names: list[str] = []
    for path in source_dir.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(source_dir).as_posix().replace("/", "\\")
        encoded_name(rel, encoding)
        names.append(rel)
    names.sort()
    if not names:
        raise PakError(f"no files found to pack in {source_dir}")
    return names


def source_path_for_name(source_dir: Path, name: str) -> Path:
    path = safe_output_path(source_dir, name)
    if not path.is_file():
        raise PakError(f"missing source file: {name}")
    return path


def compress_file(source: Path, destination: Path, level: int) -> tuple[int, int]:
    compressor = zlib.compressobj(level)
    unpacked_size = 0
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        while True:
            chunk = input_file.read(CHUNK_SIZE)
            if not chunk:
                break
            unpacked_size += len(chunk)
            output_file.write(compressor.compress(chunk))
        output_file.write(compressor.flush())

    compressed_size = destination.stat().st_size
    if compressed_size > UINT32_MAX or unpacked_size > UINT32_MAX:
        raise PakError(f"file is too large for NEKOPACK4A: {source}")
    return compressed_size, unpacked_size


def prepare_entry(
    source_dir: Path,
    index: int,
    name: str,
    encoding: str,
    level: int,
    temp_dir: Path,
) -> PreparedEntry:
    name_bytes = encoded_name(name, encoding)
    source_path = source_path_for_name(source_dir, name)
    temp_path = temp_dir / f"{index:08d}.z"
    compressed_size, unpacked_size = compress_file(source_path, temp_path, level)
    stored_size = compressed_size + 4
    if stored_size > UINT32_MAX:
        raise PakError(f"compressed file is too large for NEKOPACK4A: {name}")
    return PreparedEntry(
        name=name,
        name_bytes=name_bytes,
        temp_path=temp_path,
        compressed_size=compressed_size,
        unpacked_size=unpacked_size,
        stored_size=stored_size,
    )


def build_index(entries: Iterable[PreparedEntry]) -> bytes:
    index = bytearray()
    for entry in entries:
        key = signed_byte_sum(entry.name_bytes)
        index += write_u32(len(entry.name_bytes))
        index += entry.name_bytes
        index += write_u32(xor_u32(entry.offset, key))
        index += write_u32(xor_u32(entry.stored_size, key))
    index += write_u32(0)
    if len(index) > UINT32_MAX:
        raise PakError("index is too large for NEKOPACK4A")
    return bytes(index)


def write_encrypted_payload(
    output: BinaryIO, temp_path: Path, stored_size: int, unpacked_size: int
) -> None:
    compressed_size = stored_size - 4
    footer = write_u32(unpacked_size)
    prefix_size = min(0x20, stored_size)
    with temp_path.open("rb") as input_file:
        compressed_prefix_size = min(prefix_size, compressed_size)
        first = bytearray(input_file.read(compressed_prefix_size))
        footer_prefix_size = prefix_size - compressed_prefix_size
        if footer_prefix_size:
            first += footer[:footer_prefix_size]
        mask_payload_header(first, stored_size)
        output.write(first)
        shutil.copyfileobj(input_file, output, CHUNK_SIZE)
    output.write(footer[footer_prefix_size:])


def prepare_entries(
    source_dir: Path,
    names: list[str],
    encoding: str,
    level: int,
    temp_dir: Path,
    workers: int,
) -> list[PreparedEntry]:
    prepared: list[PreparedEntry | None] = [None] * len(names)
    if workers <= 1 or len(names) == 1:
        for index, name in enumerate(names):
            entry = prepare_entry(source_dir, index, name, encoding, level, temp_dir)
            prepared[index] = entry
            print(f"packing [{index + 1}/{len(names)}] {name}")
        return [entry for entry in prepared if entry is not None]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                prepare_entry,
                source_dir,
                index,
                name,
                encoding,
                level,
                temp_dir,
            ): index
            for index, name in enumerate(names)
        }
        completed = 0
        for future in as_completed(futures):
            index = futures[future]
            entry = future.result()
            prepared[index] = entry
            completed += 1
            print(f"packing [{completed}/{len(names)}] {entry.name}")

    missing = [names[index] for index, entry in enumerate(prepared) if entry is None]
    if missing:
        raise PakError(f"failed to prepare {len(missing)} file(s)")
    return [
        entry
        for entry in prepared
        if entry is not None
    ]


def assign_offsets(entries: list[PreparedEntry]) -> list[PreparedEntry]:
    index_size = sum(4 + len(entry.name_bytes) + 8 for entry in entries) + 4
    offset = len(MAGIC_A) + 4 + index_size
    assigned: list[PreparedEntry] = []
    for entry in entries:
        if offset > UINT32_MAX:
            raise PakError("archive offset exceeds uint32 range")
        assigned.append(
            PreparedEntry(
                name=entry.name,
                name_bytes=entry.name_bytes,
                temp_path=entry.temp_path,
                compressed_size=entry.compressed_size,
                unpacked_size=entry.unpacked_size,
                stored_size=entry.stored_size,
                offset=offset,
            )
        )
        offset += entry.stored_size
    if offset > UINT32_MAX:
        raise PakError("archive exceeds uint32 range")
    return assigned


def pack_archive(
    source_dir: Path,
    output_path: Path,
    encoding: str,
    level: int,
    workers: int,
) -> None:
    if not source_dir.is_dir():
        raise PakError(f"source is not a directory: {source_dir}")

    names = iter_source_names(source_dir, encoding)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="neko_pak_") as tmp:
        prepared = prepare_entries(source_dir, names, encoding, level, Path(tmp), workers)
        assigned = assign_offsets(prepared)
        index = build_index(assigned)

        temp_output = output_path.with_suffix(output_path.suffix + ".tmp")
        try:
            with temp_output.open("wb") as output:
                output.write(MAGIC_A)
                output.write(write_u32(len(index)))
                output.write(index)
                for entry in assigned:
                    write_encrypted_payload(
                        output, entry.temp_path, entry.stored_size, entry.unpacked_size
                    )
            os.replace(temp_output, output_path)
        finally:
            if temp_output.exists():
                temp_output.unlink()

    print(f"packed {len(names)} file(s) to {output_path}")


def pack_worker_count() -> int:
    value = os.environ.get("NEKO_PAK_WORKERS")
    if value is None:
        return DEFAULT_PACK_WORKERS
    try:
        workers = int(value)
    except ValueError as exc:
        raise PakError("NEKO_PAK_WORKERS must be a positive integer") from exc
    if workers < 1:
        raise PakError("NEKO_PAK_WORKERS must be a positive integer")
    return workers


def compression_level() -> int:
    value = os.environ.get("NEKO_PAK_LEVEL")
    if value is None:
        return DEFAULT_COMPRESSION_LEVEL
    try:
        level = int(value)
    except ValueError as exc:
        raise PakError("NEKO_PAK_LEVEL must be an integer from 0 to 9") from exc
    if not 0 <= level <= 9:
        raise PakError("NEKO_PAK_LEVEL must be an integer from 0 to 9")
    return level


def usage() -> str:
    return (
        "Usage:\n"
        "  neko_pak.py -u infile.pak outfolder\n"
        "  neko_pak.py -p infolder outfile.pak\n"
    )


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) != 3 or argv[0] not in ("-u", "-p"):
        print(usage(), file=sys.stderr, end="")
        return 2

    try:
        if argv[0] == "-u":
            unpack_archive(Path(argv[1]), Path(argv[2]), DEFAULT_ENCODING)
        else:
            pack_archive(
                Path(argv[1]),
                Path(argv[2]),
                DEFAULT_ENCODING,
                compression_level(),
                pack_worker_count(),
            )
    except (OSError, UnicodeError, zlib.error, PakError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
