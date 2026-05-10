"""APEX scan universe.

Two curated lists of US-listed tickers used by the daily screeners. yfinance
is fetched in batches against these lists — it has no API key, no rate limit,
and supports multi-ticker downloads.

SMALL_CAP_UNIVERSE: 300 small/micro/mid-cap names commonly known for
volatility, speculative setups, biotech catalysts, short-squeeze potential,
and retail interest. Final $10M–$2B market-cap filter is enforced at scan
time inside `screener_small_caps.py`.

LARGE_CAP_UNIVERSE: 100 large-cap S&P-style names that the big-player
screener evaluates for undervaluation. The $2B+ market-cap filter is
enforced at scan time inside `screener_big_players.py`.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 300 small/micro-cap candidates — biotech, EV, crypto-adjacent, fintech,
# cannabis, mining/energy, retail favorites, and speculative tech.
# ---------------------------------------------------------------------------
SMALL_CAP_UNIVERSE: list[str] = [
    # Biotech / pharma (~95)
    "SAVA", "OCGN", "INO", "BNGO", "EDIT", "NTLA", "BEAM", "VERV", "PRCT",
    "VYGR", "GERN", "RIGL", "CDXS", "HALO", "MGTA", "MIRM",
    "PRTA", "RAPT", "TYRA", "VKTX", "VOR", "ZNTL", "OPK", "ABEO", "ADMA",
    "RXRX", "TWST", "CRBP", "CORT", "ALLO",
    "ALDX", "AMRN", "APLT", "APLS", "AQST", "ARDX", "ARCT", "ARQT", "ATEC",
    "AUPH", "AXSM", "BCRX", "BPMC", "CGEN", "CRMD", "DARE", "DCPH", "DCTH",
    "DICE", "DNLI", "ENTA", "EOLS", "ESPR", "EYEN", "FATE", "FOLD", "FREQ",
    "GH", "GKOS", "IDYA", "IGMS", "INSM", "IONS", "IOVA", "ITCI", "JANX",
    "KDNY", "KROS", "KRYS", "LGND", "LXRX", "MDGL", "MNKD", "MNMD", "MORF",
    "MYGN", "NBIX", "NRIX", "NTRA", "NUVL", "NVAX", "ORIC", "PCVX", "PRTC",
    "PRVA", "RCKT", "RNA", "RVMD", "RYTM", "ARWR", "BCYC", "DYN", "ELVN",
    "TARS", "ANAB",
    # EV / mobility / clean tech (~30)
    "NKLA", "GOEV", "FFIE", "HYZN", "MULN", "WKHS", "SOLO", "FUV", "ARVL",
    "PSNY", "BLNK", "EVGO", "PLUG", "FCEL", "BE", "STEM", "CHPT", "ENVX",
    "QS", "MVST", "XPEV", "LI", "NIO", "GOTU", "ZEV", "GP", "RIDE", "CENN",
    "VLCN", "HYLN",
    # Cannabis / consumer alt (~20)
    "TLRY", "CGC", "ACB", "CRON", "SNDL", "GTBIF", "CURLF", "TCNNF", "AYRWF",
    "VFF", "OGI", "HEXO", "GRWG", "MMNFF", "HITI", "CRLBF", "SMG", "SHWZ",
    "WEED", "VLNS",
    # Mining / energy / commodities (~30)
    "HUSA", "USEG", "IPI", "REI", "GTE", "DSX", "INDO", "SHIP", "NRT", "BRY",
    "CRC", "HCC", "ARLP", "LBRT", "NOG", "SD", "AROC", "RIG", "TUSK", "WTI",
    "AMLX", "UEC", "URG", "NXE", "DNN", "EU", "REMX", "MAG", "AG", "PAAS",
    # Fintech / SPAC remnants / crypto-mining (~30)
    "GREE", "BBIG", "ATER", "BARK", "BEEM", "MRIN", "VRM", "ROOT", "LMND",
    "PRPL", "IRBT", "OPEN", "RDFN", "COMP", "OWLT", "MTTR", "SDIG", "EBET",
    "MARA", "RIOT", "CIFR", "BTBT", "HUT", "BITF", "ARBK", "IREN", "MOGO",
    "BFRG", "DJT", "HSAI",
    # Tech / software / AI / hardware small caps (~50)
    "SOUN", "BBAI", "DM", "MARK", "GLBE", "DOCN", "FSLY", "FROG", "MITK",
    "AAOI", "EXTR", "KOPN", "PSNL", "INVZ", "MVIS", "LAZR", "LIDR", "INDI",
    "INVE", "DOMO", "RNG", "BNRG", "QUIK", "IDN", "AWRE", "WRAP",
    "BZFD", "EXFY", "VNET", "ZEPP", "INSE", "CXAI", "VLN", "MOBX", "LUNR",
    "RKLB", "ASTS", "ASTR", "PL", "SPIR", "SATX", "NBIS", "ACHR", "EH",
    "IRDM", "REKR", "VLD", "TOI", "WULF",
    # Retail / consumer / restaurants (~25)
    "REAL", "WRBY", "FIGS", "RILY", "BOOT", "BIG", "JILL", "OXM", "TLYS",
    "ZUMZ", "GES", "CATO", "SCVL", "DXLG", "NWY", "HBI", "SMRT", "FOXF",
    "KRUS", "FWRG", "DENN", "BJRI", "NDLS", "RUTH", "LOCO",
    # Industrial / specialty / clean tech (~21)
    "AVAV", "MOG-A", "REVG", "NCLH", "ACMR", "HIMX", "PRTH", "INTT",
    "GRPN", "KXIN", "CDLX", "ZIM", "GLNG", "FLNC", "SHLS", "RUN", "SUNW",
    "NOVA", "DQ", "JKS", "ENPH",
]

# ---------------------------------------------------------------------------
# 100 large-cap candidates — mainstream blue chips and household giants. The
# big-player screener filters down by valuation, momentum, and signals.
# ---------------------------------------------------------------------------
LARGE_CAP_UNIVERSE: list[str] = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "BRK-B", "JPM",
    "V", "JNJ", "WMT", "XOM", "UNH", "MA", "PG", "HD", "CVX", "ABBV", "KO",
    "PFE", "AVGO", "MRK", "COST", "BAC", "PEP", "TMO", "MCD", "ABT", "CRM",
    "CSCO", "ACN", "ADBE", "DHR", "NKE", "NFLX", "DIS", "LIN", "TXN", "NEE",
    "WFC", "ORCL", "BMY", "PM", "RTX", "AMGN", "UPS", "HON", "LOW", "T",
    "IBM", "INTC", "MDT", "QCOM", "MS", "GS", "COP", "AMD", "BA", "INTU",
    "CAT", "GE", "BLK", "SPGI", "AXP", "NOW", "ELV", "DE", "PLD", "BKNG",
    "AMT", "ADI", "GILD", "SCHW", "LMT", "MO", "MMC", "REGN", "ISRG", "ZTS",
    "SYK", "VRTX", "MDLZ", "TMUS", "PYPL", "F", "GM", "FDX", "CVS", "PNC",
    "USB", "C", "MET", "ALL", "TRV", "AIG", "COF", "AFL", "DUK", "SO",
]


def all_universe() -> list[str]:
    """Combined deduped list of every ticker APEX may scan."""
    return sorted({*SMALL_CAP_UNIVERSE, *LARGE_CAP_UNIVERSE})
