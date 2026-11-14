#!/usr/bin/env python3
"""
gen_levels.py - auto-updating Bookmap notes (price-level CSV) generator for BTC charts.

Stdlib only (Python 3.9+).  Data: Deribit public API (no key needed).
Output: one CSV with a row per (symbol alias, level).  Point a Bookmap *Cloud Notes* column at it
(URL or file:/// URL) and re-run this script on a timer.  See README.md.

  python gen_levels.py                      # all defaults, writes ./out/
  python gen_levels.py --act-n 10 --label-style short
  python gen_levels.py --symbols "BTC-PERPETUAL@DB=BTC-PERPETUAL" "BTC_USDC@DB=BTC_USDC"
  python gen_levels.py --merge-gap 0                  # old behaviour: merge only levels within --tick
  python gen_levels.py --settlements                  # re-enable the SF / SM settlement levels
  python gen_levels.py --verify out/bm_levels.csv     # only validate a file
"""
import argparse, csv, gzip, json, math, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
API = "https://www.deribit.com/api/v2/public/"
HDR = ["Symbol", "Price Level", "Note", "Foreground Color", "Background Color", "Text Alignment",
       "Notification Enabled", "Sound Notification Enabled", "Notification Is Repeatable",
       "Delay Before Repeating", "Subscribing Offset", "Notification Sound",
       "Draw Note Price Horizontal Line"]

# alias -> Deribit instrument used for the candle-based levels (see README "Price source per symbol")
DEFAULT_SYMBOLS = {
    "BTC-USD:MB:SP@BMD":    "BTC_USDC",            # USD spot proxy (USDC ~ USD)
    "BTC-USDT:MB:SP@BMD":   "BTC_USDT",            # Deribit USDT spot
    "BTC-USDT:MB:PERP@BMD": "BTC_USDC-PERPETUAL",  # linear perp proxy for the USDT perp
    "BTC_USDC@DB":          "BTC_USDC",
    "BTC_USDC-PERPETUAL@DB": "BTC_USDC-PERPETUAL",
    "BTC-PERPETUAL@DB":     "BTC-PERPETUAL",
}

# ---- styles: key -> (foreground, background, horizontal line) ---------------------------------
# Only bm-levels' OWN families (the user's 10k/5k/1k/500 grid, alert and violet-marker palette is not in here).
# Readable on Bookmap 8's dark UI: mid/bright backgrounds only (no near-black), text = black or white,
# whichever contrasts more (all >= 4.5:1, checked at run time).  "previous" = lighter, softer shade of the
# same hue.  Every family draws its price line (the line takes the note's colour); --no-lines / --no-lines-for.
_BG = {
    "day":     "#e7e5e4", "pday":     "#d6d3d1",    # stone (only previous day is generated)
    "week":    "#60a5fa", "pweek":    "#bfdbfe",    # blue
    "month":   "#2dd4bf", "pmonth":   "#99f6e4",    # teal
    "quarter": "#facc15", "pquarter": "#fef08a",    # yellow
    "year":    "#f8fafc", "pyear":    "#94a3b8",    # white / slate
    "mid":     "#a1a1aa",                           # prev-period midpoints (zinc)
    "vwap":    "#c084fc", "pvwap":    "#e9d5ff",    # purple
    "poc":     "#ec4899", "ppoc":     "#f9a8d4",    # pink
    "va":      "#fda4af", "pva":      "#fecdd3",    # rose (value-area edges)
    "settle":  "#d6a77a",                           # tan (SF/SM, off by default)
    "cw":      "#84cc16", "cwx":      "#d9f99d",    # lime (call walls; weekly/monthly lighter)
    "pw":      "#d946ef", "pwx":      "#f5d0fe",    # fuchsia (put walls)
    "mp":      "#f97316",                           # orange (max pain)
    "gflip":   "#22d3ee",                           # cyan (gamma flip)
    "gpos":    "#4ade80", "gneg":     "#f87171",    # green / red GEX strikes
    "actc":    "#818cf8", "actp":     "#fed7aa",    # indigo calls / peach puts (active strikes)
}


def _text_for(bg):
    return "#000000" if contrast("#000000", bg) >= contrast("#ffffff", bg) else "#ffffff"



# ---- importance order for --merge-gap (most important first) -------------------------------------
# Used to (a) pick the level a merged note is placed at / coloured like and (b) order the labels in the note.
# Reasoning: big-timeframe extremes (year > quarter) and the all-expiry option walls first, then month
# extremes, the zero-gamma flip, week extremes, weekly/monthly option walls + max pain, period opens,
# profile / VWAP levels, GEX strikes, period mid-points, value-area edges; previous-day levels, settlements
# and the "most active strike" notes (A1..An, by their own rank) are the least important.
IMPORTANCE = [
    "YH", "YL", "pYH", "pYL",                           # year extremes
    "pQH", "pQL", "QH", "QL",                           # quarter extremes
    "CW", "PW",                                         # all-expiry call / put wall
    "pMH", "pML", "MH", "ML",                           # month extremes
    "GF",                                               # zero-gamma flip
    "pWH", "pWL", "WH", "WL",                           # week extremes (pWH/pWL are the alert levels)
    "CWm", "PWm", "MPm", "CWw", "PWw", "MPw",           # monthly / weekly option walls and max pain
    "YO", "QO", "pQO", "MO", "pMO", "WO", "pWO",        # period opens
    "WPOC", "pWPOC", "wVWAP", "pWVW",                   # current / previous week POC and VWAP
    "G+1", "G-1", "G+2", "G-2", "G+3", "G-3", "G+4", "G-4",   # GEX strikes
    "pQM", "pMM", "pWM",                                # previous-period mid-points
    "WVAH", "WVAL", "pWVAH", "pWVAL",                   # value-area edges
    "pDH", "pDL",                                       # previous day
    "pDPOC", "pDVAH", "pDVAL", "pDVW",                  # previous-day profile / VWAP
    "SF", "SM",                                         # settlements (only with --settlements)
] + [f"A{i}" for i in range(1, 100)]                    # most active strikes, in their own rank order
IMPORTANCE_RANK = {k: i for i, k in enumerate(IMPORTANCE)}
ALERT_SOUNDS = {"pWH": "Glass", "pWL": "Basso", "pMH": "Ping", "pML": "Funk"}   # names already used in user's exports


# ============================================================================== helpers
def http_json(path, **params):
    url = API + path + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bm-levels/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            if "result" in d:
                return d["result"]
            last = d.get("error")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Deribit request failed: {path} {params} -> {last}")


def ms(dt): return int(dt.timestamp() * 1000)
def from_ms(x): return datetime.fromtimestamp(x / 1000, UTC)


def add_months(d, n):
    m = d.month - 1 + n
    return d.replace(year=d.year + m // 12, month=m % 12 + 1, day=1)


def fmt_k(p):
    return f"{int(p // 1000)}k" if p % 1000 == 0 else f"{p / 1000:.1f}k"


def fmt_n(x):
    x = abs(x)
    if x >= 1e9: return f"{x / 1e9:.1f}B"
    if x >= 1e6: return f"{x / 1e6:.0f}M" if x >= 1e7 else f"{x / 1e6:.1f}M"
    if x >= 1e3: return f"{x / 1e3:.1f}k" if x < 1e4 else f"{x / 1e3:.0f}k"
    return f"{x:.1f}" if x < 10 else f"{x:.0f}"


def lum(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]


def contrast(a, b):
    l1, l2 = sorted([lum(a), lum(b)], reverse=True)
    return (l1 + .05) / (l2 + .05)


STYLES = {k: (_text_for(v), v, "true") for k, v in _BG.items()}


# ============================================================================== candles
def fetch_candles(instrument, start, end, resolution=60):
    """Candles [start,end) as list of dicts, chunked (Deribit caps results at ~5000 per call).
    resolution in minutes: 60 = 1h (period H/L/O), 1 = 1m (VWAP / volume profile)."""
    out, seen = [], set()
    step = timedelta(minutes=4000 * resolution)
    s = start
    while s < end:
        e = min(s + step, end)
        r = http_json("get_tradingview_chart_data", instrument_name=instrument,
                      start_timestamp=ms(s), end_timestamp=ms(e), resolution=resolution)
        for i, t in enumerate(r.get("ticks", [])):
            if t in seen or t >= ms(end): continue
            seen.add(t)
            out.append(dict(t=t, o=r["open"][i], h=r["high"][i], l=r["low"][i], c=r["close"][i], v=r["volume"][i]))
        s = e
    out.sort(key=lambda x: x["t"])
    return out


def period_stats(candles, start, end, max_gap_h=3):
    """open/high/low/close/vwap over [start,end); None if history doesn't cover the period start."""
    sub = [c for c in candles if ms(start) <= c["t"] < ms(end)]
    if not sub or sub[0]["t"] - ms(start) > max_gap_h * 3600_000:
        return None
    vol = sum(c["v"] for c in sub)
    vwap = sum((c["h"] + c["l"] + c["c"]) / 3 * c["v"] for c in sub) / vol if vol > 0 else None
    return dict(o=sub[0]["o"], h=max(c["h"] for c in sub), l=min(c["l"] for c in sub),
                c=sub[-1]["c"], vwap=vwap, n=len(sub))


def vwap_of(c1m, start, end):
    """Volume-weighted average of 1m HLC3 over [start,end); None without volume."""
    num = den = 0.0
    for c in c1m:
        if ms(start) <= c["t"] < ms(end):
            num += (c["h"] + c["l"] + c["c"]) / 3 * c["v"]; den += c["v"]
    return num / den if den > 0 else None


def volume_profile(c1m, start, end, bin_usd, va_frac=0.70, wkey="v"):
    """Volume profile of 1m candles over [start,end): each candle's volume is spread uniformly over the
    price bins its [low, high] range covers.  Returns dict(poc, vah, val, vol) or None.
    poc = centre of the max-volume bin; VA grown from the POC bin by adding the heavier of the next two
    bins above / below until va_frac of total volume (standard TPO/Market-Profile rule);
    vah / val = upper edge of the top / lower edge of the bottom value-area bin."""
    bins = {}
    for c in c1m:
        w = c[wkey]
        if not (ms(start) <= c["t"] < ms(end)) or w <= 0: continue
        lo, hi = int(c["l"] // bin_usd), int(c["h"] // bin_usd)
        if hi == lo: bins[lo] = bins.get(lo, 0.0) + w; continue
        rng = c["h"] - c["l"]
        for b in range(lo, hi + 1):
            ov = min(c["h"], (b + 1) * bin_usd) - max(c["l"], b * bin_usd)
            if ov > 0: bins[b] = bins.get(b, 0.0) + w * ov / rng
    if not bins: return None
    lo, hi = min(bins), max(bins)
    arr = [bins.get(b, 0.0) for b in range(lo, hi + 1)]
    total = sum(arr)
    ip = max(range(len(arr)), key=lambda i: arr[i])
    a = b = ip; acc = arr[ip]
    while acc < va_frac * total and (a > 0 or b < len(arr) - 1):
        up = sum(arr[b + 1:b + 3]) if b < len(arr) - 1 else -1
        dn = sum(arr[max(a - 2, 0):a]) if a > 0 else -1
        if up >= dn:
            for _ in range(2):
                if b < len(arr) - 1 and acc < va_frac * total: b += 1; acc += arr[b]
        else:
            for _ in range(2):
                if a > 0 and acc < va_frac * total: a -= 1; acc += arr[a]
    return dict(poc=(lo + ip + 0.5) * bin_usd, vah=(lo + b + 1) * bin_usd, val=(lo + a) * bin_usd, vol=total)


def periods(now):
    d0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    w0 = d0 - timedelta(days=d0.weekday())            # Monday 00:00 UTC
    m0 = d0.replace(day=1)
    q0 = m0.replace(month=3 * ((m0.month - 1) // 3) + 1)
    y0 = d0.replace(month=1, day=1)
    return {
        "D": (d0, d0 + timedelta(days=1)), "pD": (d0 - timedelta(days=1), d0),
        "W": (w0, w0 + timedelta(days=7)), "pW": (w0 - timedelta(days=7), w0),
        "M": (m0, add_months(m0, 1)),      "pM": (add_months(m0, -1), m0),
        "Q": (q0, add_months(q0, 3)),      "pQ": (add_months(q0, -3), q0),
        "Y": (y0, add_months(y0, 12)),     "pY": (add_months(y0, -12), y0),
    }


def candle_levels(candles, now, cfg, c1m=None):
    """-> list of level dicts for one price source."""
    P = periods(now)
    st = {k: period_stats(candles, a, min(b, now + timedelta(hours=1))) for k, (a, b) in P.items()}
    L = []
    def add(key, tag, price, style, prio, group):
        if price is not None:
            L.append(dict(key=key, tag=tag, price=float(price), rich=None, style=style, prio=prio, group=group))
    g = cfg.groups
    if "day" in g:
        # (DO = today's open was removed on request; no current-day level is generated any more)
        if st["pD"]:
            add("pDH", "pDH", st["pD"]["h"], "pday", 45, "day"); add("pDL", "pDL", st["pD"]["l"], "pday", 45, "day")
    for code, nm in (("W", "week"), ("M", "month"), ("Q", "quarter")):
        if nm not in g: continue
        c, p = st[code], st["p" + code]
        if c:
            add(f"{code}O", f"{code}O", c["o"], nm, 50, nm)
            add(f"{code}H", f"{code}H", c["h"], nm, 40, nm); add(f"{code}L", f"{code}L", c["l"], nm, 40, nm)
        if p:
            add(f"p{code}O", f"p{code}O", p["o"], "p" + nm, 52, nm)
            add(f"p{code}H", f"p{code}H", p["h"], "p" + nm, 30, nm); add(f"p{code}L", f"p{code}L", p["l"], "p" + nm, 30, nm)
            add(f"p{code}M", f"p{code}M", (p["h"] + p["l"]) / 2, "mid", 70, nm)
    if "year" in g:
        c, p = st["Y"], st["pY"]
        if c:
            add("YO", "YO", c["o"], "year", 48, "year"); add("YH", "YH", c["h"], "year", 42, "year"); add("YL", "YL", c["l"], "year", 42, "year")
        if p:
            add("pYH", "pYH", p["h"], "pyear", 44, "year"); add("pYL", "pYL", p["l"], "pyear", 44, "year")
    if c1m:
        end = now + timedelta(minutes=1)
        if "vwap" in g:
            # DVW (current-day VWAP) was removed on request; the current-week VWAP is labelled wVWAP
            for code, key, nm, pr in (("pD", "pDVW", "pvwap", 64), ("W", "wVWAP", "vwap", 63), ("pW", "pWVW", "pvwap", 63)):
                a, b = P[code]; v = vwap_of(c1m, a, min(b, end))
                if v: add(key, key, v, nm, pr, "vwap")
        if "profile" in g:
            for code, pre, cur in (("W", "W", True), ("pW", "pW", False), ("pD", "pD", False)):
                a, b = P[code]; vp = volume_profile(c1m, a, min(b, end), cfg.vp_bin, cfg.vp_va)
                if not vp: continue
                add(f"{pre}POC", f"{pre}POC", vp["poc"], "poc" if cur else "ppoc", 55, "profile")
                add(f"{pre}VAH", f"{pre}VAH", vp["vah"], "va" if cur else "pva", 66, "profile")
                add(f"{pre}VAL", f"{pre}VAL", vp["val"], "va" if cur else "pva", 66, "profile")
                st[code + "_profile"] = {k: round(v, 1) for k, v in vp.items()}
    return L, st



# ============================================================================== options
def norm_pdf(x): return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def gamma_b76(F, K, iv, T):
    """Black-76 gamma wrt forward (r=0). iv as fraction."""
    if T <= 0 or iv <= 0 or F <= 0: return 0.0
    s = iv * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    return norm_pdf(d1) / (F * s)


def load_options(now):
    book = http_json("get_book_summary_by_currency", currency="BTC", kind="option")
    idx = http_json("get_index_price", index_name="btc_usd")["index_price"]
    opts = []
    for b in book:
        p = b["instrument_name"].split("-")
        if len(p) != 4: continue
        try:
            exp = datetime.strptime(p[1], "%d%b%y").replace(hour=8, tzinfo=UTC)
            K = float(p[2])
        except ValueError:
            continue
        T = (exp - now).total_seconds() / (365 * 86400)
        if T <= 0: continue
        opts.append(dict(name=b["instrument_name"], exp=exp, K=K, cp=p[3], oi=b.get("open_interest") or 0.0,
                         vol=b.get("volume") or 0.0, iv=(b.get("mark_iv") or 0) / 100.0,
                         F=b.get("underlying_price") or idx, T=T))
    return opts, idx


def max_pain(opts):
    strikes = sorted({o["K"] for o in opts})
    best, bestv = None, None
    for x in strikes:
        pay = sum(o["oi"] * (max(0.0, x - o["K"]) if o["cp"] == "C" else max(0.0, o["K"] - x)) for o in opts)
        if bestv is None or pay < bestv: best, bestv = x, pay
    return best


def total_gex(opts, S, S0, sign_call):
    tot = 0.0
    for o in opts:
        if o["oi"] <= 0 or o["iv"] <= 0: continue
        g = gamma_b76(o["F"] * S / S0, o["K"], o["iv"], o["T"])
        tot += (sign_call if o["cp"] == "C" else -sign_call) * o["oi"] * g * S * S * 0.01
    return tot


def load_state(path):
    try:
        with open(path) as fh: return json.load(fh)
    except Exception:
        return {"snaps": []}


def save_state(path, state, now, opts, keep_h=72):
    snaps = [s for s in state.get("snaps", []) if now.timestamp() - s["ts"] < keep_h * 3600]
    if not snaps or now.timestamp() - snaps[-1]["ts"] > 3000:        # <= 1 snapshot / ~hour
        snaps.append({"ts": now.timestamp(), "oi": {o["name"]: o["oi"] for o in opts}})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh: json.dump({"snaps": snaps}, fh)


def ref_snapshot(state, now, min_age_h):
    best = None
    for s in state.get("snaps", []):
        age = (now.timestamp() - s["ts"]) / 3600
        if min_age_h <= age <= 48 and (best is None or abs(age - 24) < abs(best[0] - 24)):
            best = (age, s)
    return best


def option_levels(now, cfg, state):
    opts, S0 = load_options(now)
    L, info = [], {"spot": S0, "n_instruments": len(opts), "convention": cfg.gex_convention}
    short = cfg.label_style == "short"
    def add(key, tag, price, rich, style, prio):
        L.append(dict(key=key, tag=tag, price=float(price), rich=None if short else rich, style=style, prio=prio, group="options"))
    agg = lambda sub, cp, f: sum(o[f] for o in sub if o["cp"] == cp)
    by_k = {}
    for o in opts: by_k.setdefault(o["K"], []).append(o)
    used = set()

    def wall_pair(sub, suffix, sc, sp, prio):
        calls = {k: sum(o["oi"] for o in v if o["cp"] == "C") for k, v in by_class(sub).items()}
        puts = {k: sum(o["oi"] for o in v if o["cp"] == "P") for k, v in by_class(sub).items()}
        ck = max((k for k in calls if k >= S0 and calls[k] > 0), key=lambda k: calls[k], default=None)
        pk = max((k for k in puts if k <= S0 and puts[k] > 0), key=lambda k: puts[k], default=None)
        if ck: add("CW" + suffix, "CW" + suffix, ck, f"CW{suffix} {fmt_k(ck)} OI{fmt_n(calls[ck])}", sc, prio); used.add(ck)
        if pk: add("PW" + suffix, "PW" + suffix, pk, f"PW{suffix} {fmt_k(pk)} OI{fmt_n(puts[pk])}", sp, prio); used.add(pk)
        info[f"CW{suffix}"] = ck; info[f"PW{suffix}"] = pk

    def by_class(sub):
        d = {}
        for o in sub: d.setdefault(o["K"], []).append(o)
        return d

    exps = sorted({o["exp"] for o in opts})
    fridays = [e for e in exps if e.weekday() == 4]
    monthlies = [e for e in fridays if (e + timedelta(days=7)).month != e.month]
    wk = fridays[0] if fridays else None
    mo = monthlies[0] if monthlies else None
    info["weekly_expiry"] = wk.strftime("%d%b%y").upper() if wk else None
    info["monthly_expiry"] = mo.strftime("%d%b%y").upper() if mo else None
    if "walls" in cfg.opt:
        wall_pair(opts, "", "cw", "pw", 12)
        if wk: wall_pair([o for o in opts if o["exp"] == wk], "w", "cwx", "pwx", 14)
        if mo and mo != wk: wall_pair([o for o in opts if o["exp"] == mo], "m", "cwx", "pwx", 15)
    if "maxpain" in cfg.opt:
        for e, sfx in ((wk, "w"), (mo, "m")):
            if e is None or (sfx == "m" and mo == wk): continue
            mp = max_pain([o for o in opts if o["exp"] == e])
            if mp:
                add("MP" + sfx, "MP" + sfx, mp, f"MP{sfx} {fmt_k(mp)} {e.strftime('%d%b').upper().lstrip('0')}", "mp", 16)
                info["maxpain_" + sfx] = mp; used.add(mp)
    sign_call = -1.0 if cfg.gex_convention == "short-calls" else 1.0
    if "gex" in cfg.opt:
        gk = {}
        for o in opts:
            if o["oi"] <= 0 or o["iv"] <= 0: continue
            g = gamma_b76(o["F"], o["K"], o["iv"], o["T"])
            gk[o["K"]] = gk.get(o["K"], 0.0) + (sign_call if o["cp"] == "C" else -sign_call) * o["oi"] * g * S0 * S0 * 0.01
        near = {k: v for k, v in gk.items() if abs(k / S0 - 1) <= cfg.max_dist}
        pos = sorted((kv for kv in near.items() if kv[1] > 0), key=lambda kv: -kv[1])[:cfg.gex_n]
        neg = sorted((kv for kv in near.items() if kv[1] < 0), key=lambda kv: kv[1])[:cfg.gex_n]
        for i, (k, v) in enumerate(pos, 1): add(f"G+{i}", f"G+{i}", k, f"G+ {fmt_k(k)} ${fmt_n(v)}", "gpos", 20 + i); used.add(k)
        for i, (k, v) in enumerate(neg, 1): add(f"G-{i}", f"G-{i}", k, f"G- {fmt_k(k)} ${fmt_n(v)}", "gneg", 20 + i); used.add(k)
        # zero-gamma flip: re-price every option at hypothetical spot S, find sign change nearest to spot
        grid = [S0 * (0.70 + 0.0025 * i) for i in range(int(0.60 / 0.0025) + 1)]
        vals = [total_gex(opts, s, S0, sign_call) for s in grid]
        roots = []
        for i in range(len(grid) - 1):
            if vals[i] == 0 or vals[i] * vals[i + 1] < 0:
                x = grid[i] if vals[i] == 0 else grid[i] - vals[i] * (grid[i + 1] - grid[i]) / (vals[i + 1] - vals[i])
                roots.append(x)
        info["net_gex_at_spot_usd_per_1pct"] = total_gex(opts, S0, S0, sign_call)
        info["gex_flip_candidates"] = [round(r, 1) for r in roots]
        if roots:
            fl = min(roots, key=lambda r: abs(r - S0)); info["gex_flip"] = round(fl, 1)
            add("GF", "GF", fl, f"GF {fmt_k(round(fl, -1)) if fl % 1000 else fmt_k(fl)}", "gflip", 11)
        else:
            info["gex_flip"] = None
    if "activity" in cfg.opt:
        ref = ref_snapshot(state, now, cfg.doi_min_age_h)
        info["doi_ref_age_h"] = round(ref[0], 1) if ref else None
        rows = []
        for k, v in by_k.items():
            if abs(k / S0 - 1) > cfg.max_dist: continue
            oc, op = agg(v, "C", "oi"), agg(v, "P", "oi")
            vc, vp = agg(v, "C", "vol"), agg(v, "P", "vol")
            if oc + op < cfg.min_oi and vc + vp < cfg.min_oi: continue
            doi = None
            if ref:
                r = ref[1]["oi"]; doi = sum(o["oi"] - r.get(o["name"], 0.0) for o in v)
            rows.append(dict(K=k, oc=oc, op=op, vc=vc, vp=vp, doi=doi))
        if rows:
            mo_, mv_ = max(r["oc"] + r["op"] for r in rows) or 1, max(r["vc"] + r["vp"] for r in rows) or 1
            md_ = max((max(r["doi"], 0) for r in rows if r["doi"] is not None), default=0) or 1
            for r in rows:
                r["score"] = (cfg.w_oi * (r["oc"] + r["op"]) / mo_ + cfg.w_vol * (r["vc"] + r["vp"]) / mv_ +
                              (cfg.w_doi * max(r["doi"], 0) / md_ if r["doi"] is not None else 0))
            rows.sort(key=lambda r: -r["score"])
            if cfg.act_exclude_labelled: rows = [r for r in rows if r["K"] not in used]
            top = rows[:cfg.act_n]
            info["activity_ranked"] = []
            for i, r in enumerate(top, 1):
                side = "C" if (r["oc"] + r["vc"]) >= (r["op"] + r["vp"]) else "P"
                oi_, vol_ = r["oc"] + r["op"], r["vc"] + r["vp"]
                parts = [f"{side}{fmt_k(r['K'])}", f"OI{fmt_n(oi_)}"]
                if vol_ >= 1: parts.append(f"V{fmt_n(vol_)}")
                full = " ".join(parts)
                if r["doi"] is not None and abs(r["doi"]) >= max(10, 0.05 * oi_):
                    full = " ".join(parts[:2] + [f"{r['doi']:+.0f}"] + parts[2:])
                tag = f"{fmt_k(r['K'])}{side}"
                add(f"A{i}", tag, r["K"], full, "actc" if side == "C" else "actp", 80 + i)
                info["activity_ranked"].append(dict(rank=i, strike=r["K"], side=side, oi=round(oi_, 1), vol24h=round(vol_, 1),
                                                    doi=None if r["doi"] is None else round(r["doi"], 1), score=round(r["score"], 3), label=full))
    return L, info, opts


# ============================================================================== extras: delivery prices
def settlement_levels(cfg):
    res = http_json("get_delivery_prices", index_name="btc_usd", count=75)["data"]
    L, info = [], {}
    fri = [(datetime.strptime(d["date"], "%Y-%m-%d"), d["delivery_price"]) for d in res]
    fri = [x for x in fri if x[0].weekday() == 4]
    if fri:
        d, p = fri[0]; info["last_friday_settlement"] = (d.strftime("%Y-%m-%d"), p)
        L.append(dict(key="SF", tag="SF", price=p, rich=None if cfg.label_style == "short" else f"SF {d.strftime('%d%b').upper().lstrip('0')}",
                      style="settle", prio=62, group="extras"))
    mon = [x for x in fri if (x[0] + timedelta(days=7)).month != x[0].month]
    if mon and (not fri or mon[0] != fri[0]):
        d, p = mon[0]; info["last_monthly_settlement"] = (d.strftime("%Y-%m-%d"), p)
        L.append(dict(key="SM", tag="SM", price=p, rich=None if cfg.label_style == "short" else f"SM {d.strftime('%d%b').upper().lstrip('0')}",
                      style="settle", prio=63, group="extras"))
    return L, info


# ============================================================================== assemble
def label_for(lv, style, maxlen):
    if style == "short" or not lv.get("rich"): return lv["tag"]
    s = lv["rich"]
    if len(s) <= maxlen: return s
    toks = s.split(" ")
    while len(toks) > 2 and len(" ".join(toks)) > maxlen:
        # drop the least important token (volume, then change, then OI) before giving up
        for dropper in (lambda t: t.startswith("V"), lambda t: t[0] in "+-" and t[1:].isdigit(), lambda t: t.startswith("OI")):
            idx = [i for i, t in enumerate(toks) if i > 0 and dropper(t)]
            if idx: toks.pop(idx[-1]); break
        else: break
    s = " ".join(toks)
    return s if len(s) <= maxlen else lv["tag"]


def merge_levels(levels, tick, style, maxlen, gap=0.0):
    """Collapse levels that would hide each other into one note per cluster.

    gap <= 0  : old behaviour - only levels inside the same price tick (`--tick`) are merged; the lower `prio`
                number wins (colour / label order); price = mean of the top-prio levels.
    gap  > 0  : (`--merge-gap`) importance-driven clustering.  Take the most important unassigned level
                (IMPORTANCE order) as an anchor, absorb every unassigned level within `gap` USD of the anchor's
                price, repeat.  The note sits at the anchor's price with the anchor's colour; the label is the
                tags joined with '/' in importance order.  Result: any two notes are > gap apart."""
    if gap <= 0:
        buckets = {}
        for lv in levels: buckets.setdefault(round(lv["price"] / tick), []).append(lv)
        groups = []
        for items in buckets.values():
            items.sort(key=lambda x: x["prio"])
            top = items[0]
            same = [i["price"] for i in items if i["prio"] == top["prio"]]
            groups.append((top, items, sum(same) / len(same)))
    else:
        radius = max(gap, tick)
        order = sorted(levels, key=lambda x: (IMPORTANCE_RANK.get(x["key"], 10_000), x["prio"], x["price"]))
        left, groups = list(order), []
        while left:
            top = left[0]
            items = [x for x in left if abs(x["price"] - top["price"]) <= radius]   # includes top, in importance order
            left = [x for x in left if abs(x["price"] - top["price"]) > radius]
            groups.append((top, items, top["price"]))
    out = []
    for top, items, price in groups:
        if len(items) == 1: label = label_for(top, style, maxlen)
        elif gap > 0: label = join_tags([i["tag"] for i in items], maxlen)
        else:                                                   # old behaviour (--merge-gap 0)
            tags = [i["tag"] for i in items]
            label = "/".join(tags)
            if len(label) > maxlen: label = "/".join(tags[:2]) + f"+{len(tags) - 2}"
        out.append(dict(top, price=price, label=label, members=[i["key"] for i in items]))
    out.sort(key=lambda x: -x["price"])
    return out


def join_tags(tags, maxlen):
    """'a/b/c' if it fits in maxlen, else the most important tags that fit + '+N' (at least one tag is kept)."""
    label = "/".join(tags)
    if len(label) <= maxlen: return label
    for k in range(len(tags) - 1, 0, -1):
        cand = "/".join(tags[:k]) + f"+{len(tags) - k}"
        if len(cand) <= maxlen: return cand
    return f"{tags[0]}+{len(tags) - 1}"


def to_row(alias, lv, cfg, alert=False):
    fg, bg, hl = STYLES[lv["style"]]
    if cfg.no_lines or lv["style"] in cfg.no_lines_for: hl = ""
    notif = [""] * 6
    if alert: notif = ["true", "", "", "", "", ALERT_SOUNDS.get(lv["top_key"] if "top_key" in lv else lv["key"], "")]
    return [alias, f"{lv['price']:.1f}", lv["label"], fg, bg, "left"] + notif + [hl]


def write_csv(path, rows, gz=False):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        w.writerow(HDR); [w.writerow(r) for r in rows]
    if gz:
        with open(tmp, "rb") as a, gzip.open(path + ".gz", "wb") as b: b.write(a.read())
    os.replace(tmp, path)


def verify(path, labelmax=None, mingap=0.0):
    """Validate a notes CSV against the Bookmap v8 export convention. Returns (ok, messages)."""
    import re
    msgs = []
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"): msgs.append("BOM present")
    if b"\r\n" not in raw: msgs.append("not CRLF")
    rows = list(csv.reader(raw.decode("utf-8").splitlines(), delimiter=","))
    if rows[0] != HDR: msgs.append("header mismatch")
    seen, seen_p = {}, []
    for i, r in enumerate(rows[1:], 2):
        if len(r) != 13: msgs.append(f"line {i}: {len(r)} columns"); continue
        try:
            p = float(r[1]); assert math.isfinite(p) and p > 0
        except Exception: msgs.append(f"line {i}: bad price {r[1]!r}")
        for j in (3, 4):
            if not re.fullmatch(r"#[0-9a-f]{6}", r[j]): msgs.append(f"line {i}: bad colour {r[j]!r}")
        if r[5] not in ("left", "center", "right"): msgs.append(f"line {i}: alignment {r[5]!r}")
        if r[12] not in ("", "true"): msgs.append(f"line {i}: hline {r[12]!r}")
        if not r[2]: msgs.append(f"line {i}: empty note")
        if labelmax and len(r[2]) > labelmax: msgs.append(f"line {i}: label too long {r[2]!r}")
        if mingap > 0:
            for (a_, p_), l_ in seen_p:
                if a_ == r[0] and abs(p_ - p) <= mingap: msgs.append(f"line {i}: within {mingap:g} of line {l_} for {r[0]} (merge-gap)")
            seen_p.append(((r[0], p), i))
        key = (r[0], round(p))
        if key in seen: msgs.append(f"line {i}: price {r[1]} collides with line {seen[key]} for {r[0]} (Bookmap keeps only the last per tick)")
        seen[key] = i
    return (not msgs), msgs, len(rows) - 1


# ============================================================================== main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"))
    ap.add_argument("--symbols", nargs="*", help="ALIAS=DERIBIT_INSTRUMENT pairs (default: the 6 BTC tabs)")
    ap.add_argument("--groups", default="day,week,month,quarter,year,vwap,profile",
                    help="candle-level families to include ('extras' = settlement levels SF/SM, same as --settlements)")
    ap.add_argument("--vp-bin", type=float, default=50.0, help="volume-profile bucket in USD (POC / value area)")
    ap.add_argument("--vp-va", type=float, default=0.70, help="value-area fraction of volume")
    ap.add_argument("--opt", default="walls,maxpain,gex,activity", help="option families ('' to disable all)")
    ap.add_argument("--act-n", type=int, default=8, help="top N most active strikes (OI + volume [+OI change])")
    ap.add_argument("--w-oi", type=float, default=1.0); ap.add_argument("--w-vol", type=float, default=1.0)
    ap.add_argument("--w-doi", type=float, default=0.5, help="weight of OI increase vs ~24h-old snapshot (needs >=1 earlier run)")
    ap.add_argument("--doi-min-age-h", type=float, default=6.0)
    ap.add_argument("--min-oi", type=float, default=50.0, help="min BTC (OI or 24h volume) for an activity strike")
    ap.add_argument("--act-exclude-labelled", type=int, default=1, help="skip strikes already shown as wall/MP/GEX")
    ap.add_argument("--max-dist", type=float, default=0.25, help="strikes beyond +-this fraction of spot are ignored")
    ap.add_argument("--gex-n", type=int, default=2, help="largest +GEX and -GEX strikes to show (each)")
    ap.add_argument("--gex-convention", choices=["short-calls", "long-calls"], default="short-calls",
                    help="dealer positioning ASSUMPTION: short-calls = dealers short calls/long puts (default)")
    ap.add_argument("--label-style", choices=["rich", "short"], default="rich")
    ap.add_argument("--label-max", type=int, default=18, help="max chars of a rich label (falls back to the short tag)")
    ap.add_argument("--tick", type=float, default=1.0, help="levels closer than this are merged into one note")
    ap.add_argument("--merge-gap", type=float, default=100.0,
                    help="USD: levels of one symbol within this distance become ONE note at the most important level's "
                         "price/colour, label = tags joined with '/' by importance (see README). 0 = old behaviour (merge only within --tick)")
    ap.add_argument("--settlements", action="store_true", help="re-enable the Deribit settlement levels SF / SM (off by default)")
    ap.add_argument("--tick-map", nargs="*", default=[], help="ALIAS=TICK overrides (e.g. 'BTC-USDT:MB:SP@BMD=10')")
    ap.add_argument("--alerts", choices=["separate", "inline", "off"], default="separate")
    ap.add_argument("--alert-levels", default="pWH,pWL")
    ap.add_argument("--no-lines", action="store_true", help="never draw horizontal lines")
    ap.add_argument("--no-lines-for", default="", help="comma list of style families without a line, e.g. 'actc,actp,mid' (keys of _BG)")
    ap.add_argument("--gzip", action="store_true", help="also write .csv.gz")
    ap.add_argument("--now", help="override UTC time, ISO (testing)")
    ap.add_argument("--verify", metavar="CSV", help="only validate a CSV and exit")
    cfg = ap.parse_args()
    if cfg.verify:
        ok, msgs, n = verify(cfg.verify); print("OK" if ok else "PROBLEMS", n, "rows"); [print(" ", m) for m in msgs]; sys.exit(0 if ok else 1)
    cfg.no_lines_for = {g.strip() for g in cfg.no_lines_for.split(",") if g.strip()}
    cfg.groups = {g.strip() for g in cfg.groups.split(",") if g.strip()}
    cfg.opt = {g.strip() for g in cfg.opt.split(",") if g.strip()}
    symbols = dict(DEFAULT_SYMBOLS) if not cfg.symbols else dict(s.split("=", 1) for s in cfg.symbols)
    ticks = {a: float(t) for a, t in (s.split("=", 1) for s in cfg.tick_map)}
    now = datetime.fromisoformat(cfg.now).replace(tzinfo=UTC) if cfg.now else datetime.now(UTC)
    P = periods(now)
    t0 = P["pY"][0]

    # --- candle levels (one fetch per distinct Deribit instrument)
    per_inst, stats = {}, {}
    for inst in sorted(set(symbols.values())):
        cs = fetch_candles(inst, t0, now + timedelta(hours=1))
        c1m = None
        if cfg.groups & {"vwap", "profile"}:       # 1m candles from the start of the previous week
            c1m = fetch_candles(inst, P["pW"][0], now + timedelta(minutes=2), resolution=1)
            print(f"  candles {inst}: {len(c1m)} x 1m", file=sys.stderr)
        per_inst[inst], stats[inst] = candle_levels(cs, now, cfg, c1m)
        print(f"  candles {inst}: {len(cs)} x 1h", file=sys.stderr)
    # --- options (shared by all symbols: strikes are index-USD levels)
    opt_L, opt_info, opts = [], {}, []
    if cfg.opt:
        state_path = os.path.join(cfg.out, "state", "oi_snapshots.json")
        state = load_state(state_path)
        opt_L, opt_info, opts = option_levels(now, cfg, state)
        save_state(state_path, state, now, opts)
    extra_L, extra_info = ([], {})
    if cfg.settlements or "extras" in cfg.groups:        # SF / SM are off by default (--settlements re-enables)
        extra_L, extra_info = settlement_levels(cfg)

    rows, arows, report = [], [], {}
    alert_keys = {k.strip() for k in cfg.alert_levels.split(",") if k.strip()}
    for alias, inst in symbols.items():
        levels = per_inst[inst] + opt_L + extra_L
        tick = ticks.get(alias, cfg.tick)
        merged = merge_levels(levels, tick, cfg.label_style, cfg.label_max, cfg.merge_gap)
        report[alias] = dict(source=inst, levels=[dict(label=m["label"], price=round(m["price"], 1), members=m["members"]) for m in merged])
        if cfg.alerts == "separate":
            # alert rows are built from the level itself (own price/colour), independent of where a merged note sits
            for lv in levels:
                if lv["key"] in alert_keys:
                    arows.append(to_row(alias, dict(lv, label=lv["key"], top_key=lv["key"]), cfg, alert=True))
        for m in merged:
            is_alert = cfg.alerts == "inline" and m["members"][0] in alert_keys
            if is_alert: m = dict(m, top_key=m["members"][0])
            rows.append(to_row(alias, m, cfg, alert=is_alert))
    os.makedirs(cfg.out, exist_ok=True)
    main_path = os.path.join(cfg.out, "bm_levels.csv")
    write_csv(main_path, rows, cfg.gzip)
    if arows: write_csv(os.path.join(cfg.out, "bm_levels_alerts.csv"), arows, cfg.gzip)
    for alias in symbols:
        sub = [r for r in rows if r[0] == alias] + ([r for r in arows if r[0] == alias] if cfg.alerts == "inline" else [])
        safe = alias.replace("@", "_").replace(":", "_")
        write_csv(os.path.join(cfg.out, "per_symbol", f"{safe}_levels.csv"), sub)
    meta = dict(generated_utc=now.strftime("%Y-%m-%d %H:%M:%S"), spot_index=opt_info.get("spot"),
                periods={k: [a.isoformat(), b.isoformat()] for k, (a, b) in P.items()},
                options=opt_info, settlements=extra_info,
                stats={i: {k: (None if v is None else {a: (round(b, 1) if isinstance(b, float) else b) for a, b in v.items()}) for k, v in s.items()}
                       for i, s in stats.items()},
                symbols=report)
    with open(os.path.join(cfg.out, "levels_report.json"), "w") as fh: json.dump(meta, fh, indent=1, default=str)
    with open(os.path.join(cfg.out, "levels_summary.txt"), "w") as fh:
        fh.write(f"Generated {meta['generated_utc']} UTC   index spot {opt_info.get('spot')}\n")
        for alias, rp in report.items():
            fh.write(f"\n== {alias}  (candle source: {rp['source']})\n")
            for l in rp["levels"]: fh.write(f"{l['price']:>12,.1f}  {l['label']:<20} {','.join(l['members'])}\n")
    bad = 0
    for p in [main_path] + ([os.path.join(cfg.out, "bm_levels_alerts.csv")] if arows else []):
        ok, msgs, n = verify(p, cfg.label_max if cfg.label_style == "rich" else None,
                             cfg.merge_gap if p == main_path else 0.0)
        print(f"{'OK ' if ok else 'BAD'} {p}: {n} rows"); [print("   ", m) for m in msgs]; bad += not ok
    for k, (fg, bg, _) in STYLES.items():
        if contrast(fg, bg) < 4.5: print(f"  note: style {k} contrast {contrast(fg, bg):.1f}", file=sys.stderr)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
