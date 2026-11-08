#!/usr/bin/env python3
"""
Link checker
============

Checks that every download link in one or more manifests really exists online and that the
file there has the expected size. It does NOT download the files (it only asks for the first
byte), so it uses almost no internet data.

Examples (Windows)
------------------
python check_links.py --manifest "G:\\AmongUsBlob\\Release19I-xz\\manifest-19I.json"

python check_links.py ^
  --manifest "https://raw.githubusercontent.com/USER/REPO/main/manifest-19I.json" ^
  --manifest "https://raw.githubusercontent.com/USER/REPO/main/manifest-18I.json"

Needs Python 3.8 or newer. No extra packages.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def load_manifest(source):
    if source.lower().startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"User-Agent": "link-checker/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8-sig"))
    with open(source, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def remote_size(url):
    """Ask for just the first byte and work out the full size of the file from the headers."""
    req = urllib.request.Request(url, headers={"User-Agent": "link-checker/1.0", "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        content_range = r.headers.get("Content-Range", "")
        if "/" in content_range:
            total = content_range.rsplit("/", 1)[1].strip()
            if total.isdigit():
                return int(total)
        length = r.headers.get("Content-Length", "")
        if length.isdigit():
            return int(length)
        return None


def check_one(item):
    url, expected = item
    try:
        got = remote_size(url)
    except urllib.error.HTTPError as e:
        return url, expected, f"MISSING or refused (HTTP {e.code})"
    except Exception as e:
        return url, expected, f"ERROR ({e})"
    if got is None:
        return url, expected, "could not read the size (link exists)"
    if got != expected:
        return url, expected, f"WRONG SIZE (online {got} bytes, expected {expected})"
    return url, expected, None


def main():
    p = argparse.ArgumentParser(description="Check that every link in the manifests works.")
    p.add_argument("--manifest", action="append", required=True,
                   help="Manifest link or file path (can be used several times)")
    args = p.parse_args()

    items = {}  # url -> expected size
    for source in args.manifest:
        try:
            m = load_manifest(source)
        except Exception as e:
            sys.exit(f"Could not read manifest {source}: {e}")
        base = m.get("base_url", "").rstrip("/")
        for f in m.get("files", []):
            url = f.get("url") or (f"{base}/{f['blob']}" if base else "")
            if not url:
                print(f"No link for {f['path']} in {source}")
                continue
            items[url] = f.get("download_size", f["size"])

    print(f"Checking {len(items)} links (no files are downloaded)...")
    problems = []
    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for url, expected, problem in pool.map(check_one, list(items.items())):
            done += 1
            if problem:
                problems.append((url, problem))
            if done % 20 == 0 or done == len(items):
                print(f"  checked {done}/{len(items)}")

    print()
    if not problems:
        print(f"ALL GOOD: all {len(items)} links exist and have the right size.")
        return
    print(f"PROBLEMS FOUND: {len(problems)} of {len(items)}")
    for url, why in problems:
        print(f"  - {url}")
        print(f"    {why}")
    sys.exit(1)


if __name__ == "__main__":
    main()
