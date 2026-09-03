#!/usr/bin/env python3
"""
Mirror one X account's recent posts into posts.json.

Runs on a GitHub Actions schedule. The trading agent reads the committed
posts.json over raw.githubusercontent.com, so no credentials ever leave this
repo — the bearer token lives only in GitHub's encrypted secrets.

Writes posts.json on EVERY successful run, even when nothing new arrived, so a
stale updated_at is an unambiguous signal that the relay stopped working.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ACCOUNT = os.environ.get("X_ACCOUNT", "FL0WG0D").lstrip("@")
TOKEN = os.environ.get("X_BEARER_TOKEN", "").strip()
OUT = "posts.json"
KEEP = 200
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
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "User-Agent": "x-relay/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        if e.code == 429:
            # Rate limited. Not an error worth failing the workflow over —
            # the next scheduled run picks up where this one left off.
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


def load_state():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main():
    if not TOKEN:
        fail("X_BEARER_TOKEN is not set — add it under Settings > Secrets and variables > Actions")

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
        "tweet.fields": "created_at,text",
    }
    since_id = state.get("last_id")
    if since_id:
        params["since_id"] = since_id

    resp = api(f"users/{user_id}/tweets", params)
    fetched = resp.get("data") or []
    print(f"relay: {len(fetched)} new post(s) since {since_id or 'inception'}")

    seen = {p["id"] for p in posts}
    for t in fetched:
        if t["id"] not in seen:
            posts.append(
                {
                    "id": t["id"],
                    "created_at": t.get("created_at"),
                    "text": t.get("text", ""),
                    "url": f"https://x.com/{ACCOUNT}/status/{t['id']}",
                }
            )
            seen.add(t["id"])

    # Newest first, capped. Post IDs are monotonic, so numeric sort is chronological.
    posts.sort(key=lambda p: int(p["id"]), reverse=True)
    posts = posts[:KEEP]

    out = {
        "account": ACCOUNT,
        "user_id": user_id,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_id": posts[0]["id"] if posts else None,
        "new_this_run": len(fetched),
        "post_count": len(posts),
        "posts": posts,
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"relay: wrote {OUT} with {len(posts)} post(s)")


if __name__ == "__main__":
    main()
