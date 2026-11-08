#!/usr/bin/env python3
"""
Launcher release generator (with compression)
=============================================

What it does
------------
1. Scans your game folder (all subfolders, any file type).
2. Hashes every file (SHA-256).
3. Gives every file a gibberish name made from your SEED + the file's hash.
   - Same seed + same file contents -> the same gibberish name every time.
   - Changed file                   -> a new gibberish name (on purpose).
   - Identical files                -> share one blob (uploaded once).
4. COMPRESSES each new file with LZMA/XZ (if that actually makes it smaller) and puts the
   result in <output>/blobs/. Compressed files end in .xz, files that don't shrink stay as
   they are (.bin).
5. Writes <output>/manifest.json (+ a version-named copy + versions.json) with everything
   the launcher needs: real path, blob name, link, sizes, hash, compression type.

Usage (Windows example)
-----------------------
python generate_release.py --input "C:\\Games\\MyModdedGame" --output "C:\\release" ^
    --seed "my-secret-seed" --version 1.0.0 ^
    --base-url "https://github.com/USER/REPO/releases/download/1.0.0"

For later versions add:
    --previous "path\\to\\manifest-base.json"   (only new/changed files get made and uploaded)
    --versions-file "path\\to\\versions.json"   (adds the new version to the list)

Other options:
    --no-compress     keep every file as it is (bigger uploads and downloads)
    --level 1-9       compression strength (9 = smallest but slowest, default 9).
                      Level 9 needs about 700 MB of free RAM while compressing; use a lower
                      level (like 6) if your PC struggles.
    --method xz|zlib  xz (LZMA) compresses best and is the default. zlib is the older, faster,
                      weaker method.

Keep your seed safe and use the SAME seed every release.
Needs Python 3.8 or newer. No extra packages.
"""

import argparse
import base64
import fnmatch
import hashlib
import hmac
import json
import lzma
import os
import shutil
import sys
import zlib
from datetime import datetime, timezone

CHUNK = 1024 * 1024
DEFAULT_EXCLUDES = [".DS_Store", "Thumbs.db", "desktop.ini"]
RELEASE_ASSET_WARN = 900  # GitHub caps assets per release; check the current limit
COMPRESSED_EXT = {"xz": ".xz", "zlib": ".z"}
MIN_SAVING = 0.95  # keep the compressed version only if it is under 95% of the original


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def make_blob_stem(seed, digest, length):
    """Seeded gibberish name (without extension): HMAC(seed, file hash) -> letters/digits."""
    mac = hmac.new(seed.encode("utf-8"), digest.encode("ascii"), hashlib.sha256).digest()
    text = base64.b32encode(mac).decode("ascii").lower().rstrip("=")
    return text[:length]


def stem_of(blob_name):
    return blob_name.split(".")[0]


def compress_file(src, dst, method, level):
    if method == "xz":
        c = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=level)
    else:
        c = zlib.compressobj(level)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            chunk = fi.read(CHUNK)
            if not chunk:
                break
            fo.write(c.compress(chunk))
        fo.write(c.flush())


def make_new_blob(full, size, stem, args, blobs_dir):
    """Create the blob file for one new file. Returns (blob name, compression, stored size)."""
    if not args.no_compress:
        zname = stem + COMPRESSED_EXT[args.method]
        zpath = os.path.join(blobs_dir, zname)
        compress_file(full, zpath, args.method, args.level)
        zsize = os.path.getsize(zpath)
        if zsize < size * MIN_SAVING:
            return zname, args.method, zsize
        os.remove(zpath)  # not worth it, store the original
    rname = stem + args.ext
    shutil.copyfile(full, os.path.join(blobs_dir, rname))
    return rname, "none", size


def is_excluded(rel_path, name, patterns):
    for pat in patterns:
        if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


def scan(input_dir, excludes):
    files = []
    empty_dirs = []
    for root, dirs, names in os.walk(input_dir):
        dirs.sort()
        if not dirs and not names and root != input_dir:
            empty_dirs.append(os.path.relpath(root, input_dir).replace(os.sep, "/"))
        for name in sorted(names):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, input_dir).replace(os.sep, "/")
            if is_excluded(rel, name, excludes):
                continue
            files.append((rel, full))
    return files, empty_dirs


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def main():
    p = argparse.ArgumentParser(description="Generate compressed blobs and a manifest for the launcher.")
    p.add_argument("--input", required=True, help="Game folder to scan")
    p.add_argument("--output", required=True, help="Folder to write blobs/ and manifest.json into")
    p.add_argument("--seed", required=True, help="Secret seed text. Reuse the same one every release.")
    p.add_argument("--version", required=True, help="Version label, e.g. 1.0.0")
    p.add_argument("--base-url", default="", help="Where this release's blobs will be downloadable from")
    p.add_argument("--previous", default="", help="Previous manifest.json (for incremental updates)")
    p.add_argument("--versions-file", default="",
                   help="Your existing versions.json (so the new version gets added to the list)")
    p.add_argument("--exclude", action="append", default=[],
                   help="Pattern to skip, e.g. *.log or logs/* (can be used several times)")
    p.add_argument("--name-length", type=int, default=20, help="Length of gibberish names (default 20)")
    p.add_argument("--ext", default=".bin",
                   help='Extension for files stored uncompressed (default ".bin")')
    p.add_argument("--no-compress", action="store_true", help="Do not compress files")
    p.add_argument("--level", type=int, default=9, help="Compression strength 1-9 (default 9)")
    p.add_argument("--method", choices=["xz", "zlib"], default="xz",
                   help="Compression method (default xz, which is LZMA)")
    args = p.parse_args()

    input_dir = os.path.abspath(args.input)
    output_dir = os.path.abspath(args.output)
    blobs_dir = os.path.join(output_dir, "blobs")

    if not os.path.isdir(input_dir):
        sys.exit(f"Input folder not found: {input_dir}")
    if output_dir == input_dir or output_dir.startswith(input_dir + os.sep):
        sys.exit("The output folder must not be inside the game folder.")
    if os.path.isdir(blobs_dir) and os.listdir(blobs_dir):
        sys.exit(f"{blobs_dir} is not empty. Use a fresh output folder so old files don't get mixed in.")
    if args.name_length < 12:
        sys.exit("--name-length must be at least 12.")
    if not 1 <= args.level <= 9:
        sys.exit("--level must be between 1 and 9.")

    # What the previous release already has online (so we don't recreate or re-upload it)
    prev_info = {}
    if args.previous:
        try:
            with open(args.previous, "r", encoding="utf-8-sig") as f:
                prev = json.load(f)
            for entry in prev.get("files", []):
                prev_info[stem_of(entry["blob"])] = {
                    "blob": entry["blob"],
                    "url": entry.get("url", ""),
                    "compression": entry.get("compression", "none"),
                    "download_size": entry.get("download_size", entry["size"]),
                }
        except Exception as e:
            sys.exit(f"Could not read previous manifest: {e}")

    excludes = DEFAULT_EXCLUDES + args.exclude
    files, empty_dirs = scan(input_dir, excludes)
    if not files:
        sys.exit("No files found in the input folder.")

    os.makedirs(blobs_dir, exist_ok=True)
    base = args.base_url.rstrip("/")

    # Pass 1: hash everything
    print(f"Scanning {len(files)} files...")
    infos = []  # (rel path, digest, size, stem)
    first_file = {}  # digest -> (full path, size, stem)
    stem_to_digest = {}
    total_size = 0
    for i, (rel, full) in enumerate(files, 1):
        digest = sha256_file(full)
        size = os.path.getsize(full)
        total_size += size
        stem = make_blob_stem(args.seed, digest, args.name_length)
        if stem in stem_to_digest and stem_to_digest[stem] != digest:
            sys.exit("Name collision (extremely unlikely). Re-run with a larger --name-length.")
        stem_to_digest[stem] = digest
        infos.append((rel, digest, size, stem))
        first_file.setdefault(digest, (full, size, stem))
        if i % 25 == 0 or i == len(files):
            print(f"  hashed {i}/{len(files)}")

    # Pass 2: make blobs for files the previous release doesn't already have
    records = {}  # digest -> blob info
    new_blobs = []  # (blob name, raw size, stored size)
    to_make = [
        d for d, (_, _, stem) in first_file.items()
        if not (stem in prev_info and prev_info[stem]["url"])
    ]
    print(f"{len(to_make)} new blob(s) to create ({len(first_file) - len(to_make)} already online)...")
    k = 0
    for digest, (full, size, stem) in first_file.items():
        prev_rec = prev_info.get(stem)
        if prev_rec and prev_rec["url"]:
            records[digest] = dict(prev_rec)
            continue
        k += 1
        verb = "copying" if args.no_compress else f"compressing ({args.method})"
        print(f"  [{k}/{len(to_make)}] {verb} {os.path.basename(full)} ({human(size)})")
        blob, comp, stored = make_new_blob(full, size, stem, args, blobs_dir)
        url = f"{base}/{blob}" if base else ""
        records[digest] = {"blob": blob, "url": url, "compression": comp, "download_size": stored}
        new_blobs.append((blob, size, stored))

    entries = []
    for rel, digest, size, stem in infos:
        rec = records[digest]
        entry = {
            "path": rel,
            "blob": rec["blob"],
            "size": size,
            "sha256": digest,
            "compression": rec["compression"],
            "download_size": rec["download_size"],
        }
        if rec["url"]:
            entry["url"] = rec["url"]
        entries.append(entry)

    fresh_download = sum(r["download_size"] for r in records.values())

    manifest = {
        "manifest_version": 2,
        "version": args.version,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "file_count": len(entries),
        "total_size": total_size,
        "download_total": fresh_download,
        "files": entries,
        "empty_dirs": sorted(empty_dirs),
    }
    if base:
        manifest["base_url"] = base

    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    safe_version = "".join(c if (c.isalnum() or c in ".-_") else "_" for c in args.version)
    versioned_name = f"manifest-{safe_version}.json"
    shutil.copyfile(manifest_path, os.path.join(output_dir, versioned_name))

    versions = []
    if args.versions_file:
        try:
            with open(args.versions_file, "r", encoding="utf-8-sig") as f:
                versions = json.load(f).get("versions", [])
        except Exception as e:
            sys.exit(f"Could not read versions file: {e}")
    versions = [v for v in versions if v.get("version") != args.version]
    versions.append({"version": args.version, "manifest": versioned_name, "date": manifest["generated_at"]})
    versions_path = os.path.join(output_dir, "versions.json")
    with open(versions_path, "w", encoding="utf-8") as f:
        json.dump({"latest": args.version, "versions": versions}, f, indent=2)

    raw_new = sum(r for _, r, _ in new_blobs)
    stored_new = sum(s for _, _, s in new_blobs)

    print()
    print("Done!")
    print(f"  Files in game:            {len(entries)} ({human(total_size)} on disk)")
    print(f"  Brand new player downloads: {human(fresh_download)}")
    print(f"  Blobs to upload:          {len(new_blobs)} ({human(stored_new)})  ->  {blobs_dir}")
    if new_blobs and not args.no_compress:
        saved = raw_new - stored_new
        pct = (saved / raw_new * 100) if raw_new else 0
        print(f"  Compression saved:        {human(saved)} ({pct:.0f}%), the same files would be {human(raw_new)} uncompressed")
    print(f"  Manifest (latest):        {manifest_path}")
    print(f"  Manifest (version):       {os.path.join(output_dir, versioned_name)}")
    print(f"  Version list:             {versions_path}")
    if not base:
        print("  Note: no --base-url given, so entries have no 'url'. The launcher must build links from 'blob'.")
    if len(new_blobs) > RELEASE_ASSET_WARN:
        print(f"  Warning: {len(new_blobs)} files to upload. GitHub limits how many files a single release can hold;")
        print("  check the current limit and split across several releases if needed.")
    print()
    print("Next: upload everything in blobs/ to your GitHub release.")
    print("Then put manifest.json, the manifest-<version>.json file and versions.json in your repo.")
    print("Next time, add --versions-file pointing at this versions.json so the list keeps growing.")


if __name__ == "__main__":
    main()
