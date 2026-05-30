#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path


MAGIC = b"NEKOSDK_ADVSCRIPT2\0"
ENCODING = "cp932"
TEXT_OPCODE = 5


class ScriptError(Exception):
    pass


@dataclass
class Record:
    header: tuple[int, int, int, int]
    nums32: list[int]
    extra: int
    nums16: list[int]
    main: str
    args: list[str]
    main_had_nul: bool
    args_had_nul: list[bool]


@dataclass
class Script:
    records: list[Record]


def read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def pack_u32(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ScriptError(f"value does not fit uint32: {value}")
    return struct.pack("<I", value)


def decode_cstring(raw: bytes) -> tuple[str, bool]:
    had_nul = raw.endswith(b"\0")
    if had_nul:
        raw = raw[:-1]
    return raw.decode(ENCODING), had_nul


def encode_string(text: str, had_nul: bool) -> bytes:
    if "\0" in text:
        raise ScriptError("NUL bytes are not allowed in script strings")
    raw = text.encode(ENCODING)
    if had_nul or text:
        raw += b"\0"
    return raw


def read_script(path: Path) -> Script:
    data = path.read_bytes()
    if not data.startswith(MAGIC):
        raise ScriptError(f"not a NEKOSDK_ADVSCRIPT2 file: {path}")

    pos = len(MAGIC)
    if pos + 4 > len(data):
        raise ScriptError(f"truncated script header: {path}")
    count = read_u32(data, pos)
    pos += 4

    records: list[Record] = []
    for index in range(count):
        start = pos
        try:
            header = struct.unpack_from("<4I", data, pos)
            pos += 16
            nums32 = list(struct.unpack_from("<32I", data, pos))
            pos += 128
            extra = read_u32(data, pos)
            pos += 4
            nums16 = list(struct.unpack_from("<16I", data, pos))
            pos += 64

            main_len = read_u32(data, pos)
            pos += 4
            main, main_had_nul = (
                decode_cstring(data[pos : pos + main_len]) if main_len else ("", False)
            )
            pos += main_len

            args = []
            args_had_nul = []
            for _ in range(32):
                arg_len = read_u32(data, pos)
                pos += 4
                arg, had_nul = (
                    decode_cstring(data[pos : pos + arg_len]) if arg_len else ("", False)
                )
                pos += arg_len
                args.append(arg)
                args_had_nul.append(had_nul)
        except (struct.error, UnicodeDecodeError) as exc:
            raise ScriptError(f"bad record {index} at 0x{start:X} in {path}") from exc
        if pos > len(data):
            raise ScriptError(f"record {index} runs past EOF in {path}")
        records.append(
            Record(header, nums32, extra, nums16, main, args, main_had_nul, args_had_nul)
        )

    if pos != len(data):
        raise ScriptError(f"unexpected trailing data in {path}: 0x{pos:X} != 0x{len(data):X}")
    return Script(records)


def write_script(script: Script, path: Path) -> None:
    output = bytearray(MAGIC)
    output += pack_u32(len(script.records))
    for record in script.records:
        output += struct.pack("<4I", *record.header)
        output += struct.pack("<32I", *record.nums32)
        output += pack_u32(record.extra)
        output += struct.pack("<16I", *record.nums16)

        main = encode_string(record.main, record.main_had_nul)
        output += pack_u32(len(main))
        output += main

        if len(record.args) != 32:
            raise ScriptError("internal error: record must have 32 argument strings")
        if len(record.args_had_nul) != 32:
            raise ScriptError("internal error: record must have 32 string flags")
        for arg, had_nul in zip(record.args, record.args_had_nul):
            encoded = encode_string(arg, had_nul)
            output += pack_u32(len(encoded))
            output += encoded

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        temp_path.write_bytes(output)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def is_text_record(record: Record) -> bool:
    return record.header[3] == TEXT_OPCODE and len(record.args) >= 2 and bool(record.args[1])


def is_choice_record(record: Record) -> bool:
    if record.header[3] == TEXT_OPCODE:
        return False
    if record.main.startswith("選択肢\r") or record.main.startswith("選択肢\n"):
        return True
    haystack = record.main + "\n" + "\n".join(record.args)
    markers = ("[選択", "選択肢", "[セレクト", "choice", "Choice", "select", "Select")
    return any(marker in haystack for marker in markers)


def split_main_choice(main: str) -> tuple[str, str] | None:
    if main.startswith("選択肢\r\n"):
        return "選択肢\r\n", main[5:]
    if main.startswith("選択肢\n"):
        return "選択肢\n", main[4:]
    if main.startswith("選択肢\r"):
        return "選択肢\r", main[4:]
    return None


def set_main_choice(record: Record, choice: str) -> None:
    split = split_main_choice(record.main)
    prefix = split[0] if split else "選択肢\r\n"
    record.main = prefix + choice
    record.main_had_nul = True


def extract_choice_texts(record: Record) -> list[tuple[str, int, str]]:
    split = split_main_choice(record.main)
    if split:
        return [("main", -1, split[1])]

    results = []
    for index, value in enumerate(record.args):
        if value and index not in (30, 31):
            results.append(("arg", index, value))
    return results


def make_text_main(name: str, message: str, old_main: str) -> str:
    voice = ""
    for line in old_main.splitlines():
        if line.startswith("[テキスト表示]"):
            parts = line.split()
            for part in parts[1:]:
                if "\\" in part or "/" in part:
                    voice = part
                    break
            break
    speaker = name if name else ""
    first = f"[テキスト表示] {speaker} {voice}".rstrip()
    display_message = message.replace("\r\n", "\n")
    return f"{first}\n{display_message}\n\n"


def export_items(script: Script) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for record in script.records:
        if is_text_record(record):
            name = record.args[0]
            message = record.args[1]
            if name:
                items.append({"name": name, "message": message})
            else:
                items.append({"message": message})
        elif is_choice_record(record):
            for _, _, choice in extract_choice_texts(record):
                items.append({"choice": choice})
    return items


def import_items(script: Script, items: list[dict[str, str]], source_name: str) -> int:
    index = 0
    changed = 0
    for record in script.records:
        if is_text_record(record):
            if index >= len(items):
                raise ScriptError(f"not enough JSON entries for {source_name}")
            item = items[index]
            if "message" not in item:
                raise ScriptError(f"entry {index} should contain message in {source_name}")

            name = str(item.get("name", record.args[0]))
            message = str(item["message"])
            text_changed = record.args[0] != name or record.args[1] != message
            if text_changed:
                changed += 1
            record.args[0] = name
            record.args[1] = message
            record.args_had_nul[0] = True
            record.args_had_nul[1] = True
            if text_changed:
                record.main = make_text_main(name, message, record.main)
                record.main_had_nul = True
            index += 1
        elif is_choice_record(record):
            for target, arg_index, _ in extract_choice_texts(record):
                if index >= len(items):
                    raise ScriptError(f"not enough JSON entries for {source_name}")
                item = items[index]
                if "choice" not in item:
                    raise ScriptError(f"entry {index} should contain choice in {source_name}")
                choice = str(item["choice"])
                if target == "main":
                    old_choice = split_main_choice(record.main)
                    if old_choice is None or old_choice[1] != choice:
                        changed += 1
                    set_main_choice(record, choice)
                else:
                    if record.args[arg_index] != choice:
                        changed += 1
                    record.args[arg_index] = choice
                    record.args_had_nul[arg_index] = True
                index += 1

    if index != len(items):
        raise ScriptError(f"{len(items) - index} unused JSON entries for {source_name}")
    return changed


def export_file(input_path: Path, output_path: Path) -> None:
    script = read_script(input_path)
    items = export_items(script)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"exported {len(items)} item(s): {input_path} -> {output_path}")


def import_file(original_path: Path, json_path: Path, output_path: Path) -> None:
    script = read_script(original_path)
    items = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise ScriptError(f"JSON root must be a list: {json_path}")
    changed = import_items(script, items, str(json_path))
    write_script(script, output_path)
    print(f"imported {changed} change(s): {original_path} + {json_path} -> {output_path}")


def iter_script_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if path.is_file():
            try:
                if path.read_bytes()[: len(MAGIC)] == MAGIC:
                    files.append(path)
            except OSError:
                pass
    files.sort()
    return files


def export_path(input_path: Path, output_path: Path) -> None:
    if input_path.is_dir():
        output_path.mkdir(parents=True, exist_ok=True)
        files = iter_script_files(input_path)
        if not files:
            raise ScriptError(f"no ADVSCRIPT2 files found: {input_path}")
        for file_path in files:
            rel = file_path.relative_to(input_path)
            export_file(file_path, (output_path / rel).with_suffix(".json"))
    else:
        export_file(input_path, output_path)


def import_path(original_path: Path, json_path: Path, output_path: Path) -> None:
    if original_path.is_dir():
        if not json_path.is_dir():
            raise ScriptError("folder import requires a JSON folder")
        output_path.mkdir(parents=True, exist_ok=True)
        files = iter_script_files(original_path)
        if not files:
            raise ScriptError(f"no ADVSCRIPT2 files found: {original_path}")
        for file_path in files:
            rel = file_path.relative_to(original_path)
            src_json = (json_path / rel).with_suffix(".json")
            if not src_json.exists():
                raise ScriptError(f"missing JSON file: {src_json}")
            import_file(file_path, src_json, output_path / rel)
    else:
        import_file(original_path, json_path, output_path)


def usage() -> str:
    return (
        "Usage:\n"
        "Export:  scr.py -e in.txt out.json\n"
        "         scr.py -e src_folder out_folder\n"
        "Import:  scr.py -i in.txt in.json out.txt\n"
        "         scr.py -i src_folder json_folder out_folder\n"
    )


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        if len(argv) == 3 and argv[0] == "-e":
            export_path(Path(argv[1]), Path(argv[2]))
        elif len(argv) == 4 and argv[0] == "-i":
            import_path(Path(argv[1]), Path(argv[2]), Path(argv[3]))
        else:
            print(usage(), file=sys.stderr, end="")
            return 2
    except (OSError, UnicodeError, json.JSONDecodeError, ScriptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
