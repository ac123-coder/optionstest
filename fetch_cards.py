#!/usr/bin/env python3
"""
Download every card image referenced in archive.json, OCR it, and write the
parsed contract details to cards.json.

Why: the archive holds two months of post text, which is enough to study the
underlying but not the contract. The Bullflow card in each post carries strike,
expiry, premium, OTM%, volume and open interest — everything needed to backtest
the original options rules on real contracts rather than forward-only. The
research sandbox cannot reach pbs.twimg.com; this runner can.

Idempotent: images already on disk are not re-downloaded, and cards.json is
rebuilt from whatever is present, so a partial run is safe to resume.
"""

import json, os, re, subprocess, sys, time, urllib.request

ARCHIVE = "archive.json"
OUT_DIR = "archive-cards"
OUT = "cards.json"
MAX_PER_POST = 2

os.makedirs(OUT_DIR, exist_ok=True)
arc = json.load(open(ARCHIVE, encoding="utf-8"))
posts = arc["posts"]

def fetch(url, path):
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return True
    req = urllib.request.Request(url, headers={"User-Agent": "x-cards/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
                f.write(r.read())
            return True
        except Exception as e:
            time.sleep(2 * (attempt + 1))
    return False

def ocr(path):
    try:
        return subprocess.run(["tesseract", path, "-", "--psm", "6"],
                              capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""

# ---- parser, written against real OCR output of Bullflow cards -------------
HEAD = re.compile(r"^\s*([A-Z]{1,5})\s+(\d+(?:\.\d+)?)\s+(Call|Put)(?:\s+\$?(\d+(?:\.\d+)?))?", re.M)
EXP  = re.compile(r"Exp\.?\s*(\d{2})/(\d{2})/(\d{2})")
PREM = re.compile(r"Prem:?\s*\$?([\d.]+)\s*([KM])", re.I)
OTM  = re.compile(r"OTM:?\s*(-?[\d.]+)%")
VOL  = re.compile(r"Vol:?\s*([\d.]+K?|N/A)", re.I)
OI   = re.compile(r"(?<![A-Za-z])O[lI1]:?\s*([\d.]+K?|N/A)", re.I)   # not the "ol" inside "Vol"
FILL = re.compile(r"Avg\s*fill\s*\$?(\d+(?:\.\d+)?)", re.I)
TIME = re.compile(r"Time:\s*(\d{2}/\d{2}/\d{2}),?\s*(\d{1,2}:\d{2}:\d{2}\s*[AP]M)", re.I)

def num(s):
    if not s or s.upper() == "N/A": return None
    s = s.upper().replace(",", "")
    mult = 1000 if s.endswith("K") else 1_000_000 if s.endswith("M") else 1
    try: return float(s.rstrip("KM")) * mult
    except ValueError: return None

def parse(text):
    h = HEAD.search(text)
    if not h: return None
    ticker, strike, typ, last = h.group(1), float(h.group(2)), h.group(3).lower(), h.group(4)
    e = EXP.search(text)
    expiry = f"20{e.group(3)}-{e.group(1)}-{e.group(2)}" if e else None
    p = PREM.search(text); o = OTM.search(text); v = VOL.search(text); oi = OI.search(text)
    f = FILL.search(text); t = TIME.search(text)
    return {
        "ticker": ticker, "strike": strike, "type": typ, "expiry": expiry,
        "last": float(last) if last else None,
        "premium": num(p.group(1) + p.group(2)) if p else None,
        "otmPct": float(o.group(1)) if o else None,
        "volume": num(v.group(1)) if v else None,
        "openInterest": num(oi.group(1)) if oi else None,
        "avgFill": float(f.group(1)) if f else None,
        "cardTime": (t.group(1) + " " + t.group(2)) if t else None,
    }

out = []; got = 0; failed = 0; parsed_ok = 0
for p in posts:
    urls = (p.get("image_urls") or [])[:MAX_PER_POST]
    if not urls: continue
    rec = {"postId": p["id"], "createdAt": p.get("created_at"), "text": p.get("text", ""), "cards": []}
    for n, url in enumerate(urls):
        path = os.path.join(OUT_DIR, f"{p['id']}-{n}.jpg")
        ok = fetch(url, path)
        if not ok:
            failed += 1; rec["cards"].append({"url": url, "error": "download failed"}); continue
        got += 1
        text = ocr(path)
        parsed = parse(text)
        if parsed: parsed_ok += 1
        rec["cards"].append({"file": path, "parsed": parsed, "ocr": text[:600]})
    out.append(rec)

json.dump({"generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "posts": len(out), "images": got, "downloadFailed": failed,
           "parsed": parsed_ok, "records": out},
          open(OUT, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print(f"cards: {len(out)} posts, {got} images fetched, {failed} failed, {parsed_ok} parsed")
