MARKETS = {
    "US": ["AAPL", "KO", "ADBE", "PYPL", "INTC"],
    "UK": ["ULVR.L", "AZN.L", "SHEL.L", "BATS.L"],
    "Europe": ["MC.PA", "SAP.DE", "NESN.SW", "SIE.DE"],
    "Japan": ["7203.T", "6758.T", "9984.T"],
    "Asia": ["0700.HK", "9988.HK", "005930.KS"],
}

WATCHLIST = [ticker for tickers in MARKETS.values() for ticker in tickers]
