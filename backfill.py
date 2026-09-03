#!/usr/bin/env python3
"""
Walk BACKWARD through one X account's timeline and build a permanent archive.

Why this exists
---------------
relay.py keeps a rolling window (200 posts) so the trading agent always sees
recent flow. That is the wrong shape for research: measuring whether this
account's calls actually predict anything needs months of history, not days.
Five trading days of signals produced a mean excess return whose entire
magnitude came from three observations of a single ticker — a sample that size
cannot distinguish a real edge from one lucky stock.

So this script does the opposite of the relay: it pages backward from the
oldest post already archived, appends everything it finds, and never prunes.
archive.json is append-only and safe to run repeatedly — each run resumes where
the last one stopped.

Text alone is enough for the analysis that matters most. Ticker, timestamp and
direction are all in the post body, which is all you need to ask "does flow from
this account precede the underlying moving?" Card images carry strike and expiry
and are only needed for contract-level backtests, so they are NOT downloaded
here — pulling thousands of images would blow through both the API budget and
the repository.

Limits worth knowing
--------------------
The X API exposes roughly the most recent 3,200 posts per timeline, so that is
the hard floor on how far back this can reach. At this account's ~12 posts a day
that is about eight months, which is plenty. Rate limits are the real
constraint: PAGES caps how many 100-post requests a single run makes, and a 429
exits cleanly so the next run resumes from the saved cursor rather than losing
progress.
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
PAGES = int(os.environ.get("BACKFILL_PAGES", "10"))   # 100 posts per page
ARCHIVE = "archive.json"
API = "https://api.x.com/2/"


def fail(msg, code=1):
    print(f"backfill: {msg}", file=sys.stderr)
    sys.exit(code)


def api(path, params=None, attempt=1):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": "x-backfill/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        if e.code == 429:
            # Save nothing new, but do not treat this as failure: the archive on
            # disk is already consistent and the cursor lets the next run resume.
            print(f"backfill: rate limited (429) — stopping cleanly, rerun later. {body}")
            return None
        if e.code in (500, 502, 503, 504) and attempt < 3:
            time.sleep(3 * attempt)
            return api(path, params, attempt + 1)
        if e.code == 401:
            fail(f"401 unauthorized — bearer token wrong, expired, or revoked: {body}")
        fail(f"HTTP {e.code} on {path}: {body}")
    except urllib.error.URLError as e:
        fail(f"network error on {path}: {e}")


def load_archive():
    if not os.path.exists(ARCHIVE):
        return {"account": ACCOUNT, "user_id": None, "posts": [], "cursor": None,
                "complete": False, "updated_at": None}
    with open(ARCHIVE, "r", encoding="utf-8") as f:
        a = json.load(f)
    a.setdefault("posts", [])
    a.setdefault("cursor", None)
    a.setdefault("complete", False)
    return a


def main():
    if not TOKEN:
        fail("X_BEARER_TOKEN is not set")

    arc = load_archive()

    if not arc.get("user_id"):
        u = api("users/by/username/" + urllib.parse.quote(ACCOUNT))
        if not u or "data" not in u:
            fail(f"could not resolve @{ACCOUNT}")
        arc["user_id"] = u["data"]["id"]
        print(f"backfill: resolved @{ACCOUNT} -> {arc['user_id']}")

    if arc.get("complete"):
        print("backfill: archive already marked complete; nothing to do. "
              "Delete the 'complete' flag in archive.json to force another sweep.")
        return

    seen = {p["id"] for p in arc["posts"]}
    start_count = len(seen)
    added = 0
    cursor = arc.get("cursor")

    # Without a saved cursor, start from the oldest post we already hold so the
    # sweep continues past it rather than re-reading what we have.
    until_id = None
    if not cursor and arc["posts"]:
        until_id = min(arc["posts"], key=lambda p: int(p["id"]))["id"]
        print(f"backfill: no cursor; resuming from oldest known id {until_id}")

    for page in range(1, PAGES + 1):
        params = {
            "max_results": 100,
            "tweet.fields": "created_at,attachments,public_metrics",
            "expansions": "attachments.media_keys",
            "media.fields": "url,preview_image_url,type",
            "exclude": "retweets,replies",
        }
        if cursor:
            params["pagination_token"] = cursor
        elif until_id:
            params["until_id"] = until_id

        resp = api(f"users/{arc['user_id']}/tweets", params)
        if resp is None:            # rate limited; keep what we have
            break

        data = resp.get("data") or []
        media = {m["media_key"]: m for m in (resp.get("includes", {}).get("media") or [])}

        new_this_page = 0
        for t in data:
            if t["id"] in seen:
                continue
            imgs = []
            for k in (t.get("attachments", {}) or {}).get("media_keys", []) or []:
                m = media.get(k)
                if m and m.get("type") == "photo" and m.get("url"):
                    imgs.append(m["url"])          # remote URL only; not downloaded
            arc["posts"].append({
                "id": t["id"],
                "created_at": t.get("created_at"),
                "text": t.get("text", ""),
                "url": f"https://x.com/{ACCOUNT}/status/{t['id']}",
                "image_urls": imgs,
                "metrics": t.get("public_metrics") or {},
            })
            seen.add(t["id"])
            new_this_page += 1
            added += 1

        cursor = (resp.get("meta") or {}).get("next_token")
        oldest = data[-1].get("created_at") if data else "—"
        print(f"backfill: page {page}: {len(data)} returned, {new_this_page} new, "
              f"oldest {oldest}, {'more' if cursor else 'END OF TIMELINE'}")

        if not cursor:
            arc["complete"] = True
            break
        if not data:
            break
        time.sleep(1.5)             # be polite between pages

    arc["cursor"] = cursor
    arc["posts"].sort(key=lambda p: int(p["id"]), reverse=True)
    arc["post_count"] = len(arc["posts"])
    arc["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if arc["posts"]:
        arc["span"] = {"newest": arc["posts"][0].get("created_at"),
                       "oldest": arc["posts"][-1].get("created_at")}

    with open(ARCHIVE, "w", encoding="utf-8") as f:
        json.dump(arc, f, indent=1, ensure_ascii=False)

    print(f"backfill: {start_count} -> {len(arc['posts'])} posts (+{added}). "
          f"complete={arc['complete']}. span={arc.get('span')}")


if __name__ == "__main__":
    main()
