"""Catalyst detection from free sources: press-release wires (RSS) and SEC 8-K filings, plus IBKR news ticks.

Press releases name the ticker as "(NASDAQ: ABCD)" / "(NYSE American: ABC)"; SEC filings are mapped to tickers
with the SEC's free CIK->ticker file.
"""
import re, time, json, threading, logging, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from . import config as C

log = logging.getLogger("scanner.news")
TICKER_RX = re.compile(r"\((?:NASDAQ|Nasdaq|NYSE American|NYSE|NYSE Arca|Cboe|AMEX)\s*(?:CM|GM|GS)?\s*:\s*([A-Z]{1,5}(?:\.[A-Z])?)\)")
CIK_RX = re.compile(r"\((\d{10})\)")


def tickers_in(text):
    return {m.group(1) for m in TICKER_RX.finditer(text or "")}


def _parse_time(s):
    if not s: return datetime.now(timezone.utc)
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return datetime.now(timezone.utc)


def parse_feed(xml_text, source):
    """Return [{time, source, title, text}] from RSS or Atom."""
    out = []
    root = ET.fromstring(xml_text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item") or root.findall(".//a:entry", ns)
    for it in items:
        g = lambda tag: (it.findtext(tag) or it.findtext(f"a:{tag}", namespaces=ns) or "")
        title, desc = g("title").strip(), (g("description") or g("summary")).strip()
        when = _parse_time(g("pubDate") or g("updated") or g("published"))
        out.append(dict(time=when.isoformat(), source=source, title=title, text=desc))
    return out


class News:
    def __init__(self):
        self.by_sym = {}            # sym -> list of headlines (newest first)
        self.cik2sym = {}
        self.seen = set()
        self.lock = threading.Lock()

    def _get(self, url):
        hdr = {"User-Agent": C.SEC_USER_AGENT if "sec.gov" in url else "Mozilla/5.0 APEX scanner"}
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=10) as r:
            return r.read().decode("utf-8", "ignore")

    def load_cik_map(self):
        try:
            d = json.loads(self._get("https://www.sec.gov/files/company_tickers.json"))
            self.cik2sym = {str(v["cik_str"]).zfill(10): v["ticker"] for v in d.values()}
        except Exception as e:
            log.warning("SEC ticker map unavailable: %s", e)

    def add(self, sym, item):
        key = (sym, item["title"][:120])
        with self.lock:
            if key in self.seen: return
            self.seen.add(key)
            self.by_sym.setdefault(sym, []).insert(0, {k: item[k] for k in ("time", "source", "title")})

    def ingest(self, items, source):
        for it in items:
            syms = tickers_in(it["title"] + " " + it["text"])
            if source.startswith("SEC"):
                m = CIK_RX.search(it["title"])
                if m and m.group(1) in self.cik2sym: syms.add(self.cik2sym[m.group(1)])
            for s in syms: self.add(s, it)

    def poll_once(self):
        for name, url in C.RSS_FEEDS.items():
            try:
                self.ingest(parse_feed(self._get(url), name), name)
            except Exception as e:
                log.debug("feed %s failed: %s", name, e)

    def start(self):
        def run():
            self.load_cik_map()
            while True:
                self.poll_once(); time.sleep(C.NEWS_POLL_SEC)
        threading.Thread(target=run, daemon=True).start()

    def recent(self, sym, now=None):
        now = now or datetime.now(timezone.utc)
        cut = now - timedelta(hours=C.NEWS_LOOKBACK_H)
        with self.lock:
            return [n for n in self.by_sym.get(sym, []) if datetime.fromisoformat(n["time"]) >= cut]
