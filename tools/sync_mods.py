#!/usr/bin/env python3
"""Sync the mod catalog (mods.json) for the AUL launcher.

Sources the Bean Mod Manager registry (github.com/rewalo/BeanModManager, GPL-3.0)
and reshapes it for our needs: our launcher ships the itch/Steam build of the game
only, so the epic/msstore asset buckets are dropped and one "asset" bucket covers
the Steam+itch build family. Hand-added mods that are not in the BMM list live in
mods-extra.json and are merged in (extras win on id collisions).

Run manually whenever you want fresh data; the launcher only ever reads the
committed mods.json from raw.githubusercontent.com, never this tool or any API.

Usage:  python tools/sync_mods.py
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BMM_REGISTRY_URL = "https://raw.githubusercontent.com/rewalo/BeanModManager/master/mod-registry.json"
EXTRA_FILE = ROOT / "mods-extra.json"
OUT_FILE = ROOT / "mods.json"

# Images: fetched once from Starlight's API (allofus.dev) and cached here, so a
# sync never hammers their server and works offline afterwards. Thumbnails are
# hotlinked from their server; the launcher treats a missing image as cosmetic.
STARLIGHT_API = "https://starlight.allofus.dev/api/v3"
IMG_CACHE = ROOT / "tools" / "starlight-images.json"

# BMM category -> our category
CATEGORIES = {
    "host mod": "host",
    "mod": "mod",
    "dependency": "dependency",
    "map": "map",
    "fun": "fun",
    "utility": "utility",
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "AUL-catalog-sync"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def github_repo_of(url: str):
    """The owner/repo a Starlight 'source' link points at, or None."""
    if not url or "github.com" not in url:
        return None
    repo = url.split("github.com", 1)[1].lstrip("/").split("?")[0].strip("/")
    repo = re.sub(r"\.git$", "", repo, flags=re.I)
    return repo if "/" in repo else None


def starlight_images(refresh: bool = False) -> dict:
    """Map of github 'owner/repo' (lowercase) -> Starlight thumbnail URL."""
    if IMG_CACHE.exists() and not refresh:
        return json.loads(IMG_CACHE.read_text(encoding="utf-8"))
    mods, offset = [], 0
    while True:
        batch = json.loads(fetch(f"{STARLIGHT_API}/mods?limit=100&offset={offset}"))
        mods.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    print(f"Starlight catalog: {len(mods)} mods — reading their GitHub links ...")
    mapping = {}
    for m in mods:
        try:
            detail = json.loads(fetch(m["_links"]["self"]))
        except Exception as e:
            print(f"  detail failed for {m.get('name')}: {e}")
            continue
        src = next((l.get("url") for l in detail.get("links", []) if l.get("type") == "source"), None)
        repo = github_repo_of(src)
        thumb = m.get("_links", {}).get("thumbnail")
        if repo and thumb:
            mapping[repo.lower()] = thumb
    IMG_CACHE.write_text(json.dumps(mapping, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return mapping


def bucket(f) -> dict:
    return {"patterns": list(f.get("patterns", [])), "exclude": list(f.get("exclude", []))}


def dep(d: dict) -> dict:
    out = {"id": d["modId"]}
    if d.get("requiredVersion"):
        out["version"] = d["requiredVersion"]
    if d.get("optional"):
        out["optional"] = True
    return out


def map_mod(m: dict) -> dict:
    filters = m.get("assetFilters", {})
    out = {
        "id": m["id"],
        "name": m["name"],
        "author": m["author"],
        "description": m.get("description", ""),
        "github": f'{m["githubOwner"]}/{m["githubRepo"]}',
        "category": CATEGORIES.get(str(m.get("category", "mod")).lower(), "mod"),
    }

    # One asset bucket for the build family we ship. Steam and itch.io run the
    # same build, so the steam patterns cover both; itch/default are fallbacks.
    for key in ("steam", "itch", "default"):
        f = filters.get(key)
        if f:
            out["asset"] = bucket(f)
            break
    if filters.get("dll"):
        out["dll"] = bucket(filters["dll"])

    if m.get("packageType"):
        out["package"] = m["packageType"]          # "nested" = zip with BepInEx/ inside
    if m.get("keepFiles"):
        out["keepFiles"] = m["keepFiles"]          # folders to keep on uninstall
    if m.get("executableName"):
        out["executable"] = m["executableName"]    # not a plugin; a separate app
    if m.get("dependencies"):
        out["dependencies"] = [dep(d) for d in m["dependencies"]]
    if m.get("versionDependencies"):
        out["versionDependencies"] = {
            au: [dep(d) for d in deps] for au, deps in m["versionDependencies"].items()
        }
    if m.get("incompatibilities"):
        out["incompatible"] = m["incompatibilities"]
    if m.get("requiresDepot") and m.get("depotConfig"):
        # Steam-depot mods pin an old game version; we record it so the launcher
        # can match against the game version the user has installed.
        out["targets"] = m["depotConfig"].get("gameVersion")
    if m.get("featured"):
        out["featured"] = True
    return out


# Mod id -> Starlight cache repo key, for entries whose GitHub URL changed
# since Starlight recorded it (repo renamed or moved to an org). Curated by hand.
IMAGE_OVERRIDES = {
    "AchievementsAPI": "am-clonec/achievements-api",
    "TheOtherRolesGMIA": "gmia-nexus/theotherrolesgmia",
}


def main() -> int:
    print(f"Fetching {BMM_REGISTRY_URL} ...")
    bmm = json.loads(fetch(BMM_REGISTRY_URL))
    mods = {m["id"]: map_mod(m) for m in bmm.get("mods", [])}
    print(f"Bean Mod Manager registry: {len(mods)} mods (schema {bmm.get('version', '?')})")

    if EXTRA_FILE.exists():
        extra = json.loads(EXTRA_FILE.read_text(encoding="utf-8"))
        for m in extra.get("mods", []):
            if m.get("id") in mods:
                print(f"  extra overrides BMM entry: {m['id']}")
            else:
                print(f"  extra mod added: {m['id']}")
            mods[m["id"]] = m
    else:
        print("no mods-extra.json — create one to add mods outside the BMM list")

    # Attach the Starlight thumbnail for every mod whose GitHub repo we share.
    # A failure here only costs us images: cached mappings are reused when the
    # API is unreachable, and the sync itself never fails because of it.
    try:
        imgs = starlight_images()
        print(f"Starlight images: {len(imgs)} known repos")
    except Exception as e:
        print(f"Starlight images unavailable ({e}) — continuing without new images")
        imgs = {}
        if IMG_CACHE.exists():
            imgs = json.loads(IMG_CACHE.read_text(encoding="utf-8"))
    matched = 0
    for m in mods.values():
        key = IMAGE_OVERRIDES.get(m.get("id", ""), m.get("github", "").lower())
        url = imgs.get(key)
        if url:
            m["image"] = url
            matched += 1

    catalog = {
        "schema": 1,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "github.com/rewalo/BeanModManager mod-registry (GPL-3.0); itch/Steam build only",
        "mods": sorted(mods.values(), key=lambda m: m["name"].lower()),
    }
    OUT_FILE.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_FILE.name}: {len(catalog['mods'])} mods, {matched} with images")

    warned = [m["id"] for m in catalog["mods"] if "asset" not in m and "dll" not in m]
    if warned:
        print(f"WARNING — no usable asset bucket: {', '.join(warned)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
