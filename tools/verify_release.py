#!/usr/bin/env python3
"""
Release verifier / unpacker
===========================

What it does
------------
1. Reads a manifest (a web link or a file on your computer).
2. Gets every file listed in it, either by downloading it from the link in the
   manifest, or by copying it from a local blobs folder (uses NO internet data).
3. Checks each file's SHA-256 against the manifest.
4. Saves each file under its REAL name and folder inside --output.
5. Optionally compares the rebuilt game with your original folder (--original).

Files that are already in --output and already correct are skipped, so you can
run it again without downloading everything twice.

Examples (Windows)
------------------
Test with no internet data (copies from your local blobs folder):
python verify_release.py --manifest "G:\\AmongUsBlob\\Test\\manifest-19I.json" ^
    --local-blobs "G:\\AmongUsBlob\\Test\\blobs" ^
    --output "G:\\AmongUsBlob\\Unpacked" --original "G:\\AmongUsBlob\\Latest"

Test the real download links, but only the first 3 files:
python verify_release.py --manifest "G:\\AmongUsBlob\\Test\\manifest-19I.json" ^
    --output "G:\\AmongUsBlob\\Unpacked2" --limit 3

Needs Python 3.8 or newer. No extra packages.
"""

import argparse
import fnmatch
import hashlib
import json
import lzma
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zlib

CHUNK = 1024 * 1024
DEFAULT_EXCLUDES = [".DS_Store", "Thumbs.db", "desktop.ini"]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def make_request(url):
    return urllib.request.Request(url, headers={"User-Agent": "release-verifier/1.0"})


def load_manifest(source):
    if source.lower().startswith(("http://", "https://")):
        with urllib.request.urlopen(make_request(source), timeout=60) as r:
            data = r.read()
        return json.loads(data.decode("utf-8-sig"))
    with open(source, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def safe_target(root, rel_path):
    """Turn a manifest path into a real path, refusing anything that escapes the output folder."""
    parts = rel_path.replace("\\", "/").split("/")
    if not rel_path or any(p in ("", ".", "..") for p in parts) or ":" in parts[0]:
        return None
    target = os.path.abspath(os.path.join(root, *parts))
    if not target.startswith(os.path.abspath(root) + os.sep):
        return None
    return target


def download(url, dest, retries=3):
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(make_request(url), timeout=60) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, CHUNK)
            return
        except (urllib.error.URLError, OSError, TimeoutError):
            if attempt == retries:
                raise
            time.sleep(2 * attempt)


def decompress_into(src, dst, method):
    if method == "xz":
        d = lzma.LZMADecompressor()
    else:
        d = zlib.decompressobj()
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            chunk = fi.read(CHUNK)
            if not chunk:
                break
            fo.write(d.decompress(chunk))
        if method != "xz":
            fo.write(d.flush())


def is_excluded(rel, name, patterns):
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in patterns)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def main():
    p = argparse.ArgumentParser(description="Rebuild the game from a manifest and verify it.")
    p.add_argument("--manifest", required=True, help="Manifest link (https://...) or file path")
    p.add_argument("--output", required=True, help="Folder to rebuild the game into")
    p.add_argument("--original", default="", help="Your original game folder, to compare against")
    p.add_argument("--local-blobs", default="", help="Use blobs from this folder instead of downloading")
    p.add_argument("--limit", type=int, default=0, help="Only process the first N files (quick test)")
    p.add_argument("--exclude", action="append", default=[],
                   help="Pattern to ignore when looking for extra files in --original")
    args = p.parse_args()

    try:
        manifest = load_manifest(args.manifest)
    except Exception as e:
        sys.exit(f"Could not read the manifest: {e}")

    entries = manifest.get("files", [])
    if not entries:
        sys.exit("The manifest has no files.")
    base_url = manifest.get("base_url", "").rstrip("/")

    if args.limit > 0:
        entries = entries[: args.limit]

    output_dir = os.path.abspath(args.output)
    os.makedirs(output_dir, exist_ok=True)

    use_local = bool(args.local_blobs)
    total_bytes = sum(e["size"] for e in entries)
    print(f"Version: {manifest.get('version', '?')}   Files: {len(entries)}   Size: {human(total_bytes)}")
    print("Source: " + (f"local blobs folder ({args.local_blobs})" if use_local else "downloading from the links in the manifest"))
    print()

    failures = []
    done = skipped = 0

    for i, e in enumerate(entries, 1):
        rel = e["path"]
        target = safe_target(output_dir, rel)
        if target is None:
            print(f"[{i}/{len(entries)}] {rel} ... REFUSED (unsafe path)")
            failures.append((rel, "unsafe path in manifest"))
            continue

        # Already there and correct? Skip.
        if os.path.isfile(target) and os.path.getsize(target) == e["size"] and sha256_file(target) == e["sha256"]:
            print(f"[{i}/{len(entries)}] {rel} ... already ok")
            skipped += 1
            continue

        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".part"
        raw = target + ".dl"
        try:
            if use_local:
                src = os.path.join(args.local_blobs, e["blob"])
                if not os.path.isfile(src):
                    raise FileNotFoundError(f"blob not found in local folder: {e['blob']}")
                shutil.copyfile(src, raw)
            else:
                url = e.get("url") or (f"{base_url}/{e['blob']}" if base_url else "")
                if not url:
                    raise ValueError("no download link for this file")
                download(url, raw)

            if e.get("compression") in ("zlib", "xz"):
                decompress_into(raw, tmp, e["compression"])
                os.remove(raw)
            else:
                os.replace(raw, tmp)

            got = sha256_file(tmp)
            if got != e["sha256"]:
                raise ValueError("hash mismatch (file is corrupted or wrong)")
            os.replace(tmp, target)
            print(f"[{i}/{len(entries)}] {rel} ... ok")
            done += 1
        except Exception as ex:
            for leftover in (tmp, raw):
                if os.path.exists(leftover):
                    os.remove(leftover)
            print(f"[{i}/{len(entries)}] {rel} ... FAILED ({ex})")
            failures.append((rel, str(ex)))

    if args.limit == 0:
        for d in manifest.get("empty_dirs", []):
            t = safe_target(output_dir, d)
            if t:
                os.makedirs(t, exist_ok=True)

    # Compare with the original folder
    mismatched = []
    missing_in_original = []
    extra_in_original = []
    compared = 0
    if args.original:
        original_dir = os.path.abspath(args.original)
        if not os.path.isdir(original_dir):
            sys.exit(f"Original folder not found: {original_dir}")
        print()
        print("Comparing with the original folder...")
        failed_paths = {r for r, _ in failures}
        for e in entries:
            rel = e["path"]
            if rel in failed_paths:
                continue
            orig = os.path.join(original_dir, *rel.split("/"))
            if not os.path.isfile(orig):
                missing_in_original.append(rel)
                continue
            rebuilt = safe_target(output_dir, rel)
            compared += 1
            if sha256_file(orig) != sha256_file(rebuilt):
                mismatched.append(rel)

        if args.limit == 0:
            in_manifest = {e["path"] for e in entries}
            excludes = DEFAULT_EXCLUDES + args.exclude
            for root, dirs, names in os.walk(original_dir):
                for name in names:
                    rel = os.path.relpath(os.path.join(root, name), original_dir).replace(os.sep, "/")
                    if rel not in in_manifest and not is_excluded(rel, name, excludes):
                        extra_in_original.append(rel)

    # Summary
    print()
    print("========== RESULT ==========")
    print(f"Rebuilt now:        {done}")
    print(f"Already correct:    {skipped}")
    print(f"Failed:             {len(failures)}")
    for rel, why in failures:
        print(f"   - {rel}: {why}")
    if args.original:
        print(f"Compared to original: {compared}")
        print(f"Different from original: {len(mismatched)}")
        for rel in mismatched:
            print(f"   - {rel}")
        print(f"In manifest but missing from original: {len(missing_in_original)}")
        for rel in missing_in_original:
            print(f"   - {rel}")
        if args.limit == 0:
            print(f"In original but not in manifest: {len(extra_in_original)}")
            for rel in extra_in_original:
                print(f"   - {rel}")

    all_good = (
        not failures
        and not mismatched
        and not missing_in_original
        and not extra_in_original
    )
    print()
    if all_good:
        print("ALL GOOD: everything was rebuilt correctly" + (" and matches the original." if args.original else "."))
    else:
        print("PROBLEMS FOUND: see the lists above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
