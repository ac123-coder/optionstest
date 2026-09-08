#!/usr/bin/env python3
"""Fetch 1-minute option bars from Alpaca for every contract in requests/bars_request.json.
Runs inside GitHub Actions with ALPACA_KEY / ALPACA_SECRET secrets. Resumable: contracts that already
have data/intraday/<OCC>.json.gz are skipped, and the run stops after BUDGET seconds so the workflow
can commit and continue next time. Output per contract: {"occ", "start", "end", "bars": [[t_iso, o, h, l, c, v, n], ...]}
"""
import json, gzip, os, sys, time, urllib.request, urllib.parse
KEY, SEC = os.environ['ALPACA_KEY'], os.environ['ALPACA_SECRET']
URL = 'https://data.alpaca.markets/v1beta1/options/bars'
OUT = 'data/intraday'; os.makedirs(OUT, exist_ok=True)
BUDGET = int(os.environ.get('BUDGET', '2700'))   # seconds per run (workflow commits after)
BATCH = 40                                        # symbols per request (all with the same start/end)
req = json.load(open('requests/bars_request.json'))
todo = [(s, x) for s, x in sorted(req.items()) if not os.path.exists(f'{OUT}/{s}.json.gz')]
print(f'{len(req)} contracts, {len(todo)} to fetch')
def get(params):
    for attempt in range(6):
        r = urllib.request.Request(URL + '?' + urllib.parse.urlencode(params), headers={'APCA-API-KEY-ID': KEY, 'APCA-API-SECRET-KEY': SEC, 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(r, timeout=60) as h: return json.load(h)
        except urllib.error.HTTPError as e:
            body = e.read()[:300]
            if e.code == 429: time.sleep(15 + 15 * attempt); continue
            if e.code in (401, 403): print('AUTH/PLAN ERROR', e.code, body); sys.exit(2)
            print('HTTP', e.code, body); time.sleep(5)
        except Exception as e: print('ERR', e); time.sleep(5)
    return None
t0 = time.time(); done = 0
# group by (start,end) so one request covers many symbols
groups = {}
for s, x in todo: groups.setdefault((x['start'], x['end']), []).append(s)
for (start, end), syms in groups.items():
    for i in range(0, len(syms), BATCH):
        if time.time() - t0 > BUDGET: print('budget reached; fetched', done); sys.exit(0)
        chunk = syms[i:i + BATCH]; bars = {s: [] for s in chunk}; tok = None
        while True:
            p = {'symbols': ','.join(chunk), 'timeframe': '1Min', 'start': f'{start}T00:00:00Z', 'end': f'{end}T23:59:59Z', 'limit': 10000, 'sort': 'asc'}
            if tok: p['page_token'] = tok
            d = get(p)
            if d is None: print('giving up on chunk', chunk[:3]); break
            for s, bs in (d.get('bars') or {}).items():
                bars.setdefault(s, []).extend([[b['t'], b['o'], b['h'], b['l'], b['c'], b.get('v', 0), b.get('n', 0)] for b in bs])
            tok = d.get('next_page_token')
            if not tok: break
        if d is None: continue
        for s in chunk:
            with gzip.open(f'{OUT}/{s}.json.gz', 'wt') as f: json.dump({'occ': s, 'start': start, 'end': end, 'bars': bars.get(s, [])}, f)
            done += 1
        print(f'{done}/{len(todo)} {start}..{end} {len(chunk)} syms, bars {sum(len(bars.get(s, [])) for s in chunk)}', flush=True)
print('all done', done)

# ---- SPY / QQQ 1-minute stock bars (IEX feed, free) for the heatmap-level study, one file per month ----
SURL = 'https://data.alpaca.markets/v2/stocks/bars'
os.makedirs('data/stocks', exist_ok=True)
import datetime as _dt
m = _dt.date(2025, 10, 1); today = _dt.date.today()
while m <= today and time.time() - t0 < BUDGET:
    nxt = (m.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
    fn = f'data/stocks/SPY_QQQ_{m:%Y-%m}.json.gz'
    if os.path.exists(fn) and nxt <= today: m = nxt; continue     # complete months are final
    bars = {}; tok = None
    while True:
        p = {'symbols': 'SPY,QQQ', 'timeframe': '1Min', 'start': f'{m}T00:00:00Z', 'end': f'{min(nxt, today + _dt.timedelta(days=1))}T00:00:00Z', 'limit': 10000, 'feed': 'iex', 'sort': 'asc'}
        if tok: p['page_token'] = tok
        for attempt in range(6):
            r = urllib.request.Request(SURL + '?' + urllib.parse.urlencode(p), headers={'APCA-API-KEY-ID': KEY, 'APCA-API-SECRET-KEY': SEC})
            try:
                with urllib.request.urlopen(r, timeout=60) as h: d = json.load(h); break
            except urllib.error.HTTPError as e:
                print('stocks HTTP', e.code, e.read()[:200]); time.sleep(15); d = None
            except Exception as e: print('stocks ERR', e); time.sleep(5); d = None
        if not d: break
        for s, bs in (d.get('bars') or {}).items(): bars.setdefault(s, []).extend([[b['t'], b['o'], b['h'], b['l'], b['c'], b.get('v', 0)] for b in bs])
        tok = d.get('next_page_token')
        if not tok: break
    if bars:
        with gzip.open(fn, 'wt') as f: json.dump(bars, f)
        print('stocks', fn, {s: len(v) for s, v in bars.items()}, flush=True)
    m = nxt
