"""Small-cap momentum scanner settings (Ross Cameron's 5 pillars of stock selection).

Pillars (his slide "Indicators of High Demand and Low Supply"):
  1) Demand: 5x relative volume        2) Demand: already up 10% on the day
  3) Demand: a catalyst (news)          4) Demand: price $1-20
  5) Supply: less than 10 million shares available to trade (float)
"""
import os

# ---- pillars
MIN_RVOL = 5.0            # relative volume (see rvol.py for how it is measured)
MIN_CHANGE_PCT = 10.0     # % up vs previous close (pre-market included)
MIN_PRICE, MAX_PRICE = 1.0, 20.0
MAX_FLOAT = 10_000_000    # ideal; floats up to SOFT_MAX_FLOAT are shown as "near" (he also trades 10-20M)
SOFT_MAX_FLOAT = 20_000_000
NEWS_LOOKBACK_H = 24      # a headline in the last 24 h counts as a catalyst

# ---- IBKR (paper or live login both work; the scanner never places orders)
IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", "4002"))       # 4002 Gateway paper, 4001 Gateway live, 7497/7496 TWS
IB_CLIENT_ID = int(os.environ.get("IB_SCANNER_CLIENT_ID", "73"))
SCAN_CODES = ("TOP_PERC_GAIN", "HOT_BY_VOLUME")        # IB server-side scans, merged
SCAN_ROWS = 50                                         # IB maximum per scan
MAX_LIVE_LINES = 60                                    # candidates with live quotes (IB default limit is 100 lines)

# ---- free sources
FLOAT_CACHE = os.environ.get("SCANNER_FLOAT_CACHE", "scalping_data/scanner/float_cache.json")
FLOAT_MAX_AGE_H = 24
NEWS_POLL_SEC = 30
RSS_FEEDS = {
    "GlobeNewswire": "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
    "PR Newswire": "https://www.prnewswire.com/rss/news-releases-list.rss",
    "Business Wire": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFtRWA==",
    "ACCESSWIRE": "https://www.accesswire.com/users/newswire/rss.ashx",
    "SEC 8-K": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=100&output=atom",
}
# SEC asks automated clients to identify themselves; set your own contact in the environment
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "APEX scanner (set SEC_USER_AGENT to name + email)")

# ---- dashboard
HTTP_PORT = int(os.environ.get("SCANNER_PORT", "8765"))
REFRESH_SEC = 2
