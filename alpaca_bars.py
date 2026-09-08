#!/usr/bin/env python3
"""Alpaca market-data backfill for Aahil's lab (runs in GitHub Actions with ALPACA_KEY / ALPACA_SECRET).
Resumable and idempotent: every stage skips outputs that already exist and stops when BUDGET seconds are used.

Stage 1  FL0WG0D contracts (requests/bars_request.json) -> data/intraday/<OCC>.json.gz      (done 2026-09-08)
Stage 2  SPY + QQQ 1-minute stock bars (IEX feed), one file per month from 2024-02 -> data/stocks/SPY_QQQ_YYYY-MM.json.gz
Stage 3  SPY same-day-expiry (0DTE) options, 5-minute bars, for every trading day from 2024-02:
         the 12 strikes above and 12 below the 09:35 ET price plus the ATM strike, calls and puts
         -> data/stocks/spy0dte/<YYYY-MM-DD>.json.gz = {date, p935, atm, contracts:{OCC:[[t,o,h,l,c,v,n],...]}}
"""
import json, gzip, os, sys, time, urllib.request, urllib.parse, datetime as dt
from zoneinfo import ZoneInfo
KEY, SEC = os.environ['ALPACA_KEY'], os.environ['ALPACA_SECRET']
BUDGET = int(os.environ.get('BUDGET', '2700')); t0 = time.time()
def left(): return BUDGET - (time.time() - t0)
HDR = {'APCA-API-KEY-ID': KEY, 'APCA-API-SECRET-KEY': SEC, 'Accept': 'application/json'}
def get(url, params):
    for attempt in range(6):
        r = urllib.request.Request(url + '?' + urllib.parse.urlencode(params), headers=HDR)
        try:
            with urllib.request.urlopen(r, timeout=60) as h: return json.load(h)
        except urllib.error.HTTPError as e:
            body = e.read()[:300]
            if e.code == 429: time.sleep(15 + 15 * attempt); continue
            if e.code in (401, 403): print('AUTH/PLAN ERROR', e.code, body); sys.exit(2)
            print('HTTP', e.code, body); time.sleep(5)
        except Exception as e: print('ERR', e); time.sleep(5)
    return None
def paged(url, params, key='bars'):
    out = {}; tok = None
    while True:
        p = dict(params)
        if tok: p['page_token'] = tok
        d = get(url, p)
        if d is None: return None
        for s, bs in (d.get(key) or {}).items(): out.setdefault(s, []).extend(bs)
        tok = d.get('next_page_token')
        if not tok: return out
OPT = 'https://data.alpaca.markets/v1beta1/options/bars'; STK = 'https://data.alpaca.markets/v2/stocks/bars'
def row(b): return [b['t'], b['o'], b['h'], b['l'], b['c'], b.get('v', 0), b.get('n', 0)]

# ---------------- Stage 1: FL0WG0D contracts ----------------
os.makedirs('data/intraday', exist_ok=True)
if os.path.exists('requests/bars_request.json'):
    req = json.load(open('requests/bars_request.json'))
    todo = [(s, x) for s, x in sorted(req.items()) if not os.path.exists(f'data/intraday/{s}.json.gz')]
    print(f'stage 1: {len(req)} contracts, {len(todo)} to fetch')
    groups = {}
    for s, x in todo: groups.setdefault((x['start'], x['end']), []).append(s)
    for (start, end), syms in groups.items():
        for i in range(0, len(syms), 40):
            if left() < 60: print('budget'); sys.exit(0)
            chunk = syms[i:i + 40]
            bars = paged(OPT, {'symbols': ','.join(chunk), 'timeframe': '1Min', 'start': f'{start}T00:00:00Z', 'end': f'{end}T23:59:59Z', 'limit': 10000, 'sort': 'asc'})
            if bars is None: continue
            for s in chunk:
                with gzip.open(f'data/intraday/{s}.json.gz', 'wt') as f: json.dump({'occ': s, 'start': start, 'end': end, 'bars': [row(b) for b in bars.get(s, [])]}, f)
            print(f'stage 1: {start}..{end} {len(chunk)} syms', flush=True)

# ---------------- Stage 2: SPY / QQQ minute bars by month ----------------
os.makedirs('data/stocks', exist_ok=True)
m = dt.date(2024, 2, 1); today = dt.date.today()
while m <= today and left() > 60:
    nxt = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    fn = f'data/stocks/SPY_QQQ_{m:%Y-%m}.json.gz'
    if os.path.exists(fn) and nxt <= today: m = nxt; continue
    bars = paged(STK, {'symbols': 'SPY,QQQ', 'timeframe': '1Min', 'start': f'{m}T00:00:00Z', 'end': f'{min(nxt, today + dt.timedelta(days=1))}T00:00:00Z', 'limit': 10000, 'feed': 'iex', 'sort': 'asc'})
    if bars:
        with gzip.open(fn, 'wt') as f: json.dump({s: [row(b)[:6] for b in v] for s, v in bars.items()}, f)
        print('stage 2:', fn, {s: len(v) for s, v in bars.items()}, flush=True)
    m = nxt

# ---------------- Stage 3: SPY 0DTE option bars around the open ----------------
os.makedirs('data/stocks/spy0dte', exist_ok=True)
ET = ZoneInfo('America/New_York'); OFFS = list(range(-12, 13))
spy = {}
for fn in sorted(os.listdir('data/stocks')):
    if not fn.startswith('SPY_QQQ_'): continue
    d = json.load(gzip.open(f'data/stocks/{fn}', 'rt'))
    for b in d.get('SPY', []):
        t = dt.datetime.fromisoformat(b[0].replace('Z', '+00:00')).astimezone(ET)
        if t.hour == 9 and t.minute in (34, 35, 36):   # price at ~09:35 ET
            day = t.date().isoformat()
            if day not in spy or t.minute == 35: spy[day] = b[1] if t.minute == 35 else b[4]
days = [d for d in sorted(spy) if d >= '2024-02-01' and d < today.isoformat() and dt.date.fromisoformat(d).weekday() < 5]
todo = [d for d in days if not os.path.exists(f'data/stocks/spy0dte/{d}.json.gz')]
print(f'stage 3: {len(days)} trading days with a 09:35 SPY price, {len(todo)} to fetch')
for d in todo:
    if left() < 45: print('budget'); sys.exit(0)
    p = spy[d]; atm = int(round(p)); dd = dt.date.fromisoformat(d)
    occ = [f"SPY{dd:%y%m%d}{cp}{(atm + o) * 1000:08d}" for o in OFFS for cp in ('C', 'P')]
    bars = paged(OPT, {'symbols': ','.join(occ), 'timeframe': '5Min', 'start': f'{d}T12:00:00Z', 'end': f'{d}T22:00:00Z', 'limit': 10000, 'sort': 'asc'})
    if bars is None: print('stage 3: failed', d); continue
    with gzip.open(f'data/stocks/spy0dte/{d}.json.gz', 'wt') as f:
        json.dump({'date': d, 'p935': p, 'atm': atm, 'contracts': {s: [row(b) for b in v] for s, v in bars.items()}}, f)
    print(f'stage 3: {d} p935 {p} contracts with bars {len(bars)} bars {sum(len(v) for v in bars.values())}', flush=True)
print('all stages complete')
