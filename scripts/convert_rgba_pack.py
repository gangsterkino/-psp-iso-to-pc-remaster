#!/usr/bin/env python3
"""Losslessly store an existing RGTX pack; never generate or resample art.

RGTX = b'RGTX' + little-endian uint32 width, height + straight RGBA8.
Filenames remain unchanged: the runtime must detect the stored format by magic.
Pillow roundtrips do NOT prove equivalence in the game's production decoder.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import time

MAX_DIMENSION = 16384
MAX_PIXELS = 64 * 1024 * 1024
TEXTURE_SUFFIXES = {".rgba", ".legacy-rgba"}
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def fail(message):
    raise ValueError(message)


def is_link(path):
    entry = path.lstat()
    return stat.S_ISLNK(entry.st_mode) or bool(
        getattr(entry, "st_file_attributes", 0) & REPARSE_POINT
    )


def reject_link_ancestors(path):
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate) and is_link(candidate):
            fail(f"Symlink/junction/reparse path is not allowed: {candidate}")


def contained(path, parent):
    return path == parent or parent in path.parents


def prepare_paths(args):
    paths = [Path(value).absolute() for value in
             (args.source, args.output, args.report)]
    for path in paths:
        reject_link_ancestors(path)
    source, output, report = [p.resolve() for p in paths]
    if not source.is_dir():
        fail(f"Source must be an existing directory: {source}")
    if contained(output, source) or contained(source, output):
        fail("Source and output must be separate, non-nested directories")
    if contained(report, source) or contained(report, output):
        fail("Report must be outside both source and output directories")
    if contained(output, report):
        fail("Report path cannot be an ancestor of the output directory")
    if os.path.lexists(output):
        fail(f"Output already exists; use a fresh path: {output}")
    if os.path.lexists(report):
        fail(f"Report already exists; use a fresh path: {report}")
    return source, output, report


def inventory(source):
    files = []
    directories = []
    for current, dirs, names in os.walk(source, followlinks=False):
        base = Path(current)
        for name in dirs + names:
            path = base / name
            if is_link(path):
                fail(f"Symlink/junction/reparse input is not allowed: {path}")
            mode = path.stat().st_mode
            if stat.S_ISDIR(mode):
                directories.append(path.relative_to(source))
            elif stat.S_ISREG(mode):
                files.append(path.relative_to(source))
            else:
                fail(f"Input is not a regular file/directory: {path}")
    return sorted(files), sorted(directories)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_rgba(path):
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b"RGTX":
            fail(f"Expected an uncompressed RGTX input: {path}")
        width, height = struct.unpack("<II", header[4:])
        if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
            fail(f"Invalid/bounded dimensions {width}x{height}: {path}")
        if width * height > MAX_PIXELS:
            fail(f"Texture exceeds {MAX_PIXELS} pixel limit: {path}")
        expected = width * height * 4
        if size != 12 + expected:
            fail(f"RGTX length mismatch ({size} != {12 + expected}): {path}")
        pixels = stream.read(expected + 1)
        if len(pixels) != expected:
            fail(f"RGTX changed or has an invalid payload: {path}")
    return header + pixels, pixels, (width, height)


def roundtrip(Image, encoded, dimensions, pixels, label):
    with Image.open(io.BytesIO(encoded)) as decoded:
        if decoded.size != dimensions:
            fail(f"{label}: roundtrip dimensions differ")
        # Opaque WebP may decode as RGB. Normalization only adds alpha=255.
        rgba = decoded.convert("RGBA").tobytes()
    if rgba != pixels:
        fail(f"{label}: RGBA bytes differ (including RGB under alpha=0)")
    return rgba


def convert_texture(Image, path, target, relative, codec):
    raw, pixels, dimensions = read_rgba(path)
    image = Image.frombytes("RGBA", dimensions, pixels)
    chosen = raw
    storage = "rgtx"
    sizes = {"rgtx": len(raw)}
    skipped = {}
    codecs = ["png", "webp"] if codec == "best" else [codec]
    for candidate in codecs:
        if candidate == "webp" and max(dimensions) > 16383:
            if codec == "webp":
                fail(f"WebP dimension limit is 16383: {relative}")
            skipped["webp"] = "dimension exceeds 16383"
            continue
        buffer = io.BytesIO()
        if candidate == "png":
            image.save(buffer, format="PNG", compress_level=9)
            label = "png-rgba8"
        else:
            image.save(buffer, format="WEBP", lossless=True, exact=True, method=6)
            label = "webp-lossless-rgba8"
        encoded = buffer.getvalue()
        roundtrip(Image, encoded, dimensions, pixels, relative.as_posix())
        sizes[label] = len(encoded)
        if len(encoded) < len(chosen):
            chosen, storage = encoded, label
    # Exclusive creation protects against overwriting a concurrently added file.
    with target.open("xb") as stream:
        stream.write(chosen)
    stored = target.read_bytes()
    if stored != chosen:
        fail(f"Stored bytes differ after write: {target}")
    decoded = (stored[12:] if storage == "rgtx" else
               roundtrip(Image, stored, dimensions, pixels, str(target)))
    return {
        "path": relative.as_posix(), "format": storage,
        "width": dimensions[0], "height": dimensions[1],
        "source_bytes": len(raw), "stored_bytes": len(stored),
        "decoded_bytes": len(decoded), "source_rgba_bytes": len(pixels),
        "source_sha256": sha256(raw), "stored_sha256": sha256(stored),
        "source_rgba_sha256": sha256(pixels),
        "decoded_rgba_sha256": sha256(decoded), "candidate_bytes": sizes,
        "skipped_candidates": skipped,
    }


def copy_config(path, target, relative):
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as incoming, target.open("xb") as outgoing:
        while chunk := incoming.read(1024 * 1024):
            outgoing.write(chunk)
            digest.update(chunk)
            count += len(chunk)
    with target.open("rb") as stored:
        stored_hash = hashlib.file_digest(stored, "sha256").hexdigest()
    if stored_hash != digest.hexdigest() or target.stat().st_size != count:
        fail(f"Copied configuration differs: {relative}")
    return {"path": relative.as_posix(), "format": "unchanged",
            "source_bytes": count, "stored_bytes": count,
            "source_sha256": digest.hexdigest(), "stored_sha256": stored_hash,
            "source_rgba_bytes": None, "decoded_bytes": None,
            "source_rgba_sha256": None, "decoded_rgba_sha256": None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Immutable raw RGTX pack")
    parser.add_argument("--output", required=True, help="New, non-existing directory")
    parser.add_argument("--report", required=True, help="New JSON path outside both packs")
    parser.add_argument("--codec", choices=("png", "webp", "best"), default="best")
    args = parser.parse_args()
    try:
        from PIL import Image, features, __version__ as pillow_version
    except ImportError:
        parser.exit(2, "error: Pillow is required; install it with: python -m pip install Pillow\n")
    if args.codec in ("webp", "best") and not features.check("webp"):
        parser.exit(2, "error: This Pillow build lacks WebP support; use a WebP-enabled build or --codec png\n")
    start = time.perf_counter()
    try:
        source, output, report = prepare_paths(args)
        files, directories = inventory(source)
        # Reject compressed/mislabeled legacy texture inputs before creating output.
        for relative in files:
            path = source / relative
            with path.open("rb") as stream:
                magic = stream.read(4)
            if path.suffix.lower() in TEXTURE_SUFFIXES and magic != b"RGTX":
                fail(f"Input already encoded or malformed; use the raw RGTX pack: {path}")
        output.mkdir(parents=True, exist_ok=False)
        for directory in directories:
            (output / directory).mkdir(parents=True, exist_ok=False)
        entries = []
        for relative in files:
            path, target = source / relative, output / relative
            reject_link_ancestors(path)
            before = path.stat()
            with path.open("rb") as stream:
                texture = stream.read(4) == b"RGTX"
            entry = (convert_texture(Image, path, target, relative, args.codec)
                     if texture else copy_config(path, target, relative))
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size, after.st_mtime_ns, after.st_ino):
                fail(f"Source changed during conversion: {path}")
            entries.append(entry)
        if inventory(source) != (files, directories):
            fail("Source inventory changed during conversion")
        result = {
            "schema": 1, "complete": True, "validation": "Pillow-only RGBA byte equality",
            "source": str(source), "output": str(output), "codec": args.codec,
            "pillow_version": pillow_version, "files": len(entries),
            "directories": [p.as_posix() for p in directories],
            "textures": sum(e["format"] != "unchanged" for e in entries),
            "source_bytes": sum(e["source_bytes"] for e in entries),
            "stored_bytes": sum(e["stored_bytes"] for e in entries),
            "seconds": round(time.perf_counter() - start, 3), "entries": entries,
        }
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps({k: v for k, v in result.items()
                          if k not in ("entries", "directories")}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}\nSource was not written. Any partial output is retained; inspect it and retry with fresh output/report paths.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
