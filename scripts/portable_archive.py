#!/usr/bin/env python3
"""Build/verify a portable ZIP from an explicit, quiescent release stage. Stdlib only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unicodedata
import zipfile
import zlib
from datetime import datetime, timezone

SCHEMA = "portable-release-manifest/v1"
CHUNK = 1024 * 1024
BLOCKED = {
    "userdata", ".refract", "logs", "captures", ".git", ".hg", ".svn",
    "node_modules", ".venv", "venv", "build", "builds", "toolchain",
    "toolchains", "tools", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".cache", "cmakefiles",
}
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10))}


class ValidationError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def safe_name(name):
    require(isinstance(name, str) and name, "Empty/non-string archive path")
    require("\\" not in name and not name.startswith("/"), f"Unsafe path: {name!r}")
    parts = name.split("/")
    for part in parts:
        require(part not in {"", ".", ".."}, f"Unsafe path: {name!r}")
        require(not any(ord(c) < 32 or c in '<>:"|?*' for c in part),
                f"Not a portable Windows path: {name!r}")
        require(part == part.rstrip(" ."), f"Trailing dot/space: {name!r}")
        key = part.casefold()
        require(key.split(".")[0] not in RESERVED, f"Reserved Windows path: {name!r}")
        require(key not in BLOCKED and not key.startswith("cmake-build-"),
                f"Private/development path is forbidden in a release: {name!r}")
    return unicodedata.normalize("NFC", name).casefold()


def no_link(path):
    info = path.lstat()
    require(not stat.S_ISLNK(info.st_mode), f"Symlink forbidden: {path}")
    require(not (getattr(info, "st_file_attributes", 0) & 0x400),
            f"Windows reparse point/junction forbidden: {path}")
    return info


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(stage):
    require(stage.is_dir(), f"Stage must be an existing directory: {stage}")
    no_link(stage)
    result, seen = [], set()
    for folder, dirs, files in os.walk(stage, followlinks=False):
        dirs.sort()
        files.sort()
        for name in dirs + files:
            path = Path(folder) / name
            info = no_link(path)
            relative = path.relative_to(stage).as_posix()
            key = safe_name(relative)
            require(key not in seen, f"Duplicate/case/Unicode path collision: {relative}")
            seen.add(key)
            if stat.S_ISDIR(info.st_mode):
                continue
            require(stat.S_ISREG(info.st_mode), f"Non-regular file forbidden: {path}")
            digest = sha256_file(path)
            after = path.stat()
            require((info.st_size, info.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
                    f"Stage changed while hashing: {relative}")
            result.append({"path": relative, "bytes": info.st_size, "sha256": digest})
    require(result, "Stage contains no files")
    return sorted(result, key=lambda item: item["path"])


def load_manifest(path):
    with path.open("r", encoding="utf-8") as source:
        return json.load(source)


def validate_manifest(manifest):
    require(isinstance(manifest, dict) and manifest.get("schema") == SCHEMA,
            "Unsupported/missing manifest schema")
    files = manifest.get("files")
    require(isinstance(files, list) and files, "Manifest needs nonempty files list")
    expected, seen = {}, set()
    for item in files:
        require(isinstance(item, dict), "Malformed manifest file item")
        name = item.get("path")
        key = safe_name(name)
        require(key not in seen, f"Duplicate/colliding manifest path: {name}")
        seen.add(key)
        require(type(item.get("bytes")) is int and item["bytes"] >= 0,
                f"Invalid byte count for {name}")
        require(isinstance(item.get("sha256"), str) and
                re.fullmatch(r"[a-f0-9]{64}", item["sha256"]), f"Invalid hash for {name}")
        expected[name] = item
    for key in seen:
        parts = key.split("/")
        require(not any("/".join(parts[:i]) in seen for i in range(1, len(parts))),
                f"File/ancestor path conflict in manifest: {key}")
    require(manifest.get("file_count") == len(files), "Manifest file count mismatch")
    require(manifest.get("uncompressed_bytes") == sum(i["bytes"] for i in files),
            "Manifest total bytes mismatch")
    return expected


def verify_archive(archive, manifest, check_container=True):
    expected = validate_manifest(manifest)
    if check_container:
        container = manifest.get("archive")
        require(isinstance(container, dict), "Missing archive metadata")
        require(archive.stat().st_size == container.get("bytes"), "Archive size mismatch")
        require(sha256_file(archive) == container.get("sha256"), "Archive SHA-256 mismatch")
    with zipfile.ZipFile(archive, "r") as bundle:
        entries = bundle.infolist()
        require(len(entries) == len(expected), "Archive entry count mismatch")
        actual, seen = {}, set()
        for entry in entries:
            key = safe_name(entry.filename)
            require(key not in seen, f"Duplicate/colliding ZIP entry: {entry.filename}")
            seen.add(key)
            require(not entry.is_dir(), f"Unexpected directory ZIP entry: {entry.filename}")
            mode = (entry.external_attr >> 16) & 0xFFFF
            require(not stat.S_ISLNK(mode), f"Symlink ZIP entry forbidden: {entry.filename}")
            require(not (entry.flag_bits & 1), "Encrypted ZIP entries are unsupported")
            actual[entry.filename] = entry
        require(set(actual) == set(expected), "Archive path inventory mismatch")
        for name, item in expected.items():
            require(actual[name].file_size == item["bytes"], f"Length mismatch: {name}")
            digest, count = hashlib.sha256(), 0
            # Reading to EOF makes zipfile check the complete entry CRC, including empty files.
            with bundle.open(actual[name], "r") as source:
                for chunk in iter(lambda: source.read(CHUNK), b""):
                    digest.update(chunk)
                    count += len(chunk)
            require(count == item["bytes"], f"Read length mismatch: {name}")
            require(digest.hexdigest() == item["sha256"], f"Content SHA-256 mismatch: {name}")
    return {"status": "passed", "file_count": len(expected),
            "uncompressed_bytes": sum(i["bytes"] for i in expected.values()),
            "exact_inventory": True, "full_crc": True, "all_file_sha256": True,
            "archive_sha256": sha256_file(archive), "archive_bytes": archive.stat().st_size,
            "runtime_test": "not_performed"}


def outside_stage(path, stage):
    try:
        path.resolve().relative_to(stage.resolve())
    except ValueError:
        return
    raise ValidationError(f"Archive/manifest must be outside stage: {path}")


def temp_file(parent, suffix):
    fd, name = tempfile.mkstemp(prefix=".portable-", suffix=suffix, dir=parent)
    os.close(fd)
    return Path(name)


def write_json(path, data):
    with path.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(data, target, ensure_ascii=False, indent=2)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())


def publish_pair(temp_archive, archive, temp_manifest, manifest):
    # Exclusive placeholders prevent replacing a previous deliverable or a concurrent build.
    owned = []
    try:
        for destination in (archive, manifest):
            with destination.open("xb"):
                pass
            owned.append(destination)
        os.replace(temp_archive, archive)
        os.replace(temp_manifest, manifest)
    except BaseException:
        for destination in owned:
            destination.unlink(missing_ok=True)
        raise


def build(args):
    # Keep the stage path un-resolved until the root symlink check has run.
    stage = Path(os.path.abspath(args.stage))
    archive, manifest_path = Path(args.archive).absolute(), Path(args.manifest).absolute()
    require(archive.resolve() != manifest_path.resolve(), "Archive and manifest must differ")
    for output in (archive, manifest_path):
        outside_stage(output, stage)
        require(not output.exists() and not output.is_symlink(), f"Refusing overwrite: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
    prefix = stage.name
    safe_name(prefix)
    files = inventory(stage)
    manifest = {
        "schema": SCHEMA, "created_utc": datetime.now(timezone.utc).isoformat(),
        "archive": {}, "file_count": len(files),
        "uncompressed_bytes": sum(item["bytes"] for item in files),
        "files": [{**item, "path": prefix + "/" + item["path"]} for item in files],
    }
    temp_archive = temp_file(archive.parent, ".zip.tmp")
    temp_manifest = temp_file(manifest_path.parent, ".json.tmp")
    try:
        with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=args.compression_level, allowZip64=True) as bundle:
            for item in files:
                path = stage / item["path"]
                no_link(path)
                digest, count = hashlib.sha256(), 0
                info = zipfile.ZipInfo.from_file(path, prefix + "/" + item["path"])
                info.compress_type = zipfile.ZIP_DEFLATED
                info._compresslevel = args.compression_level
                with path.open("rb") as source, bundle.open(info, "w", force_zip64=True) as target:
                    for chunk in iter(lambda: source.read(CHUNK), b""):
                        digest.update(chunk)
                        count += len(chunk)
                        target.write(chunk)
                require(count == item["bytes"] and digest.hexdigest() == item["sha256"],
                        f"Stage changed during archive write: {item['path']}")
        require(inventory(stage) == files, "Stage inventory/content changed during build")
        report = verify_archive(temp_archive, manifest, check_container=False)
        manifest["archive"] = {"filename": archive.name, "bytes": report["archive_bytes"],
                               "sha256": report["archive_sha256"]}
        write_json(temp_manifest, manifest)
        publish_pair(temp_archive, archive, temp_manifest, manifest_path)
        return {**report, "archive": str(archive), "manifest": str(manifest_path)}
    finally:
        temp_archive.unlink(missing_ok=True)
        temp_manifest.unlink(missing_ok=True)


def verify(args):
    archive, manifest_path = Path(args.archive), Path(args.manifest)
    report_path = Path(args.report).absolute() if args.report else None
    if report_path:
        require(report_path.resolve() not in {archive.resolve(), manifest_path.resolve()},
                "Report must differ from archive/manifest")
        require(not report_path.exists() and not report_path.is_symlink(),
                f"Refusing report overwrite: {report_path}")
        report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = verify_archive(archive, load_manifest(manifest_path))
        report.update({"archive": str(archive.absolute()), "manifest": str(manifest_path.absolute())})
    except (ValidationError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile,
            RuntimeError, NotImplementedError, EOFError, zlib.error) as error:
        report = {"status": "failed", "error": str(error), "runtime_test": "not_performed"}
    if report_path:
        # A verification report also never silently replaces earlier evidence.
        with report_path.open("x", encoding="utf-8", newline="\n") as target:
            json.dump(report, target, ensure_ascii=False, indent=2)
            target.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="Inventory a pristine stage, build, fully verify, publish")
    build_parser.add_argument("--stage", required=True, type=Path)
    build_parser.add_argument("--archive", required=True, type=Path)
    build_parser.add_argument("--manifest", required=True, type=Path)
    build_parser.add_argument("--compression-level", type=int, choices=range(10), default=6)
    verify_parser = sub.add_parser("verify", help="Verify ZIP without extracting or running it")
    verify_parser.add_argument("--archive", required=True, type=Path)
    verify_parser.add_argument("--manifest", required=True, type=Path)
    verify_parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = build(args) if args.command == "build" else verify(args)
    except (ValidationError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile,
            RuntimeError, NotImplementedError, EOFError, zlib.error) as error:
        result = {"status": "failed", "error": str(error), "runtime_test": "not_performed"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
