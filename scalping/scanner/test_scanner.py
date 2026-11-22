"""python -m pytest scalping/scanner/test_scanner.py -q"""
from datetime import datetime
from zoneinfo import ZoneInfo
from scalping.scanner.pillars import Candidate, score, session_fraction
from scalping.scanner.floatdata import parse_finviz, parse_yahoo, _num
from scalping.scanner.news import tickers_in, parse_feed, News

NY = ZoneInfo("America/New_York")
T = datetime(2024, 8, 21, 9, 45, tzinfo=NY)


def test_all_five():
    c = Candidate("VRAX", last=4.2, prev_close=2.0, volume=9e6, avg_volume=3e5, float_shares=3e6,
                  news=[{"time": "x", "source": "s", "title": "t"}])
    r = score(c, T)
    assert r["all5"] and r["pillars"] == 5 and round(r["change_pct"]) == 110


def test_each_pillar_fails():
    base = dict(last=4.2, prev_close=2.0, volume=9e6, avg_volume=3e5, float_shares=3e6, news=[{"t": 1}])
    for k, v, pillar in (("last", 25.0, "price"), ("prev_close", 4.0, "change"), ("volume", 1e5, "rvol"),
                         ("float_shares", 15e6, "float"), ("news", [], "news")):
        r = score(Candidate("X", **{**base, k: v}), T)
        assert not r["checks"][pillar] and not r["all5"], pillar
    assert score(Candidate("X", **{**base, "float_shares": 15e6}), T)["near_float"]


def test_session_fraction_monotonic():
    xs = [session_fraction(datetime(2024, 8, 21, h, m, tzinfo=NY)) for h, m in ((3, 0), (7, 0), (9, 29), (9, 31), (12, 0), (16, 0))]
    assert xs == sorted(xs) and xs[0] == 0.05 and xs[-1] == 1.0 and 0.08 < xs[2] < 0.11


def test_missing_data_is_not_a_hit():
    r = score(Candidate("X"), T)
    assert r["pillars"] == 0 and r["rvol_tod"] is None and r["change_pct"] is None


def test_float_parsers():
    assert _num("5.23M") == 5.23e6 and _num("1,234") == 1234 and _num("0.9B") == 9e8
    assert parse_finviz('<td class="snapshot-td2">Shs Float</td><td class="snapshot-td2"><b>4.81M</b></td>') == 4.81e6
    assert parse_yahoo('<td>Float</td> <td class="value">12.3M</td>') == 12.3e6


def test_news_tickers_and_feed():
    assert tickers_in("Acme Bio (NASDAQ: ACME) announces FDA approval") == {"ACME"}
    assert tickers_in("Foo Corp (NYSE American: FOO) and Bar (Nasdaq: BAR)") == {"FOO", "BAR"}
    rss = """<rss><channel><item><title>Zapp (NASDAQ: ZAPP) launches i300</title><description>x</description>
             <pubDate>Thu, 13 Jun 2024 12:00:00 GMT</pubDate></item></channel></rss>"""
    items = parse_feed(rss, "GlobeNewswire")
    n = News(); n.ingest(items, "GlobeNewswire")
    assert n.by_sym["ZAPP"][0]["title"].startswith("Zapp")
    n.ingest(items, "GlobeNewswire"); assert len(n.by_sym["ZAPP"]) == 1        # no duplicates


def test_sec_atom_maps_cik():
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>8-K - GeoVax Labs, Inc. (0000832489) (Filer)</title>
              <summary>Item 8.01</summary><updated>2024-08-19T08:00:00-04:00</updated></entry></feed>"""
    n = News(); n.cik2sym = {"0000832489": "GOVX"}
    n.ingest(parse_feed(atom, "SEC 8-K"), "SEC 8-K")
    assert "GOVX" in n.by_sym
