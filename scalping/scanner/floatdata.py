"""Float (shares available to trade) from free sources, cached for a day.

Primary: Finviz quote page ("Shs Float"). Fallback: Yahoo quote page ("Float"). Floats change rarely, so a daily
value is fine. Failures are cached briefly so a missing symbol isn't hammered every cycle.
"""
import json, os, re, time, threading, logging, urllib.request
from . import config as C

log = logging.getLogger("scanner.float")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
_lock = threading.Lock()


def _num(s):
    s = s.strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([KMBT]?)$", s, re.I)
    if not m: return None
    mult = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[m.group(2).upper()]
    return float(m.group(1)) * mult


def parse_finviz(html):
    m = re.search(r"Shs Float</td>\s*<td[^>]*>(?:<[^>]+>)*\s*([\d.,]+[KMB]?)", html)
    return _num(m.group(1)) if m else None


def parse_yahoo(html):
    m = re.search(r"Float\s*(?:<[^>]+>\s*)+([\d.,]+[KMB])", html)
    return _num(m.group(1)) if m else None


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


class FloatCache:
    def __init__(self, path=C.FLOAT_CACHE):
        self.path = path
        try:
            self.d = json.load(open(path))
        except (OSError, ValueError):
            self.d = {}

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        json.dump(self.d, open(self.path, "w"))

    def get(self, sym):
        """Cached value or None (never blocks)."""
        e = self.d.get(sym)
        if not e: return None
        return e.get("float")

    def stale(self, sym):
        e = self.d.get(sym)
        if not e: return True
        age_h = (time.time() - e["t"]) / 3600
        return age_h > (C.FLOAT_MAX_AGE_H if e.get("float") else 1)   # retry failures after 1 h

    def fetch(self, sym):
        val, src = None, None
        for name, url, parse in (("finviz", f"https://finviz.com/quote.ashx?t={sym}", parse_finviz),
                                 ("yahoo", f"https://finance.yahoo.com/quote/{sym}/key-statistics/", parse_yahoo)):
            try:
                val = parse(_get(url))
            except Exception as e:  # network, blocked, layout change
                log.debug("float %s via %s failed: %s", sym, name, e)
            if val: src = name; break
        with _lock:
            self.d[sym] = {"float": val, "src": src, "t": time.time()}
            self._save()
        return val

    def refresh_async(self, symbols):
        todo = [s for s in symbols if self.stale(s)]
        if not todo: return
        def run():
            for s in todo:
                self.fetch(s); time.sleep(0.5)     # be polite
        threading.Thread(target=run, daemon=True).start()
