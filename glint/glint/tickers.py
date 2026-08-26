"""Curated ticker universes (UK FTSE 100 + US large-cap), Yahoo Finance format.

Free-tier data sources don't expose a full exchange ticker-list endpoint, so
these lists are hardcoded. Swap for a fetched exchange symbol list if/when
Glint moves to a paid provider (EODHD / FMP).
"""

FTSE_100 = [
    "AAF.L", "ADM.L", "AAL.L", "ANTO.L", "ABF.L", "AZN.L", "AUTO.L",
    "AV.L", "BME.L", "BA.L", "BARC.L", "BTRW.L", "BEZ.L", "BKG.L", "BP.L",
    "BATS.L", "BLND.L", "BT-A.L", "BNZL.L", "BRBY.L", "CNA.L", "CCH.L",
    "CPG.L", "CTEC.L", "CRDA.L", "DCC.L", "DGE.L", "DPLM.L", "EDV.L",
    "ENT.L", "EXPN.L", "FCIT.L", "FRAS.L", "FRES.L", "GAW.L", "GLEN.L",
    "GSK.L", "HLN.L", "HLMA.L", "HIK.L", "HWDN.L", "HSBA.L",
    "IHG.L", "IMI.L", "IMB.L", "INF.L", "ICG.L", "IAG.L", "ITRK.L", "JD.L",
    "KGF.L", "LAND.L", "LGEN.L", "LLOY.L", "LMP.L", "LSEG.L", "MNG.L",
    "MKS.L", "MRO.L", "MNDI.L", "NG.L", "NWG.L", "NXT.L", "PSON.L",
    "PSH.L", "PSN.L", "PHNX.L", "PCT.L", "PRU.L", "RKT.L", "REL.L",
    "RTO.L", "RMV.L", "RIO.L", "RR.L", "SGE.L", "SBRY.L", "SDR.L",
    "SMT.L", "SGRO.L", "SVT.L", "SHEL.L", "SN.L", "SMIN.L",
    "SPX.L", "SSE.L", "STAN.L", "STJ.L", "TW.L", "TSCO.L", "ULVR.L",
    "UTG.L", "UU.L", "VOD.L", "WEIR.L", "WTB.L", "WPP.L",
]

# ~100 liquid US large caps spanning sectors, no suffix needed for Yahoo Finance.
US_LARGE_CAP = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO", "ORCL",
    "CRM", "ADBE", "CSCO", "AMD", "INTC", "IBM", "QCOM", "TXN", "INTU",
    "NOW", "UBER",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "AXP", "BLK", "SPGI",
    "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT", "BMY", "AMGN",
    "CVS", "MDT", "GILD", "CI", "ELV",
    "PG", "KO", "PEP", "WMT", "COST", "MCD", "NKE", "SBUX", "TGT", "HD",
    "LOW", "CL", "KMB", "MO", "PM", "EL",
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY",
    "BA", "CAT", "GE", "HON", "UPS", "RTX", "LMT", "DE", "MMM", "UNP",
    "LIN", "APD", "SHW", "NEM", "FCX",
    "NEE", "DUK", "SO", "D", "AEP",
    "AMT", "PLD", "SPG", "O",
    "DIS", "NFLX", "CMCSA", "T", "VZ", "TMUS",
    "PYPL", "V", "MA", "SQ",
    "F", "GM",
]
