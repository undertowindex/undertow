import os
import sys, os, json, tempfile, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["UNDERTOW_CACHE_DIR"] = tempfile.mkdtemp()
os.environ.pop("FRED_API_KEY", None)
os.environ["FED_TARGET_RATE"] = "3.875"
import undertow_feeds as F
import undertow as U

class R:
    def __init__(s, code=200, js=None, text=""): s.status_code=code; s._j=js; s.text=text
    def json(s): return s._j

NY = {"refRates":[{"effectiveDate":"2026-10-22","type":"SOFR","percentRate":3.90,"volumeInBillions":2945},
                  {"effectiveDate":"2026-10-21","type":"SOFR","percentRate":3.88,"volumeInBillions":2942}]}
CSV = ("Friday_of_Week,NFCI,ANFCI,Risk,Credit,Leverage,Nonfinancial_Leverage\n"
       "10/10/2025,-0.50,-0.40,0,0,0,0\n10/17/2025,-0.52,-0.45,0,0,0,0\n10/24/2025,-0.56,-0.60,0,0,0,0\n10/31/20")

def mock(mode):
    def g(url, params=None, timeout=0, headers=None):
        if mode == "blocked": raise OSError("CONNECT tunnel failed, response 403")
        if "newyorkfed" in url: return R(200, NY)
        if "chicagofed" in url: return R(200, text=CSV)
        raise OSError("blocked")
    return g

import requests
results = []
def check(name, cond): results.append((name, bool(cond))); print(("PASS " if cond else "FAIL "), name)

# 1 live path
requests.get = mock("live")
l8 = U.get_layer8(); l9 = U.get_layer9()
check("L8 live source NY Fed", l8["data"].get("sofr_source") == "NY Fed (overnight SOFR)")
check("L8 live values", l8["data"]["sofr_3m_pct"] == 3.90 and l8["data"]["sofr_volume_b"] == 2945)
check("L9 live source Chicago Fed", l9["data"].get("nfci_source") == "Chicago Fed (direct)")
check("L9 uses newest complete row, skips truncated", l9["data"]["nfci_date"] == "2025-10-24" and l9["data"]["nfci_value"] == -0.56)
check("L9 momentum + ANFCI", l9["data"]["nfci_momentum"] == -0.04 and l9["data"]["anfci_value"] == -0.60)
check("L9 CAUTION since ANFCI -0.47 is calm and NFCI -0.56 is calm => CALM", l9["data"]["nfci_status"] == "CALM" and l9["score"] == 0)
check("live layers have no degraded gap", U._layer_gap("Layer 8", l8) is None and U._layer_gap("Layer 9", l9) is None)
check("dashboard live states", U._feed_state(l8,"sofr_3m_pct","sofr_source") is True and U._feed_state(l9,"nfci_value","nfci_source") is True)

# 2 blocked, cache from the live run above (fresh)
requests.get = mock("blocked")
l8c = U.get_layer8(); l9c = U.get_layer9()
check("L8 cached labelled", l8c["data"]["sofr_source"] == "cache" and any("CACHED" in f for f in l8c["flags"]))
check("L9 cached labelled", l9c["data"]["nfci_source"] == "cache" and any("CACHED" in f for f in l9c["flags"]))
check("cached shows CACHED not LIVE", U._feed_state(l8c,"sofr_3m_pct","sofr_source") == "cached")

# 3 blocked, no cache -> honest unavailable, never invented
for p in (F.SOFR_CACHE, F.NFCI_CACHE):
    os.remove(p)
l8u = U.get_layer8(); l9u = U.get_layer9()
check("L8 unavailable has no data", l8u["data"] == {} and "unavailable" in l8u["flags"][0])
check("L9 unavailable has no data", l9u["data"] == {} and "unavailable" in l9u["flags"][0])
check("no invented -0.20 / Fed-target values", "nfci_value" not in l9u["data"] and "sofr_3m_pct" not in l8u["data"])
check("dashboard says MISSING", U._feed_state(l8u,"sofr_3m_pct","sofr_source") is False)

# 4 composite: calm total + missing core data must NOT be GREEN
calm = {"score":0,"max":5,"flags":[],"data":{"x":1}}
sc = U.compute_score(calm,calm,calm,calm,calm,calm,calm,l8u,l9u)
check("GREEN capped at AMBER when L8/L9 unavailable", sc["signal"] == "AMBER" and len(sc["data_gaps"]) == 2)
sc2 = U.compute_score(calm,calm,calm,calm,calm,calm,calm,l8,l9)
check("GREEN allowed with live L8/L9", sc2["signal"] == "GREEN")

# 5 expiry: old cache is ignored
F.write_cache(F.SOFR_CACHE, {"rate":3.9,"date":"x","volume_b":1,"source":"s"})
blob = json.load(open(F.SOFR_CACHE)); blob["saved_utc"] = (datetime.datetime.utcnow()-datetime.timedelta(days=9)).isoformat()
json.dump(blob, open(F.SOFR_CACHE,"w"))
check("expired cache ignored", F.read_cache(F.SOFR_CACHE, 5) == (None, None))

# 6 stress path still scores
CSV2 = "Friday_of_Week,NFCI,ANFCI\n10/17/2025,0.10,0.20\n10/24/2025,0.30,0.50\n"
requests.get = lambda url, params=None, timeout=0, headers=None: R(200, text=CSV2) if "chicagofed" in url else (_ for _ in ()).throw(OSError("x"))
l9s = U.get_layer9()
check("L9 WARNING scores 5 on NFCI>=0", l9s["score"] == 5 and l9s["data"]["nfci_status"] == "WARNING")

# 7 scoring rule on backtested episodes (spread vs range top)
S = F.sofr_spread_score
check("2019-09-17 +300bp -> 5", S(5.25, 2.25)[0] == 5)
check("2019-09-16 +18bp -> 2", S(2.43, 2.25)[0] == 2)
check("2025-10-31 +22bp -> 2", S(4.22, 4.00)[0] == 2)
check("2025-11-03 +13bp -> 1", S(4.13, 4.00)[0] == 1)
check("2026-10-22 3.90 vs 4.00 -> 0", S(3.90, 4.00)[0] == 0)
check("2023-03 4.55 vs 4.75 -> 0", S(4.55, 4.75)[0] == 0)
check("L8 live scores 0 today (3.90 vs 4.00 top)", l8["score"] == 0 and l8["data"]["sofr_spread_bp"] == -10.0)
requests.get = lambda url, params=None, timeout=0, headers=None: R(200, {"refRates":[{"effectiveDate":"2026-10-30","type":"SOFR","percentRate":4.22,"volumeInBillions":2800}]}) if "newyorkfed" in url else (_ for _ in ()).throw(OSError("x"))
l8s = U.get_layer8()
check("L8 +22bp squeeze scores CAUTION 2, volume not scored", l8s["score"] == 2 and l8s["data"]["sofr_status"] == "CAUTION")
check("L8 email text has no stale 3.80% thresholds", "3.80" not in U.format_layer8_for_email(l8s))

bad = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(bad)}/{len(results)} passed"); sys.exit(1 if bad else 0)
