"""
Download euro area market data from the ECB Data Portal and write data/market.json
for mariobajo.github.io. Standard library only, so it runs on any GitHub runner.

Usage: python scripts/fetch_market.py data/market.json [url_of_previous_json]

Each block is fetched independently. If a block fails, the value from the previously
published JSON (if reachable) is kept, so the site never loses its last good data.
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.parse
import urllib.request

BASE = "https://data-api.ecb.europa.eu/service/data/"
TENORS = ["3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"]
YEARS = [0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def ecb(flow, key, **params):
    """Return {series_suffix: [(date, value), ...]} sorted by date."""
    params["format"] = "csvdata"
    url = f"{BASE}{flow}/{key}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "text/csv", "User-Agent": "mariobajo.github.io data job"})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8")
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        v = (row.get("OBS_VALUE") or "").strip()
        d = (row.get("TIME_PERIOD") or "").strip()
        k = (row.get("KEY") or "").strip()
        if not v or not d or not k:
            continue
        try:
            out.setdefault(k.split(".")[-1], []).append((d, float(v)))
        except ValueError:
            continue
    for k in out:
        out[k].sort()
    if not out:
        raise RuntimeError(f"no observations from {url}")
    return out


def last_two(series):
    s = series[-2:]
    if len(s) == 1:
        return s[0], None
    return s[1], s[0]


def curve_block():
    start = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    aaa = ecb("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM." + "+".join("SR_" + t for t in TENORS), startPeriod=start)
    by = {t: dict(aaa.get("SR_" + t, [])) for t in TENORS}
    common = sorted(set.intersection(*(set(v) for v in by.values())))
    if not common:
        raise RuntimeError("no common date across tenors")
    d0 = common[-1]
    target = (dt.date.fromisoformat(d0) - dt.timedelta(days=365)).isoformat()
    older = [d for d in common if d <= target]
    dprev = older[-1] if older else common[0]
    d1 = common[-2] if len(common) > 1 else d0
    curve = {
        "date": d0,
        "tenors": YEARS,
        "labels": TENORS,
        "y": [round(by[t][d0], 4) for t in TENORS],
        "prev": {"date": dprev, "y": [round(by[t][dprev], 4) for t in TENORS]},
    }
    two, ten = by["2Y"], by["10Y"]
    tape = {
        "aaa2": {"v": round(two[d0], 3), "d": round((two[d0] - two[d1]) * 100, 1), "kind": "pct_bp", "date": d0},
        "aaa10": {"v": round(ten[d0], 3), "d": round((ten[d0] - ten[d1]) * 100, 1), "kind": "pct_bp", "date": d0},
        "s2s10": {"v": round((ten[d0] - two[d0]) * 100, 1),
                  "d": round(((ten[d0] - two[d0]) - (ten[d1] - two[d1])) * 100, 1), "kind": "bp", "date": d0},
    }
    return curve, tape, ten


def spread_block(aaa10):
    allr = dict(ecb("YC", "B.U2.EUR.4F.G_N_C.SV_C_YM.SR_10Y", lastNObservations=15)["SR_10Y"])
    common = sorted(set(allr) & set(aaa10))
    if len(common) < 2:
        raise RuntimeError("spread: not enough common dates")
    d0, d1 = common[-1], common[-2]
    s0, s1 = allr[d0] - aaa10[d0], allr[d1] - aaa10[d1]
    return {"v": round(s0 * 100, 1), "d": round((s0 - s1) * 100, 1), "kind": "bp", "date": d0}


def simple(flow, keys, kind, scale_change):
    last_err = None
    for key in keys:
        try:
            data = ecb(flow, key, lastNObservations=5)
            series = next(iter(data.values()))
            (d0, v0), prev = last_two(series)
            chg = 0.0 if prev is None else (v0 - prev[1]) * scale_change if kind != "fx" else (v0 / prev[1] - 1) * 100
            return {"v": round(v0, 4), "d": round(chg, 2), "kind": kind, "date": d0}
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"{flow} failed: {last_err}")


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "data/market.json"
    prev_url = sys.argv[2] if len(sys.argv) > 2 else None
    old = {}
    if prev_url:
        try:
            with urllib.request.urlopen(prev_url, timeout=30) as r:
                old = json.loads(r.read().decode("utf-8"))
            log("previous json loaded")
        except Exception as e:  # noqa: BLE001
            log(f"no previous json: {e}")
    res = {"source": "ECB Data Portal", "curve": old.get("curve"), "tape": dict(old.get("tape") or {})}
    ok = 0
    aaa10 = None
    try:
        curve, tape, aaa10 = curve_block()
        res["curve"] = curve
        res["tape"].update(tape)
        ok += 1
    except Exception as e:  # noqa: BLE001
        log(f"curve failed: {e}")
    if aaa10:
        try:
            res["tape"]["spr10"] = spread_block(aaa10)
            ok += 1
        except Exception as e:  # noqa: BLE001
            log(f"spread failed: {e}")
    jobs = {
        "dfr": ("FM", ["D.U2.EUR.4F.KR.DFR.LEV", "B.U2.EUR.4F.KR.DFR.LEV"], "pct_bp", 100),
        "estr": ("EST", ["B.EU000A2X2A25.WT"], "pct_bp", 100),
        "eurusd": ("EXR", ["D.USD.EUR.SP00.A"], "fx", 1),
    }
    for name, (flow, keys, kind, sc) in jobs.items():
        try:
            res["tape"][name] = simple(flow, keys, kind, sc)
            ok += 1
        except Exception as e:  # noqa: BLE001
            log(f"{name} failed: {e}")
    if not res["curve"] and not res["tape"]:
        log("nothing to write")
        return 0
    res["updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
    log(f"wrote {out_path} ({ok} blocks refreshed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
