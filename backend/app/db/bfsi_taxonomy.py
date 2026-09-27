"""
BFSI verticals and sub-industries, for the Companies solar system.

The `industry` field on Company is too coarse for this - 99 companies are
simply "Bank" and 69 "Insurance" - so each BFSI company gets a vertical (a
star) and a sub-industry (a planet) here. Like the headquarters map in
region_data.py, this is a hand-curated, auditable table: one line per
company, matched by name prefix, and `apply` refuses to run if any line
matches no company or more than one, or any BFSI company is left unmapped.

Banks are split partly by market (European, Asia-Pacific, Americas, Middle
East & Africa) because their business models overlap heavily and a sales team
works them by territory; the global universal banks, US regionals and
consumer / mortgage lenders are split out by business model.

NN INC is deliberately unmapped: it is seeded as an insurer but is the US
precision-components maker (NNBR), so it is left out of the solar system
rather than drawn as an insurance company.
"""
from typing import Dict, List, Optional, Tuple

# Display order of stars and their planets.
VERTICALS: List[Tuple[str, List[str]]] = [
    ("Banking", ["Global Universal Banks", "US Regional Banks", "European Banks", "Asia-Pacific Banks",
                 "Americas Banks", "Middle East & Africa Banks", "Consumer & Mortgage Finance"]),
    ("Insurance", ["Life & Retirement", "Property & Casualty", "Health & Managed Care", "Reinsurance",
                   "Multi-line & Composite", "Title Insurance", "Insurance Brokers"]),
    ("Capital Markets & Wealth", ["Investment Banks & Brokers", "Asset & Wealth Management",
                                  "Custody & Trust Banks"]),
    ("Payments & Fintech", ["Card Networks", "Processors & Digital Payments"]),
]
_VERTICAL_OF = {sub: v for v, subs in VERTICALS for sub in subs}

_MAP: Dict[str, List[str]] = {
    # ---------------------------------------------------------------- Banking
    "Global Universal Banks": [
        "JPMorgan Chase", "Bank of America", "Citigroup", "Wells Fargo", "HSBC", "BARCLAYS",
        "Standard Chartered", "BNP Paribas", "Deutsche Bank", "Societe Generale", "Credit Agricole",
        "Groupe BPCE", "Banco Santander, S.A", "UBS Group", "ING Group", "UniCredit", "MITSUBISHI UFJ",
        "MIZUHO", "SUMITOMO MITSUI", "Industrial & Commercial Bank", "China Construction Bank",
        "Agricultural Bank of China", "Bank of China", "Bank of Communications"],
    "US Regional Banks": [
        "U.S. Bancorp", "PNC FINANCIAL", "TRUIST", "Citizens Financial", "FIFTH THIRD", "KeyCorp",
        "Huntington", "M&T BANK", "REGIONS FINANCIAL", "Comerica", "Zions", "FIRST HORIZON",
        "EAST WEST BANCORP", "WESTERN ALLIANCE", "POPULAR, INC"],
    "European Banks": [
        "Lloyds Banking", "NatWest", "KBC Group", "Danske Bank", "Nordea", "DNB", "Skandinaviska Enskilda",
        "Svenska Handelsbanken", "Swedbank", "Commerzbank", "Intesa Sanpaolo", "BANCO BILBAO", "CaixaBank",
        "Credit Mutuel"],
    "Asia-Pacific Banks": [
        "ANZ Group", "Commonwealth Bank", "National Australia Bank", "WESTPAC", "China Merchants Bank",
        "China Minsheng", "China CITIC", "Industrial Bank Co", "Shanghai Pudong", "Postal Savings Bank",
        "Bank of East Asia", "Hang Seng", "HDFC BANK", "ICICI BANK", "Axis Bank", "Kotak Mahindra",
        "State Bank of India", "Punjab National", "Bank of Baroda", "Bank Central Asia", "Bank Mandiri",
        "Japan Post Bank", "SHINHAN", "KB Financial", "Hana Financial", "Maybank", "DBS Group",
        "Oversea-Chinese", "United Overseas Bank", "Bangkok Bank", "Kasikornbank"],
    "Americas Banks": [
        "BANK OF NOVA SCOTIA", "Bank of Montreal", "Canadian Imperial", "ROYAL BANK OF CANADA",
        "TORONTO DOMINION", "CREDICORP", "Banco Bradesco", "Banco Santander (Brasil)", "Banco do Brasil",
        "Itau Unibanco", "Bancolombia", "Grupo Aval", "Grupo Financiero Banorte"],
    "Middle East & Africa Banks": ["Qatar National Bank", "Al Rajhi", "FirstRand", "Standard Bank Group"],
    "Consumer & Mortgage Finance": [
        "CAPITAL ONE", "Ally Financial", "Discover Financial", "Synchrony", "Fannie Mae", "Freddie Mac"],
    # -------------------------------------------------------------- Insurance
    "Life & Retirement": [
        "GREAT-WEST LIFECO", "MANULIFE", "Power Corporation of Canada", "SUN LIFE", "CHINA LIFE",
        "New China Life", "AIA Group", "Life Insurance Corporation", "Dai-ichi", "Japan Post Insurance",
        "Nippon Life", "Samsung Life", "AEGON", "Legal & General", "PRUDENTIAL PLC", "AFLAC", "GLOBE LIFE",
        "LINCOLN NATIONAL", "METLIFE", "PRINCIPAL FINANCIAL", "PRUDENTIAL FINANCIAL", "TIAA", "Unum"],
    "Property & Casualty": [
        "ALLSTATE", "AMERICAN FINANCIAL GROUP", "CINCINNATI FINANCIAL", "ERIE INDEMNITY", "Liberty Mutual",
        "MARKEL", "The Travelers", "The Progressive", "State Farm", "USAA", "W. R. Berkley", "The Hartford",
        "Mapfre", "TOKIO MARINE", "MS&AD", "Sompo", "People's Insurance", "ARCH CAPITAL", "Chubb", "LOEWS",
        "Nationwide", "ASSURANT"],
    "Health & Managed Care": ["CENTENE", "Cigna", "Elevance", "HUMANA"],
    "Reinsurance": ["Munich Re", "Swiss Re", "EVEREST GROUP", "REINSURANCE GROUP OF AMERICA"],
    "Multi-line & Composite": [
        "AXA", "Allianz", "Assicurazioni Generali", "Zurich Insurance", "Aviva", "Ping An", "China Pacific",
        "AMERICAN INTERNATIONAL GROUP", "Talanx", "BERKSHIRE HATHAWAY", "Poste Italiane", "Bajaj Finserv"],
    "Title Insurance": ["Fidelity National Financial", "First American", "OLD REPUBLIC"],
    "Insurance Brokers": ["Aon", "Marsh McLennan"],
    # ----------------------------------------------- Capital Markets & Wealth
    "Investment Banks & Brokers": [
        "The Goldman Sachs", "Morgan Stanley", "NOMURA", "Macquarie", "BTG Pactual", "RAYMOND JAMES",
        "The Charles Schwab"],
    "Asset & Wealth Management": ["BlackRock", "Fidelity Investments", "AMERIPRISE", "Voya Financial"],
    "Custody & Trust Banks": ["STATE STREET", "Bank of New York Mellon", "NORTHERN TRUST"],
    # ----------------------------------------------------- Payments & Fintech
    "Card Networks": ["VISA", "Mastercard", "American Express"],
    "Processors & Digital Payments": ["Fidelity National Information", "FISERV", "GLOBAL PAYMENTS", "PayPal"],
}

# Seeded as BFSI but not a financial company - see the module docstring.
_NOT_BFSI = {"NN INC"}


def classify(companies) -> Dict[str, Optional[Tuple[str, str]]]:
    """company id -> (vertical, sub_industry), or None for the known non-BFSI seed."""
    out: Dict[str, Optional[Tuple[str, str]]] = {}
    used: Dict[str, int] = {}
    for c in companies:
        if c.name in _NOT_BFSI:
            out[c.id] = None
            continue
        low = c.name.lower()
        hits = [(sub, pre) for sub, prefixes in _MAP.items() for pre in prefixes if low.startswith(pre.lower())]
        if len(hits) != 1:
            raise ValueError(f"{c.name!r} matched {len(hits)} sub-industry lines: {hits}")
        sub, pre = hits[0]
        used[pre] = used.get(pre, 0) + 1
        out[c.id] = (_VERTICAL_OF[sub], sub)
    unused = [p for prefixes in _MAP.values() for p in prefixes if p not in used]
    if unused:
        raise ValueError(f"sub-industry lines matching no company: {unused}")
    doubled = [p for p, n in used.items() if n > 1]
    if doubled:
        raise ValueError(f"sub-industry lines matching several companies: {doubled}")
    return out


def apply(db) -> dict:
    """Write vertical / sub_industry onto every BFSI company. Idempotent."""
    from ..models.schema import Company
    companies = db.query(Company).filter(Company.sector == "BFSI").all()
    mapping = classify(companies)
    changed = 0
    for c in companies:
        v, s = mapping[c.id] if mapping[c.id] else (None, None)
        if (c.vertical, c.sub_industry) != (v, s):
            c.vertical, c.sub_industry = v, s
            changed += 1
    db.commit()
    return {"companies": len(companies), "changed": changed,
            "unmapped": sorted(c.name for c in companies if mapping[c.id] is None)}
