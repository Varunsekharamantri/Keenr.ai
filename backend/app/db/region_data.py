"""
Headquarters -> region classification for the company universe.

Two passes, because neither alone is sufficient:

1. SEC submissions JSON is authoritative for companies with a CIK — it carries
   the filer's real business address (`addresses.business`).
2. Everything else needs curation. A "has CIK => Americas" shortcut is
   demonstrably wrong on this data: U.S. Bancorp, State Farm, USAA, Fannie Mae,
   Discover, Schwab and KeyCorp are all American yet carry no CIK in the seed,
   so the rule would misfile ~19 US institutions as Rest of World.
"""
import logging
import time
from typing import Dict, Optional, Tuple

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

AMERICAS = "Americas"
REST_OF_WORLD = "Rest of World"

# Countries in the Americas; everything else rolls up to Rest of World.
AMERICAS_COUNTRIES = {
    "United States", "Canada", "Mexico", "Brazil", "Argentina", "Chile",
    "Colombia", "Peru", "Panama", "Bermuda", "Puerto Rico", "Uruguay",
    "Venezuela", "Ecuador", "Costa Rica", "Dominican Republic", "Bahamas",
    "Cayman Islands", "Jamaica", "Trinidad and Tobago", "Guatemala", "Bolivia",
    "Paraguay", "Honduras", "El Salvador", "Nicaragua",
}

# EDGAR uses its own state/country codes (X0 = United Kingdom, U3 = Spain,
# A6 = Ontario, P7 = Netherlands) — NOT ISO-2. SEC usually supplies
# `stateOrCountryDescription` with the readable name, which is what we prefer;
# this map only covers filers that send a bare code with no description.
EDGAR_CODE_TO_COUNTRY = {
    "P7": "Netherlands", "X0": "United Kingdom", "U3": "Spain",
    "L6": "Switzerland", "I0": "France", "2M": "Germany", "L2": "Sweden",
    "M5": "Japan", "F4": "China", "K3": "Hong Kong", "K7": "India",
    "C3": "Australia", "U0": "Singapore", "T3": "South Africa",
    "D0": "Bermuda", "G0": "Cayman Islands", "N4": "Ireland",
    "1K": "Canada", "B0": "Canada",
}

# ISO-2 country codes (used when SEC sends countryCode instead) -> name.
COUNTRY_CODE_TO_NAME = {
    "US": "United States", "CA": "Canada", "MX": "Mexico", "BR": "Brazil",
    "AR": "Argentina", "CL": "Chile", "CO": "Colombia", "PE": "Peru",
    "BM": "Bermuda", "KY": "Cayman Islands", "PA": "Panama", "UY": "Uruguay",
    "GB": "United Kingdom", "IE": "Ireland", "FR": "France", "DE": "Germany",
    "CH": "Switzerland", "NL": "Netherlands", "BE": "Belgium", "IT": "Italy",
    "ES": "Spain", "PT": "Portugal", "SE": "Sweden", "NO": "Norway",
    "DK": "Denmark", "FI": "Finland", "AT": "Austria", "LU": "Luxembourg",
    "PL": "Poland", "GR": "Greece", "RU": "Russia", "TR": "Turkey",
    "IL": "Israel", "AE": "United Arab Emirates", "SA": "Saudi Arabia",
    "QA": "Qatar", "ZA": "South Africa", "NG": "Nigeria", "EG": "Egypt",
    "IN": "India", "CN": "China", "HK": "Hong Kong", "TW": "Taiwan",
    "JP": "Japan", "KR": "South Korea", "SG": "Singapore", "MY": "Malaysia",
    "TH": "Thailand", "ID": "Indonesia", "PH": "Philippines", "VN": "Vietnam",
    "AU": "Australia", "NZ": "New Zealand",
}

# US states/territories that appear in SEC's stateOrCountry field.
US_STATE_CODES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH",
    "NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT",
    "VT","VA","WA","WV","WI","WY","PR","VI","GU",
}

# Curated HQ for companies with no CIK (manually checked). Keys are matched
# case-insensitively against Company.name.
HQ_BY_COMPANY: Dict[str, str] = {
    # --- Americas ---
    "citizens financial group": "United States",
    "comerica": "United States",
    "discover financial services": "United States",
    "fannie mae": "United States",
    "fidelity investments": "United States",
    "freddie mac": "United States",
    "huntington bancshares": "United States",
    "keycorp": "United States",
    "liberty mutual": "United States",
    "marsh mclennan": "United States",
    "nationwide": "United States",
    "state farm": "United States",
    "tiaa": "United States",
    "the charles schwab corporation": "United States",
    "the hartford": "United States",
    "u.s. bancorp": "United States",
    "usaa": "United States",
    "w. r. berkley": "United States",
    "zions bancorporation": "United States",
    "bank of montreal": "Canada",
    "canadian imperial bank of commerce (cibc)": "Canada",
    "power corporation of canada": "Canada",
    # Foreign private issuers that file with SEC but publish no business
    # address in their submissions JSON, so the authoritative pass can't see them.
    "royal bank of canada": "Canada",
    "sun life financial inc": "Canada",
    "btg pactual": "Brazil",
    "banco bradesco": "Brazil",
    "banco do brasil": "Brazil",
    "bancolombia": "Colombia",
    "grupo aval": "Colombia",
    "grupo financiero banorte": "Mexico",
    # --- EMEA ---
    "axa": "France",
    "bnp paribas": "France",
    "credit agricole": "France",
    "credit mutuel": "France",
    "groupe bpce": "France",
    "societe generale": "France",
    "allianz": "Germany",
    "commerzbank": "Germany",
    "deutsche bank": "Germany",
    "munich re": "Germany",
    "talanx": "Germany",
    "assicurazioni generali": "Italy",
    "intesa sanpaolo": "Italy",
    "unicredit": "Italy",
    "poste italiane": "Italy",
    "caixabank": "Spain",
    "mapfre": "Spain",
    "aviva": "United Kingdom",
    "legal & general": "United Kingdom",
    "standard chartered": "United Kingdom",
    "ing group": "Netherlands",
    "kbc group": "Belgium",
    "swiss re": "Switzerland",
    "zurich insurance group": "Switzerland",
    "ubs group ag": "Switzerland",
    "danske bank": "Denmark",
    "dnb": "Norway",
    "nordea": "Finland",
    "skandinaviska enskilda banken (seb)": "Sweden",
    "svenska handelsbanken": "Sweden",
    "swedbank": "Sweden",
    "al rajhi bank": "Saudi Arabia",
    "qatar national bank": "Qatar",
    "firstrand": "South Africa",
    "standard bank group": "South Africa",
    # --- APAC ---
    "aia group": "Hong Kong",
    "bank of east asia": "Hong Kong",
    "hang seng bank": "Hong Kong",
    "anz group": "Australia",
    "commonwealth bank of australia": "Australia",
    "macquarie group": "Australia",
    "national australia bank": "Australia",
    "agricultural bank of china": "China",
    "bank of china": "China",
    "bank of communications": "China",
    "china citic bank": "China",
    "china construction bank": "China",
    "china merchants bank": "China",
    "china minsheng bank": "China",
    "china pacific insurance": "China",
    "industrial & commercial bank of china (icbc)": "China",
    "industrial bank co.": "China",
    "new china life insurance": "China",
    "people's insurance company of china (picc)": "China",
    "ping an insurance": "China",
    "postal savings bank of china": "China",
    "shanghai pudong development bank": "China",
    "axis bank": "India",
    "bajaj finserv": "India",
    "bank of baroda": "India",
    "kotak mahindra bank": "India",
    "life insurance corporation of india (lic)": "India",
    "punjab national bank": "India",
    "state bank of india": "India",
    "dai-ichi life holdings": "Japan",
    "japan post insurance": "Japan",
    "nomura holdings inc": "Japan",
    "tokio marine holdings, inc.": "Japan",
    "ms&ad insurance group": "Japan",
    "nippon life insurance": "Japan",
    "sompo holdings": "Japan",
    "hana financial group": "South Korea",
    "samsung life insurance": "South Korea",
    "dbs group": "Singapore",
    "oversea-chinese banking corporation (ocbc)": "Singapore",
    "united overseas bank (uob)": "Singapore",
    "maybank": "Malaysia",
    "bangkok bank": "Thailand",
    "kasikornbank": "Thailand",
    "bank central asia": "Indonesia",
    "bank mandiri": "Indonesia",
}


def region_for_country(country: Optional[str]) -> Optional[str]:
    if not country:
        return None
    return AMERICAS if country in AMERICAS_COUNTRIES else REST_OF_WORLD


def curated_hq(company_name: str) -> Optional[str]:
    return HQ_BY_COMPANY.get((company_name or "").strip().lower())


def hq_from_sec(cik: str, session: Optional[httpx.Client] = None) -> Optional[str]:
    """
    Authoritative HQ country from the SEC submissions JSON
    (`addresses.business`). Returns a country name, or None if unavailable.
    """
    if not cik:
        return None
    formatted = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{formatted}.json"
    headers = {
        "User-Agent": settings.SEC_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }
    try:
        time.sleep(settings.SEC_RATE_LIMIT_DELAY)
        client = session or httpx.Client(timeout=15.0)
        resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        business = (resp.json().get("addresses") or {}).get("business") or {}
    except Exception as e:
        logger.warning(f"SEC HQ lookup failed for CIK {formatted}: {e}")
        return None

    # Prefer SEC's own readable description — it resolves EDGAR's proprietary
    # codes for us (X0 -> "United Kingdom", A6 -> "Ontario, Canada").
    description = (business.get("stateOrCountryDescription") or "").strip()
    state_or_country = (business.get("stateOrCountry") or "").strip().upper()

    if description:
        if description.upper() in US_STATE_CODES:
            return "United States"
        # "Ontario, Canada" / "Quebec, Canada" -> the country is after the comma
        return description.split(",")[-1].strip()

    if state_or_country in US_STATE_CODES:
        return "United States"
    if state_or_country in EDGAR_CODE_TO_COUNTRY:
        return EDGAR_CODE_TO_COUNTRY[state_or_country]

    code = (business.get("countryCode") or "").strip().upper()
    if code:
        return EDGAR_CODE_TO_COUNTRY.get(code) or COUNTRY_CODE_TO_NAME.get(code)
    return None


def resolve_company_region(name: str, cik: Optional[str],
                           session: Optional[httpx.Client] = None) -> Tuple[Optional[str], Optional[str], str]:
    """
    Returns (country, region, source) where source is 'sec' | 'curated' | 'unknown'.
    Curated values win for names we've explicitly checked; SEC fills the rest.
    """
    curated = curated_hq(name)
    if curated:
        return curated, region_for_country(curated), "curated"
    if cik:
        country = hq_from_sec(cik, session=session)
        if country:
            return country, region_for_country(country), "sec"
    return None, None, "unknown"
