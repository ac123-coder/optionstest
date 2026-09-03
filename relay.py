#!/usr/bin/env python3
"""
Mirror one X account's recent posts — and their attached images — into this repo.

The trading agent runs in a sandbox that can reach raw.githubusercontent.com but
not api.x.com or pbs.twimg.com, so everything it needs has to be committed here.
Posts land in posts.json; attached images land in images/ and are referenced from
each post entry. The bearer token never leaves GitHub's encrypted secrets.

posts.json is rewritten on every successful run even when nothing new arrived, so
a stale updated_at is an unambiguous signal that the relay stopped working.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ACCOUNT = os.environ.get("X_ACCOUNT", "FL0WG0D").lstrip("@")
TOKEN = os.environ.get("X_BEARER_TOKEN", "").strip()
OUT = "posts.json"
IMG_DIR = "images"
KEEP = 200          # posts retained in posts.json
IMG_KEEP = 60       # posts whose images are retained on disk
MAX_IMG_PER_POST = 3
API = "https://api.x.com/2/"


def fail(msg, code=1):
    print(f"relay: {msg}", file=sys.stderr)
    sys.exit(code)


def api(path, params=None, attempt=1):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": "x-relay/2.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        if e.code == 429:
            print(f"relay: rate limited (429), leaving state unchanged: {body}")
            sys.exit(0)
        if e.code in (500, 502, 503, 504) and attempt < 3:
            time.sleep(3 * attempt)
            return api(path, params, attempt + 1)
        if e.code == 401:
            fail(f"401 unauthorized — the bearer token is wrong, expired, or revoked: {body}")
        fail(f"HTTP {e.code} on {path}: {body}")
    except urllib.error.URLError as e:
        if attempt < 3:
            time.sleep(3 * attempt)
            return api(path, params, attempt + 1)
        fail(f"network error on {path}: {e.reason}")


def download(url, dest):
    """Best effort. A missing image must never fail the run."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "x-relay/2.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        if not data:
            return False
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception as e:  # noqa: BLE001 - deliberately broad
        print(f"relay: could not fetch image {url}: {e}")
        return False


def load_state():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main():
    if not TOKEN:
        fail("X_BEARER_TOKEN is not set — add it under Settings > Secrets and variables > Actions")

    os.makedirs(IMG_DIR, exist_ok=True)

    state = load_state()
    posts = state.get("posts") or []
    user_id = state.get("user_id")

    if not user_id:
        user = api(f"users/by/username/{ACCOUNT}")
        if "data" not in user:
            fail(f"could not resolve @{ACCOUNT}: {json.dumps(user)[:300]}")
        user_id = user["data"]["id"]
        print(f"relay: resolved @{ACCOUNT} to user id {user_id}")

    params = {
        "max_results": 100,
        "exclude": "retweets,replies",
        "tweet.fields": "created_at,text,attachments",
        "expansions": "attachments.media_keys",
        "media.fields": "url,preview_image_url,type",
    }
    since_id = state.get("last_id")
    if since_id:
        params["since_id"] = since_id

    resp = api(f"users/{user_id}/tweets", params)
    fetched = resp.get("data") or []
    media_by_key = {
        m["media_key"]: m for m in (resp.get("includes", {}).get("media") or [])
    }
    print(f"relay: {len(fetched)} new post(s) since {since_id or 'inception'}")

    seen = {p["id"] for p in posts}
    saved_images = 0

    for t in fetched:
        if t["id"] in seen:
            continue

        images = []
        keys = (t.get("attachments") or {}).get("media_keys") or []
        for i, key in enumerate(keys[:MAX_IMG_PER_POST]):
            m = media_by_key.get(key)
            if not m:
                continue
            # Photos carry `url`; videos and GIFs only carry a preview frame.
            src = m.get("url") or m.get("preview_image_url")
            if not src:
                continue
            ext = os.path.splitext(urllib.parse.urlparse(src).path)[1] or ".jpg"
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                ext = ".jpg"
            name = f"{t['id']}-{i}{ext}"
            # Request the full-size render; X serves a small default otherwise.
            full = src + ("&" if "?" in src else "?") + "name=large"
            if download(full, os.path.join(IMG_DIR, name)):
                images.append(f"{IMG_DIR}/{name}")
                saved_images += 1

        posts.append(
            {
                "id": t["id"],
                "created_at": t.get("created_at"),
                "text": t.get("text", ""),
                "url": f"https://x.com/{ACCOUNT}/status/{t['id']}",
                "images": images,
            }
        )
        seen.add(t["id"])

    # Newest first, capped. Post IDs are monotonic, so numeric sort is chronological.
    posts.sort(key=lambda p: int(p["id"]), reverse=True)
    posts = posts[:KEEP]

    # Prune images for anything older than the retention window so the repo
    # doesn't grow without bound.
    keep_files = set()
    for p in posts[:IMG_KEEP]:
        keep_files.update(os.path.basename(x) for x in p.get("images") or [])
    removed = 0
    for fn in os.listdir(IMG_DIR):
        if fn not in keep_files:
            try:
                os.remove(os.path.join(IMG_DIR, fn))
                removed += 1
            except OSError:
                pass
    # Drop references the pruner just invalidated.
    for p in posts[IMG_KEEP:]:
        if p.get("images"):
            p["images"] = []

    out = {
        "account": ACCOUNT,
        "user_id": user_id,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_id": posts[0]["id"] if posts else None,
        "new_this_run": len(fetched),
        "post_count": len(posts),
        "image_retention_posts": IMG_KEEP,
        "posts": posts,
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(
        f"relay: wrote {OUT} with {len(posts)} post(s); "
        f"saved {saved_images} image(s), pruned {removed}"
    )


if __name__ == "__main__":
    main()
