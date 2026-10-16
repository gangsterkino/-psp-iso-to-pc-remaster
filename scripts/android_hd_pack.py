#!/usr/bin/env python3
"""Build or verify an Android HD resource ZIP and its pinned manifest.

Python 3.11+, standard library only. This preserves an existing clean texture
stage byte-for-byte; it does not convert ISOs, build APKs, or download anything.
The manifest matches HdPackInstaller's version/archive_sha256/size/
unpacked_bytes/files schema. Keep the stage quiescent during a build.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tempfile
import zipfile
import zlib

CHUNK = 1024 * 1024
MAX_FILES = 4096
MAX_FILE = 256 * 1024 * 1024
MAX_ARCHIVE = 3 * 1024 * 1024 * 1024
MAX_UNPACKED = 6 * 1024 * 1024 * 1024
MAX_MANIFEST = 2 * 1024 * 1024
MARKER = ".installed.json"
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
WINDOWS_DEVICES = {"con", "prn", "aux", "nul",
                   *(f"com{i}" for i in range(1, 10)),
                   *(f"lpt{i}" for i in range(1, 10))}


class ValidationError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def valid_name(name):
    require(isinstance(name, str) and 0 < len(name) <= 240,
            "Resource paths must be 1..240 ASCII characters")
    require("\\" not in name and not name.startswith("/"), f"Unsafe path: {name!r}")
    for part in name.split("/"):
        require(re.fullmatch(r"[A-Za-z0-9_.-]+", part) is not None and
                part not in {".", ".."} and not part.endswith("."),
                f"Unsafe/non-ASCII path: {name!r}")
        require(part.casefold() != MARKER and
                part.casefold().split(".")[0] not in WINDOWS_DEVICES,
                f"Reserved path: {name!r}")
    return name.casefold()


def validate_path_set(names):
    files, components = set(), {}
    for name in names:
        key = valid_name(name)
        require(key not in files, f"Duplicate/case-colliding resource: {name}")
        files.add(key)
        pieces = name.split("/")
        for index in range(1, len(pieces) + 1):
            prefix = "/".join(pieces[:index])
            require(components.setdefault(prefix.casefold(), prefix) == prefix,
                    f"Case-colliding directory path: {name}")
    for name in names:
        parts = name.casefold().split("/")
        require(not any("/".join(parts[:index]) in files
                        for index in range(1, len(parts))),
                f"File/directory prefix conflict: {name}")


def no_link(path):
    info = path.lstat()
    require(not stat.S_ISLNK(info.st_mode) and
            not (getattr(info, "st_file_attributes", 0) & REPARSE),
            f"Symlink/junction/reparse point forbidden: {path}")
    return info


def no_link_ancestors(path):
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate):
            no_link(candidate)


def absolute(path):
    return Path(os.path.abspath(path))


def signature(info):
    # Windows Python versions can report creation time for lstat.st_ctime but
    # change time for fstat.st_ctime. Identity, mode, size and mtime are stable;
    # streamed SHA256 checks additionally pin the contents.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns)


@contextmanager
def regular_reader(path, expected=None):
    no_link_ancestors(path)
    before = no_link(path)
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
            f"Only ordinary, non-hardlinked files are allowed: {path}")
    if expected is not None:
        require(signature(before) == expected, f"Source changed before reading: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) |
                         getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        require(signature(os.fstat(stream.fileno())) == signature(before),
                f"File changed while opening: {path}")
        yield stream, before
        require(signature(os.fstat(stream.fileno())) == signature(before) and
                signature(no_link(path)) == signature(before),
                f"File changed while reading: {path}")


def stream_hash(path, limit=MAX_ARCHIVE, expected=None):
    digest, total = hashlib.sha256(), 0
    with regular_reader(path, expected) as (source, info):
        require(info.st_size <= limit, f"File exceeds size limit: {path}")
        for block in iter(lambda: source.read(CHUNK), b""):
            total += len(block)
            require(total <= limit and total <= info.st_size, f"File grew while reading: {path}")
            digest.update(block)
        require(total == info.st_size, f"File truncated while reading: {path}")
    return total, digest.hexdigest()


@dataclass(frozen=True)
class Record:
    path: str
    size: int
    sha256: str
    stamp: tuple

    def manifest_item(self):
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


def inventory(source):
    source = absolute(source)
    no_link_ancestors(source)
    require(stat.S_ISDIR(no_link(source).st_mode), f"Source is not a directory: {source}")
    pending, result, seen, total = [source], [], {}, 0
    while pending:
        folder = pending.pop()
        no_link_ancestors(folder)
        with os.scandir(folder) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(source).as_posix()
            key = valid_name(relative)
            require(key not in seen, f"Duplicate/case-colliding path: {relative}")
            seen[key] = relative
            info = no_link(path)
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
                continue
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                    f"Non-regular or linked source file: {relative}")
            require(len(result) < MAX_FILES, f"More than {MAX_FILES} files")
            size, digest = stream_hash(path, MAX_FILE, signature(info))
            total += size
            require(total <= MAX_UNPACKED, "Unpacked data exceeds 6 GiB")
            result.append(Record(relative, size, digest, signature(info)))
    result.sort(key=lambda record: record.path)
    require(result and total > 0, "Stage must contain nonempty resource data")
    validate_path_set([record.path for record in result])
    return result


def integer(value, low, high, label):
    require(type(value) is int and low <= value <= high, f"Invalid {label}: {value!r}")


def valid_hash(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value),
            "SHA256 must be 64 lowercase hexadecimal characters")


def validate_manifest(manifest):
    require(type(manifest) is dict and set(manifest) ==
            {"version", "archive_sha256", "size", "unpacked_bytes", "files"},
            "Manifest keys do not match the Android HD schema")
    require(isinstance(manifest["version"], str) and
            re.fullmatch(r"[A-Za-z0-9._-]{1,100}", manifest["version"]), "Invalid version")
    valid_hash(manifest["archive_sha256"])
    integer(manifest["size"], 1, MAX_ARCHIVE, "archive size")
    integer(manifest["unpacked_bytes"], 1, MAX_UNPACKED, "unpacked bytes")
    items = manifest["files"]
    require(type(items) is list and 1 <= len(items) <= MAX_FILES, "Invalid file count")
    total, names = 0, []
    for item in items:
        require(type(item) is dict and set(item) == {"path", "size", "sha256"},
                "Invalid file manifest item")
        valid_name(item["path"])
        integer(item["size"], 0, MAX_FILE, "file size")
        valid_hash(item["sha256"])
        total += item["size"]
        names.append(item["path"])
    validate_path_set(names)
    require(total == manifest["unpacked_bytes"], "Unpacked byte total does not match files")
    return {item["path"]: item for item in items}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_manifest(path):
    with regular_reader(absolute(path)) as (source, info):
        require(info.st_size <= MAX_MANIFEST, "Manifest exceeds 2 MiB")
        data = source.read(MAX_MANIFEST + 1)
    require(len(data) <= MAX_MANIFEST, "Manifest exceeds 2 MiB")
    result = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    validate_manifest(result)
    return result


def check_classic_directory(stream, size, count):
    stream.seek(max(0, size - 65557))
    tail = stream.read(65557)
    end = -1
    for index in range(len(tail) - 22, -1, -1):
        if tail[index:index + 4] == b"PK\x05\x06" and \
                index + 22 + struct.unpack_from("<H", tail, index + 20)[0] == len(tail):
            end = index
            break
    require(end >= 0, "Missing/truncated ZIP end record")
    _, disk, cd_disk, disk_count, total_count, cd_size, cd_offset, _ = \
        struct.unpack_from("<4s4H2IH", tail, end)
    end_offset = size - len(tail) + end
    require(disk == cd_disk == 0 and disk_count == total_count == count,
            "Multidisk/ZIP64 or incorrect ZIP directory count")
    require(cd_offset != 0xffffffff and cd_size != 0xffffffff and
            cd_offset + cd_size == end_offset, "ZIP64/concatenated ZIP layout is not supported")
    stream.seek(0)


def verify_archive(archive, manifest, source=None):
    expected = validate_manifest(manifest)
    archive = absolute(archive)
    size, archive_hash = stream_hash(archive)
    require(size == manifest["size"] and archive_hash == manifest["archive_sha256"],
            "Archive size/SHA256 does not match manifest")
    seen, unpacked = set(), 0
    with regular_reader(archive) as (raw, before):
        check_classic_directory(raw, size, len(expected))
        with zipfile.ZipFile(raw) as bundle:
            entries = bundle.infolist()
            require(len(entries) == len(expected), "ZIP has extra or missing entries")
            for entry in entries:
                name = entry.filename
                valid_name(name)
                require(entry.orig_filename == name and name in expected and name not in seen,
                        f"Unexpected/duplicate ZIP member: {name!r}")
                seen.add(name)
                mode = entry.external_attr >> 16
                require(not entry.is_dir() and (entry.external_attr & 0x410) == 0 and
                        entry.create_system in {0, 3} and
                        (entry.create_system != 3 or stat.S_IFMT(mode) in {0, stat.S_IFREG}),
                        f"ZIP link/directory/device metadata forbidden: {name}")
                require(entry.compress_type == zipfile.ZIP_STORED and entry.flag_bits & 1 == 0,
                        f"Only unencrypted ZIP_STORED resources are supported: {name}")
                item = expected[name]
                require(entry.file_size == item["size"] and entry.compress_size == item["size"],
                        f"ZIP member size mismatch: {name}")
                digest, crc, count = hashlib.sha256(), 0, 0
                with bundle.open(entry) as member:
                    for block in iter(lambda: member.read(CHUNK), b""):
                        count += len(block)
                        unpacked += len(block)
                        require(count <= item["size"] and unpacked <= manifest["unpacked_bytes"],
                                f"ZIP member exceeds quota: {name}")
                        digest.update(block)
                        crc = zlib.crc32(block, crc)
                require(count == item["size"] and digest.hexdigest() == item["sha256"] and
                        (crc & 0xffffffff) == entry.CRC, f"ZIP member SHA256/CRC mismatch: {name}")
    require(seen == set(expected) and unpacked == manifest["unpacked_bytes"],
            "ZIP member set/total does not match manifest")
    if source is not None:
        current = {record.path: record.manifest_item() for record in inventory(source)}
        require(current == expected, "Source stage bytes/paths differ from the archive")
    return {"status": "passed", "version": manifest["version"], "files": len(expected),
            "archive_bytes": size, "unpacked_bytes": unpacked, "archive_sha256": archive_hash,
            "crc_verified": True, "source_verified": source is not None,
            "runtime_test": "not_performed"}


def prepare_outputs(source, archive, manifest):
    source, archive, manifest = map(absolute, (source, archive, manifest))
    for path in (source, archive, manifest):
        no_link_ancestors(path)
    require(archive != manifest and os.path.normcase(str(archive)) != os.path.normcase(str(manifest)),
            "Archive and manifest must be different paths")
    for output in (archive, manifest):
        require(output != source and source not in output.parents,
                f"Output must be outside the source stage: {output}")
        require(not os.path.lexists(output), f"Refusing to overwrite: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        no_link_ancestors(output.parent)
        require(source.resolve() not in output.parent.resolve().parents and
                source.resolve() != output.parent.resolve(), "Output parent resolves inside source")
    return source, archive, manifest


def temporary(parent, suffix):
    descriptor, name = tempfile.mkstemp(prefix=".android-hd-", suffix=suffix, dir=parent)
    os.close(descriptor)
    return Path(name)


def publish_pair(temporary_archive, archive, temporary_manifest, manifest):
    owned = []
    try:
        # Reserve both names exclusively before replacing our own placeholders.
        # A failed second reservation never removes someone else's destination.
        for path in (archive, manifest):
            with path.open("xb"):
                pass
            owned.append(path)
        os.replace(temporary_archive, archive)
        os.replace(temporary_manifest, manifest)
    except BaseException:
        for path in owned:
            path.unlink(missing_ok=True)
        raise


def build(args):
    source, archive, manifest_path = prepare_outputs(args.source, args.archive, args.manifest)
    require(re.fullmatch(r"[A-Za-z0-9._-]{1,100}", args.version), "Invalid version")
    records = inventory(source)
    temporary_archive, temporary_manifest = None, None
    try:
        temporary_archive = temporary(archive.parent, ".zip.tmp")
        temporary_manifest = temporary(manifest_path.parent, ".json.tmp")
        # Python's conservative 2-GiB ZIP64 threshold is below classic ZIP's
        # unsigned 32-bit field limit. Our 3-GiB cap and 256-MiB members fit
        # classic ZIP and the Android installer explicitly rejects ZIP64 EOCD.
        previous_limit = zipfile.ZIP64_LIMIT
        try:
            zipfile.ZIP64_LIMIT = 0xffffffff
            with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_STORED,
                                 allowZip64=False) as bundle:
                for record in records:
                    info = zipfile.ZipInfo(record.path, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    info.compress_type = zipfile.ZIP_STORED
                    info.file_size = record.size
                    digest, total = hashlib.sha256(), 0
                    with regular_reader(source / record.path, record.stamp) as (input_file, _), \
                            bundle.open(info, "w", force_zip64=False) as member:
                        for block in iter(lambda: input_file.read(CHUNK), b""):
                            total += len(block)
                            require(total <= record.size, f"Source grew during packaging: {record.path}")
                            member.write(block)
                            digest.update(block)
                    require(total == record.size and digest.hexdigest() == record.sha256,
                            f"Source changed during packaging: {record.path}")
                    require(bundle.fp.tell() <= MAX_ARCHIVE, "ZIP exceeds 3 GiB")
        finally:
            zipfile.ZIP64_LIMIT = previous_limit
        size, archive_hash = stream_hash(temporary_archive)
        manifest = {"version": args.version, "archive_sha256": archive_hash, "size": size,
                    "unpacked_bytes": sum(record.size for record in records),
                    "files": [record.manifest_item() for record in records]}
        report = verify_archive(temporary_archive, manifest)
        require(inventory(source) == records, "Source stage changed during build")
        encoded = (json.dumps(manifest, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        require(len(encoded) <= MAX_MANIFEST, "Manifest exceeds 2 MiB")
        with temporary_manifest.open("wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        # Windows _commit/fsync requires a writable descriptor.
        with temporary_archive.open("r+b") as output:
            os.fsync(output.fileno())
        publish_pair(temporary_archive, archive, temporary_manifest, manifest_path)
        report.update({"archive": str(archive), "manifest": str(manifest_path), "source_verified": True})
        return report
    finally:
        for path in (temporary_archive, temporary_manifest):
            if path is not None:
                path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("build", help="Build a fresh, verified ZIP_STORED resource pack")
    create.add_argument("--source", required=True, type=Path)
    create.add_argument("--archive", required=True, type=Path)
    create.add_argument("--manifest", required=True, type=Path)
    create.add_argument("--version", required=True)
    check = commands.add_parser("verify", help="Verify a ZIP and manifest without extraction or execution")
    check.add_argument("--archive", required=True, type=Path)
    check.add_argument("--manifest", required=True, type=Path)
    check.add_argument("--source", type=Path, help="Also require byte-for-byte matching with this stage")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build(args)
        else:
            result = verify_archive(args.archive, load_manifest(args.manifest), args.source)
    except (ValidationError, OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile,
            zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as error:
        result = {"status": "failed", "error": str(error), "runtime_test": "not_performed"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
