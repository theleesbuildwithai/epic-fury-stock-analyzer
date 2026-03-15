"""
Ticker search — lets users type a company name and find the ticker.
Uses a built-in database of ~300 popular stocks so it's instant
and doesn't hit Yahoo Finance (no rate limit risk).
"""


# Built-in database: (ticker, company name, sector)
# Covers S&P 500 top holdings, popular tech, ETFs, and commonly traded stocks
TICKER_DATABASE = [
    # Tech Giants
    ("AAPL", "Apple Inc", "Technology"),
    ("MSFT", "Microsoft Corporation", "Technology"),
    ("GOOGL", "Alphabet Inc (Google) Class A", "Technology"),
    ("GOOG", "Alphabet Inc (Google) Class C", "Technology"),
    ("AMZN", "Amazon.com Inc", "Consumer Cyclical"),
    ("META", "Meta Platforms Inc (Facebook)", "Technology"),
    ("NVDA", "NVIDIA Corporation", "Technology"),
    ("TSLA", "Tesla Inc", "Consumer Cyclical"),
    ("TSM", "Taiwan Semiconductor Manufacturing", "Technology"),
    ("AVGO", "Broadcom Inc", "Technology"),
    ("ORCL", "Oracle Corporation", "Technology"),
    ("ADBE", "Adobe Inc", "Technology"),
    ("CRM", "Salesforce Inc", "Technology"),
    ("CSCO", "Cisco Systems Inc", "Technology"),
    ("ACN", "Accenture plc", "Technology"),
    ("IBM", "International Business Machines", "Technology"),
    ("INTC", "Intel Corporation", "Technology"),
    ("AMD", "Advanced Micro Devices Inc", "Technology"),
    ("TXN", "Texas Instruments Inc", "Technology"),
    ("QCOM", "Qualcomm Inc", "Technology"),
    ("INTU", "Intuit Inc", "Technology"),
    ("AMAT", "Applied Materials Inc", "Technology"),
    ("NOW", "ServiceNow Inc", "Technology"),
    ("ISRG", "Intuitive Surgical Inc", "Healthcare"),
    ("MU", "Micron Technology Inc", "Technology"),
    ("LRCX", "Lam Research Corporation", "Technology"),
    ("KLAC", "KLA Corporation", "Technology"),
    ("SNPS", "Synopsys Inc", "Technology"),
    ("CDNS", "Cadence Design Systems", "Technology"),
    ("MRVL", "Marvell Technology Inc", "Technology"),
    ("NXPI", "NXP Semiconductors", "Technology"),
    ("PANW", "Palo Alto Networks Inc", "Technology"),
    ("CRWD", "CrowdStrike Holdings Inc", "Technology"),
    ("FTNT", "Fortinet Inc", "Technology"),
    ("NET", "Cloudflare Inc", "Technology"),
    ("DDOG", "Datadog Inc", "Technology"),
    ("ZS", "Zscaler Inc", "Technology"),
    ("SNOW", "Snowflake Inc", "Technology"),
    ("PLTR", "Palantir Technologies Inc", "Technology"),
    ("U", "Unity Software Inc", "Technology"),
    ("RBLX", "Roblox Corporation", "Technology"),
    ("SHOP", "Shopify Inc", "Technology"),
    ("SQ", "Block Inc (Square)", "Technology"),
    ("PYPL", "PayPal Holdings Inc", "Technology"),
    ("COIN", "Coinbase Global Inc", "Technology"),
    ("ROKU", "Roku Inc", "Technology"),
    ("UBER", "Uber Technologies Inc", "Technology"),
    ("LYFT", "Lyft Inc", "Technology"),
    ("ABNB", "Airbnb Inc", "Technology"),
    ("DASH", "DoorDash Inc", "Technology"),
    ("SPOT", "Spotify Technology SA", "Technology"),
    ("PINS", "Pinterest Inc", "Technology"),
    ("SNAP", "Snap Inc (Snapchat)", "Technology"),
    ("TTD", "The Trade Desk Inc", "Technology"),
    ("DELL", "Dell Technologies Inc", "Technology"),
    ("HPQ", "HP Inc", "Technology"),
    ("HPE", "Hewlett Packard Enterprise", "Technology"),
    ("WDAY", "Workday Inc", "Technology"),
    ("TEAM", "Atlassian Corporation", "Technology"),
    ("MDB", "MongoDB Inc", "Technology"),
    ("TWLO", "Twilio Inc", "Technology"),
    ("ZM", "Zoom Video Communications", "Technology"),
    ("DOCU", "DocuSign Inc", "Technology"),
    ("OKTA", "Okta Inc", "Technology"),
    ("PATH", "UiPath Inc", "Technology"),
    ("BILL", "BILL Holdings Inc", "Technology"),
    ("HUBS", "HubSpot Inc", "Technology"),
    ("VEEV", "Veeva Systems Inc", "Technology"),
    ("ANSS", "ANSYS Inc", "Technology"),
    ("SMCI", "Super Micro Computer Inc", "Technology"),
    ("ARM", "Arm Holdings plc", "Technology"),
    ("ON", "ON Semiconductor", "Technology"),
    ("MCHP", "Microchip Technology Inc", "Technology"),
    ("SWKS", "Skyworks Solutions Inc", "Technology"),
    ("MPWR", "Monolithic Power Systems", "Technology"),

    # Finance
    ("JPM", "JPMorgan Chase & Co", "Financial Services"),
    ("V", "Visa Inc", "Financial Services"),
    ("MA", "Mastercard Inc", "Financial Services"),
    ("BAC", "Bank of America Corp", "Financial Services"),
    ("WFC", "Wells Fargo & Company", "Financial Services"),
    ("GS", "Goldman Sachs Group Inc", "Financial Services"),
    ("MS", "Morgan Stanley", "Financial Services"),
    ("BLK", "BlackRock Inc", "Financial Services"),
    ("SCHW", "Charles Schwab Corporation", "Financial Services"),
    ("C", "Citigroup Inc", "Financial Services"),
    ("AXP", "American Express Company", "Financial Services"),
    ("USB", "U.S. Bancorp", "Financial Services"),
    ("PNC", "PNC Financial Services", "Financial Services"),
    ("TFC", "Truist Financial Corporation", "Financial Services"),
    ("COF", "Capital One Financial", "Financial Services"),
    ("BK", "Bank of New York Mellon", "Financial Services"),
    ("CME", "CME Group Inc", "Financial Services"),
    ("ICE", "Intercontinental Exchange", "Financial Services"),
    ("SPGI", "S&P Global Inc", "Financial Services"),
    ("MCO", "Moody's Corporation", "Financial Services"),
    ("MSCI", "MSCI Inc", "Financial Services"),
    ("FIS", "Fidelity National Information", "Financial Services"),
    ("AIG", "American International Group", "Financial Services"),
    ("MET", "MetLife Inc", "Financial Services"),
    ("PRU", "Prudential Financial Inc", "Financial Services"),
    ("ALL", "Allstate Corporation", "Financial Services"),
    ("TRV", "Travelers Companies Inc", "Financial Services"),
    ("CB", "Chubb Limited", "Financial Services"),
    ("AFL", "Aflac Inc", "Financial Services"),
    ("SOFI", "SoFi Technologies Inc", "Financial Services"),
    ("HOOD", "Robinhood Markets Inc", "Financial Services"),

    # Healthcare
    ("UNH", "UnitedHealth Group Inc", "Healthcare"),
    ("JNJ", "Johnson & Johnson", "Healthcare"),
    ("LLY", "Eli Lilly and Company", "Healthcare"),
    ("PFE", "Pfizer Inc", "Healthcare"),
    ("ABBV", "AbbVie Inc", "Healthcare"),
    ("MRK", "Merck & Co Inc", "Healthcare"),
    ("TMO", "Thermo Fisher Scientific", "Healthcare"),
    ("ABT", "Abbott Laboratories", "Healthcare"),
    ("DHR", "Danaher Corporation", "Healthcare"),
    ("BMY", "Bristol-Myers Squibb", "Healthcare"),
    ("AMGN", "Amgen Inc", "Healthcare"),
    ("GILD", "Gilead Sciences Inc", "Healthcare"),
    ("MDT", "Medtronic plc", "Healthcare"),
    ("SYK", "Stryker Corporation", "Healthcare"),
    ("ELV", "Elevance Health Inc", "Healthcare"),
    ("CI", "Cigna Group", "Healthcare"),
    ("CVS", "CVS Health Corporation", "Healthcare"),
    ("HUM", "Humana Inc", "Healthcare"),
    ("REGN", "Regeneron Pharmaceuticals", "Healthcare"),
    ("VRTX", "Vertex Pharmaceuticals", "Healthcare"),
    ("ZTS", "Zoetis Inc", "Healthcare"),
    ("MRNA", "Moderna Inc", "Healthcare"),
    ("BNTX", "BioNTech SE", "Healthcare"),
    ("BIIB", "Biogen Inc", "Healthcare"),
    ("ILMN", "Illumina Inc", "Healthcare"),
    ("DXCM", "DexCom Inc", "Healthcare"),
    ("BSX", "Boston Scientific Corporation", "Healthcare"),
    ("EW", "Edwards Lifesciences", "Healthcare"),
    ("A", "Agilent Technologies Inc", "Healthcare"),
    ("IQV", "IQVIA Holdings Inc", "Healthcare"),
    ("NVO", "Novo Nordisk A/S", "Healthcare"),

    # Consumer
    ("WMT", "Walmart Inc", "Consumer Defensive"),
    ("PG", "Procter & Gamble Company", "Consumer Defensive"),
    ("KO", "Coca-Cola Company", "Consumer Defensive"),
    ("PEP", "PepsiCo Inc", "Consumer Defensive"),
    ("COST", "Costco Wholesale Corporation", "Consumer Defensive"),
    ("HD", "Home Depot Inc", "Consumer Cyclical"),
    ("MCD", "McDonald's Corporation", "Consumer Cyclical"),
    ("NKE", "Nike Inc", "Consumer Cyclical"),
    ("SBUX", "Starbucks Corporation", "Consumer Cyclical"),
    ("TGT", "Target Corporation", "Consumer Defensive"),
    ("LOW", "Lowe's Companies Inc", "Consumer Cyclical"),
    ("TJX", "TJX Companies Inc", "Consumer Cyclical"),
    ("ROST", "Ross Stores Inc", "Consumer Cyclical"),
    ("DG", "Dollar General Corporation", "Consumer Defensive"),
    ("DLTR", "Dollar Tree Inc", "Consumer Defensive"),
    ("EL", "Estee Lauder Companies", "Consumer Defensive"),
    ("CL", "Colgate-Palmolive Company", "Consumer Defensive"),
    ("KMB", "Kimberly-Clark Corporation", "Consumer Defensive"),
    ("GIS", "General Mills Inc", "Consumer Defensive"),
    ("K", "Kellanova (Kellogg's)", "Consumer Defensive"),
    ("HSY", "Hershey Company", "Consumer Defensive"),
    ("KHC", "Kraft Heinz Company", "Consumer Defensive"),
    ("MDLZ", "Mondelez International", "Consumer Defensive"),
    ("STZ", "Constellation Brands Inc", "Consumer Defensive"),
    ("PM", "Philip Morris International", "Consumer Defensive"),
    ("MO", "Altria Group Inc", "Consumer Defensive"),
    ("CMG", "Chipotle Mexican Grill", "Consumer Cyclical"),
    ("YUM", "Yum! Brands Inc", "Consumer Cyclical"),
    ("DPZ", "Domino's Pizza Inc", "Consumer Cyclical"),
    ("LULU", "Lululemon Athletica Inc", "Consumer Cyclical"),
    ("DECK", "Deckers Outdoor Corporation", "Consumer Cyclical"),
    ("CROX", "Crocs Inc", "Consumer Cyclical"),
    ("F", "Ford Motor Company", "Consumer Cyclical"),
    ("GM", "General Motors Company", "Consumer Cyclical"),
    ("RIVN", "Rivian Automotive Inc", "Consumer Cyclical"),
    ("LCID", "Lucid Group Inc", "Consumer Cyclical"),
    ("DIS", "Walt Disney Company", "Communication Services"),
    ("NFLX", "Netflix Inc", "Communication Services"),
    ("WBD", "Warner Bros Discovery", "Communication Services"),
    ("PARA", "Paramount Global", "Communication Services"),
    ("CMCSA", "Comcast Corporation", "Communication Services"),
    ("CHTR", "Charter Communications", "Communication Services"),
    ("TMUS", "T-Mobile US Inc", "Communication Services"),
    ("VZ", "Verizon Communications", "Communication Services"),
    ("T", "AT&T Inc", "Communication Services"),

    # Energy
    ("XOM", "Exxon Mobil Corporation", "Energy"),
    ("CVX", "Chevron Corporation", "Energy"),
    ("COP", "ConocoPhillips", "Energy"),
    ("SLB", "Schlumberger Limited", "Energy"),
    ("EOG", "EOG Resources Inc", "Energy"),
    ("MPC", "Marathon Petroleum Corp", "Energy"),
    ("PSX", "Phillips 66", "Energy"),
    ("VLO", "Valero Energy Corporation", "Energy"),
    ("OXY", "Occidental Petroleum", "Energy"),
    ("PXD", "Pioneer Natural Resources", "Energy"),
    ("DVN", "Devon Energy Corporation", "Energy"),
    ("HAL", "Halliburton Company", "Energy"),
    ("BKR", "Baker Hughes Company", "Energy"),
    ("FANG", "Diamondback Energy Inc", "Energy"),
    ("KMI", "Kinder Morgan Inc", "Energy"),
    ("WMB", "Williams Companies Inc", "Energy"),
    ("OKE", "ONEOK Inc", "Energy"),
    ("ET", "Energy Transfer LP", "Energy"),
    ("ENPH", "Enphase Energy Inc", "Energy"),
    ("SEDG", "SolarEdge Technologies", "Energy"),
    ("FSLR", "First Solar Inc", "Energy"),

    # Industrials
    ("CAT", "Caterpillar Inc", "Industrials"),
    ("BA", "Boeing Company", "Industrials"),
    ("HON", "Honeywell International", "Industrials"),
    ("UNP", "Union Pacific Corporation", "Industrials"),
    ("RTX", "RTX Corporation (Raytheon)", "Industrials"),
    ("GE", "GE Aerospace", "Industrials"),
    ("LMT", "Lockheed Martin Corporation", "Industrials"),
    ("DE", "Deere & Company (John Deere)", "Industrials"),
    ("MMM", "3M Company", "Industrials"),
    ("UPS", "United Parcel Service", "Industrials"),
    ("FDX", "FedEx Corporation", "Industrials"),
    ("GD", "General Dynamics Corporation", "Industrials"),
    ("NOC", "Northrop Grumman Corporation", "Industrials"),
    ("WM", "Waste Management Inc", "Industrials"),
    ("EMR", "Emerson Electric Co", "Industrials"),
    ("ETN", "Eaton Corporation plc", "Industrials"),
    ("ITW", "Illinois Tool Works Inc", "Industrials"),
    ("CSX", "CSX Corporation", "Industrials"),
    ("NSC", "Norfolk Southern Corporation", "Industrials"),
    ("DAL", "Delta Air Lines Inc", "Industrials"),
    ("UAL", "United Airlines Holdings", "Industrials"),
    ("AAL", "American Airlines Group", "Industrials"),
    ("LUV", "Southwest Airlines Co", "Industrials"),

    # Real Estate
    ("AMT", "American Tower Corporation", "Real Estate"),
    ("PLD", "Prologis Inc", "Real Estate"),
    ("CCI", "Crown Castle Inc", "Real Estate"),
    ("EQIX", "Equinix Inc", "Real Estate"),
    ("SPG", "Simon Property Group", "Real Estate"),
    ("O", "Realty Income Corporation", "Real Estate"),
    ("PSA", "Public Storage", "Real Estate"),
    ("WELL", "Welltower Inc", "Real Estate"),
    ("DLR", "Digital Realty Trust", "Real Estate"),
    ("AVB", "AvalonBay Communities Inc", "Real Estate"),

    # Materials
    ("LIN", "Linde plc", "Materials"),
    ("APD", "Air Products & Chemicals", "Materials"),
    ("SHW", "Sherwin-Williams Company", "Materials"),
    ("NEM", "Newmont Corporation", "Materials"),
    ("FCX", "Freeport-McMoRan Inc", "Materials"),
    ("NUE", "Nucor Corporation", "Materials"),
    ("DD", "DuPont de Nemours Inc", "Materials"),
    ("DOW", "Dow Inc", "Materials"),
    ("ECL", "Ecolab Inc", "Materials"),
    ("VMC", "Vulcan Materials Company", "Materials"),

    # Utilities
    ("NEE", "NextEra Energy Inc", "Utilities"),
    ("DUK", "Duke Energy Corporation", "Utilities"),
    ("SO", "Southern Company", "Utilities"),
    ("D", "Dominion Energy Inc", "Utilities"),
    ("AEP", "American Electric Power", "Utilities"),
    ("SRE", "Sempra Energy", "Utilities"),
    ("EXC", "Exelon Corporation", "Utilities"),
    ("XEL", "Xcel Energy Inc", "Utilities"),
    ("ED", "Consolidated Edison Inc", "Utilities"),
    ("WEC", "WEC Energy Group Inc", "Utilities"),

    # Popular ETFs
    ("SPY", "SPDR S&P 500 ETF Trust", "ETF"),
    ("QQQ", "Invesco QQQ Trust (Nasdaq 100)", "ETF"),
    ("DIA", "SPDR Dow Jones Industrial ETF", "ETF"),
    ("IWM", "iShares Russell 2000 ETF", "ETF"),
    ("VOO", "Vanguard S&P 500 ETF", "ETF"),
    ("VTI", "Vanguard Total Stock Market ETF", "ETF"),
    ("VGT", "Vanguard Information Technology ETF", "ETF"),
    ("ARKK", "ARK Innovation ETF", "ETF"),
    ("XLF", "Financial Select Sector SPDR", "ETF"),
    ("XLE", "Energy Select Sector SPDR", "ETF"),
    ("XLK", "Technology Select Sector SPDR", "ETF"),
    ("XLV", "Health Care Select Sector SPDR", "ETF"),
    ("XLI", "Industrial Select Sector SPDR", "ETF"),
    ("XLP", "Consumer Staples Select SPDR", "ETF"),
    ("XLY", "Consumer Discretionary SPDR", "ETF"),
    ("XLU", "Utilities Select Sector SPDR", "ETF"),
    ("GLD", "SPDR Gold Shares", "ETF"),
    ("SLV", "iShares Silver Trust", "ETF"),
    ("TLT", "iShares 20+ Year Treasury Bond", "ETF"),
    ("HYG", "iShares iBoxx High Yield Bond", "ETF"),
    ("VNQ", "Vanguard Real Estate ETF", "ETF"),
    ("EEM", "iShares MSCI Emerging Markets", "ETF"),
    ("EFA", "iShares MSCI EAFE ETF", "ETF"),
    ("SOXX", "iShares Semiconductor ETF", "ETF"),
    ("SMH", "VanEck Semiconductor ETF", "ETF"),
    ("SCHD", "Schwab U.S. Dividend Equity ETF", "ETF"),

    # Crypto-related
    ("MSTR", "MicroStrategy Incorporated", "Technology"),
    ("MARA", "Marathon Digital Holdings", "Technology"),
    ("RIOT", "Riot Platforms Inc", "Technology"),
    ("CLSK", "CleanSpark Inc", "Technology"),
    ("HUT", "Hut 8 Mining Corp", "Technology"),

    # Popular international ADRs
    ("BABA", "Alibaba Group Holding", "Consumer Cyclical"),
    ("JD", "JD.com Inc", "Consumer Cyclical"),
    ("PDD", "PDD Holdings (Pinduoduo)", "Consumer Cyclical"),
    ("BIDU", "Baidu Inc", "Technology"),
    ("NIO", "NIO Inc", "Consumer Cyclical"),
    ("LI", "Li Auto Inc", "Consumer Cyclical"),
    ("XPEV", "XPeng Inc", "Consumer Cyclical"),
    ("SE", "Sea Limited", "Technology"),
    ("GRAB", "Grab Holdings Limited", "Technology"),
    ("SONY", "Sony Group Corporation", "Technology"),
    ("TM", "Toyota Motor Corporation", "Consumer Cyclical"),
    ("HMC", "Honda Motor Co Ltd", "Consumer Cyclical"),
    ("SAP", "SAP SE", "Technology"),
    ("ASML", "ASML Holding NV", "Technology"),
    ("BP", "BP plc", "Energy"),
    ("SHEL", "Shell plc", "Energy"),
    ("RIO", "Rio Tinto Group", "Materials"),
    ("BHP", "BHP Group Limited", "Materials"),
    ("VALE", "Vale SA", "Materials"),
    ("UL", "Unilever plc", "Consumer Defensive"),
    ("DEO", "Diageo plc", "Consumer Defensive"),
    ("AZN", "AstraZeneca plc", "Healthcare"),
    ("GSK", "GSK plc (GlaxoSmithKline)", "Healthcare"),
    ("SNY", "Sanofi SA", "Healthcare"),
]


def search_tickers(query: str, limit: int = 8) -> list:
    """
    Search for tickers by company name or ticker symbol.
    Returns matches sorted by relevance.
    No API calls — uses the built-in database for instant results.
    """
    if not query or len(query.strip()) < 1:
        return []

    q = query.strip().lower()
    results = []

    for ticker, name, sector in TICKER_DATABASE:
        ticker_lower = ticker.lower()
        name_lower = name.lower()

        # Exact ticker match = highest priority
        if ticker_lower == q:
            results.append((0, ticker, name, sector))
        # Ticker starts with query
        elif ticker_lower.startswith(q):
            results.append((1, ticker, name, sector))
        # Company name starts with query
        elif name_lower.startswith(q):
            results.append((2, ticker, name, sector))
        # Query words all found in name (handles "goldman sachs", "micro devices", etc.)
        elif all(word in name_lower for word in q.split()):
            results.append((3, ticker, name, sector))
        # Partial match anywhere in name
        elif q in name_lower:
            results.append((4, ticker, name, sector))
        # Partial match in ticker
        elif q in ticker_lower:
            results.append((5, ticker, name, sector))

    # Sort by priority, then alphabetically
    results.sort(key=lambda x: (x[0], x[1]))

    return [
        {"ticker": r[1], "name": r[2], "sector": r[3]}
        for r in results[:limit]
    ]
