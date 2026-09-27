"""
People to Tap - named leaders at the companies behind an opportunity.

Where the names come from
-------------------------
One Exa search per company, restricted to LinkedIn profiles, for senior
technology titles. Every result carries the profile's own headline ("CTO at
Punjab National Bank"), and a person is kept only when that headline:

  * names this company - not a subsidiary or a sister brand
    ("CIO - PNB Housing Finance" is not Punjab National Bank),
  * carries a senior title (chief / head / VP / director / EVP ...), and
  * does not describe the role as a former one ("Ex CIO at ...").

The name, title and URL all come from the search result. Nothing is recalled
by a language model and no URL is ever built from a name - a guessed profile
link can land on a stranger who shares the name.

What it does not claim
----------------------
No public source says who owns a given initiative's budget. A leader is
shown next to an opportunity because their title covers that area ("Head of
AI" beside an AI opportunity), or, failing that, because they are a senior
technology leader. The tile says which of the two it is.

When it refreshes
-----------------
A company is searched once, then again only when a leadership-change event
(the executive_leadership_change initiative) is stored after the last search,
or when the data is older than LEADERS_TTL_DAYS. See LeaderFetch.
"""
import datetime
import logging
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..ingestion.exa_budget import ExaBudgetTracker
from ..models.schema import Company, Event, Leader, LeaderFetch

logger = logging.getLogger("market_signals.people")

budget = ExaBudgetTracker(settings.DATA_DIR / "exa_people_usage.json", settings.EXA_PEOPLE_MONTHLY_BUDGET)

# --------------------------------------------------------------------- titles
# Checked against the headline in this order; a headline can carry several.
FUNCTIONS: List[Tuple[str, str, "re.Pattern"]] = [
    ("security", "cybersecurity",
     re.compile(r"\bciso\b|information security|cyber|ciberseg|chief security|security officer|head of security|"
                r"seguridad|segurança|sécurité|sicurezza|sicherheit")),
    ("data_ai", "AI & data",
     re.compile(r"\bcdao\b|chief data|chief analytics|chief ai\b|chief artificial|artificial intelligence|\bai\b|"
                r"machine learning|\bml\b|data science|head of data|data (?:&|and) analytics|analytics|genai|generative|"
                r"\bdatos\b|\bdados\b|données|\bdaten\b|inteligencia artificial|intelligence artificielle|anal[ií]tica")),
    ("cloud_infra", "cloud & infrastructure",
     re.compile(r"cloud|infrastructure|infraestructura|infraestrutura|platform engineering|\bplatforms?\b|devops|"
                r"site reliability|\bsre\b|engineering|ingenier[ií]a")),
    ("enterprise_apps", "enterprise applications",
     re.compile(r"\berp\b|\bcrm\b|salesforce|\bsap\b|enterprise applications|core banking|core systems")),
    ("digital", "digital",
     re.compile(r"chief digital|\bdigital\b")),
    ("strategy", "strategy & transformation",
     re.compile(r"transformation|transformaci[oó]n|transformação|strategy|estrategia|innovation|innovaci[oó]n|inovação")),
    ("operations", "operations",
     re.compile(r"\bcoo\b|chief operating|operating officer|head of (?:business )?operations|"
                r"director de operaciones|directeur des opérations")),
    ("procurement", "procurement",
     re.compile(r"procurement|sourcing|vendor management|third[- ]party")),
    ("finance", "finance",
     re.compile(r"\bcfo\b|chief financial|\bfinance\b")),
    ("risk", "risk & compliance",
     re.compile(r"\bcro\b|chief risk|compliance|regulatory|risk officer|head of risk")),
    ("technology", "technology",
     re.compile(r"\bcto\b|\bcio\b|\bcito\b|chief technology|chief information officer|chief information technology|"
                r"head of (?:technology|it)\b|\btechnology\b|tecnolog|technologie|informatique|\bsistemas\b")),
]
FUNCTION_LABEL = {key: label for key, label, _ in FUNCTIONS}

# Which functions speak to which initiative, most specific first. "technology"
# is last everywhere: it is the fallback, never the reason a match is specific.
INITIATIVE_FUNCTIONS: Dict[str, List[str]] = {
    "cloud_migration": ["cloud_infra", "technology"],
    "ai_genai_ml": ["data_ai", "digital", "technology"],
    "data_platform_analytics": ["data_ai", "technology"],
    "cybersecurity_zero_trust": ["security", "technology"],
    "core_system_modernization": ["enterprise_apps", "operations", "technology"],
    "erp_crm_enterprise_apps": ["enterprise_apps", "operations", "technology"],
    "devsecops_platform_eng": ["cloud_infra", "security", "technology"],
    "digital_transformation": ["digital", "strategy", "technology"],
    "cost_optimization": ["procurement", "finance", "operations", "technology"],
    "esg_sustainability_tech": ["strategy", "technology"],
    "executive_leadership_change": ["technology", "digital", "data_ai", "security"],
    "ma_integration": ["strategy", "operations", "technology"],
    "capex_it_budget": ["finance", "technology"],
    "vendor_partnership_rfp": ["procurement", "technology"],
    "regulatory_compliance_tech": ["risk", "security", "technology"],
}

_SENIOR = re.compile(r"\bchief\b|\bhead\b|\bevp\b|\bsvp\b|executive vice president|senior vice president|"
                     r"managing director|general manager|group head|\bvp\b|vice president|\bdirector\b|"
                     r"\bdirectora\b|\bdirecteur|\bdiretor|\bdirettore|\bresponsable\b|\bresponsabile\b|"
                     r"\bjef[ea]\b|\bleiter|\bsubdirector|gerente general|\bpresidente\b|"
                     r"\b(?:cto|cio|cito|ciso|cdo|cdao|coo|cfo|cro)\b")
_C_LEVEL = re.compile(r"\bchief\b|\b(?:cto|cio|cito|ciso|cdo|cdao|coo|cfo|cro)\b")
_TIER_TWO = re.compile(r"\bhead\b|\bevp\b|\bsvp\b|executive vice president|senior vice president|"
                       r"managing director|general manager|group head")
_FORMER = re.compile(r"\b(?:ex|former|formerly|retired|previously|past|antiguo|ancien|ehemalige[rn]?)\b")


def role_segment(headline: str) -> str:
    """
    The part of a headline that states the job. Headlines read "Title at
    Company | tagline | tagline"; the taglines are self-description ("Leading
    270+ people | Digital transformation") and must not decide what the
    person's role covers. The first segment carrying a senior title is the
    role; failing that, the first segment.
    """
    segs = [x.strip() for x in re.split(r"\s*[|·•]\s*", headline or "") if x.strip()]
    for seg in segs:
        if _SENIOR.search(seg.lower()):
            return seg
    return segs[0] if segs else (headline or "")


def classify_title(headline: str) -> List[str]:
    headline = role_segment(headline)
    low = (headline or "").lower()
    found = [key for key, _, rx in FUNCTIONS if rx.search(low)]
    # "IT" only counts in capitals; lower-case "it" is a pronoun.
    if "technology" not in found and re.search(r"\bIT\b", headline or ""):
        found.append("technology")
    return found


def seniority_of(headline: str) -> int:
    low = role_segment(headline or "").lower()
    # "Chief Manager" is middle management in Japanese and Indian banks, and
    # "Chief General Manager" one level below the executive board.
    if re.search(r"\bchief general manager\b", low):
        return 2
    if re.search(r"\bchief manager\b", low) and not re.search(r"\bchief (?!manager)\w+ officer\b|\b(?:cto|cio|ciso|cdo)\b", low):
        return 1
    if _C_LEVEL.search(low):
        return 3
    if _TIER_TWO.search(low):
        return 2
    return 1


# ------------------------------------------------------------ company names
_SUFFIXES = re.compile(r"[,\s]+(?:inc\.?|incorporated|corp\.?|corporation|co\.?|company|plc|ltd\.?|limited|"
                       r"llc|n\.?v\.?|s\.?a\.?|ag|se|sa|spa|s\.p\.a\.?|holdings?)$", re.I)
_GENERIC = {"financial", "group", "holdings", "holding", "insurance", "bank", "banking", "bancorp",
            "bancorporation", "corporation", "company", "services", "inc", "corp", "plc"}
# Words a legal name carries that its everyday brand drops: "First American
# Financial Corp" is "First American"; "Old Republic International" is "Old
# Republic". Only ever stripped from the end of the name.
_TRAILING = _GENERIC | {"international", "indemnity", "lifeco", "trust", "co"}
# Descriptor prefixes the brand drops: "Svenska Handelsbanken" is
# "Handelsbanken", "Banco Bradesco" is "Bradesco".
_PREFIXES = {"svenska", "banco", "banca", "banque", "assicurazioni", "grupo", "groupe", "gruppo"}
# Words that may directly follow a matched company name without it becoming a
# different company. "Swiss Re Corporate Solutions" is Swiss Re; "HDFC Life"
# is not HDFC Bank, which is why "life" is not in this set.
_ALLOWED_NEXT = _GENERIC | {"corporate", "solutions", "global", "international", "europe", "emea", "tech",
                            "americas", "asia", "apac", "uk", "us", "usa", "india", "north", "america",
                            "at", "in", "and", "for", "of",
                            # a country unit of the same group ("AXA France")
                            "france", "germany", "deutschland", "spain", "españa", "italy", "italia",
                            "japan", "china", "mexico", "méxico", "brazil", "brasil", "canada", "australia",
                            "singapore", "switzerland", "schweiz", "belgium", "ireland", "poland", "portugal",
                            "netherlands", "nordics", "latam", "africa", "chile", "colombia", "peru",
                            "indonesia", "malaysia", "thailand", "philippines", "vietnam", "korea", "turkey"}
_SHORT_STOP = {"american", "national", "general", "united", "first", "capital", "state", "royal",
               "standard", "legal", "global", "international", "principal", "new", "old", "great",
               "northern", "southern", "western", "eastern", "china", "japan", "india", "bank",
               "central", "citizens", "fifth", "regions", "discover", "progressive",
               "industrial", "commercial", "federal", "mutual", "security", "guardian", "liberty",
               "pacific", "atlantic", "heritage", "pioneer", "summit", "alliance", "life", "sun"}


def _fold(text: str) -> str:
    """Strip accents so "Itaú" and "Itau" compare equal."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(ch))


def _clean(name: str) -> str:
    n = re.sub(r"\([^)]*\)", " ", name or "").strip()
    n = re.sub(r"^the\s+", "", n, flags=re.I)
    prev = None
    while prev != n:
        prev = n
        n = _SUFFIXES.sub("", n).strip(" ,.&")
    return re.sub(r"\s+", " ", n)


def name_variants(company: Company) -> Tuple[List[str], List[str]]:
    """(lower-case phrases, case-sensitive abbreviations) that identify the company."""
    phrases, abbrevs = set(), set()
    for raw in [company.name] + list(company.aliases or []):
        for ab in re.findall(r"\(([A-Z][A-Za-z&]{1,9})\)", raw or ""):
            abbrevs.add(ab)
        # A short capitalised brand followed only by corporate words is how
        # these banks are named in headlines: "ANZ Group" -> "ANZ", "HDFC BANK"
        # -> "HDFC". Matched case-sensitively, so the word in prose cannot count.
        parts = _clean(raw).split()
        if (len(parts) > 1 and 3 <= len(parts[0]) <= 5 and parts[0].isupper() and parts[0].isalpha()
                and all(w.lower() in _GENERIC for w in parts[1:])):
            abbrevs.add(parts[0])
        # A one-word name shorter than a phrase match allows ("AXA", "UBS",
        # "ING"): match it exactly as written, case-sensitively.
        if len(parts) == 1 and 2 <= len(parts[0]) < 4 and parts[0].isalpha():
            abbrevs.add(parts[0])
        c = _fold(_clean(raw)).lower()
        if len(c) >= 4:
            phrases.add(c)
            words = c.split()
            # The everyday brand: trailing corporate words dropped. Two words
            # left is distinctive enough ("first american", "east west"); one
            # word must be long, not a common word, and is still subject to the
            # next-word check in _names_company.
            core = list(words)
            while len(core) > 1 and core[-1] in _TRAILING:
                core.pop()
            if len(core) >= 2 or (len(core) == 1 and len(core[0]) >= 4 and core[0] not in _SHORT_STOP):
                if core != words:
                    phrases.add(" ".join(core))
            if len(words) >= 2 and words[0] in _PREFIXES and len(" ".join(words[1:])) >= 5:
                phrases.add(" ".join(words[1:]))
            # "Voya" for Voya Financial, "Lloyds" for Lloyds Banking Group - but
            # never "Punjab" for Punjab National Bank ("Punjab & Sind Bank" is
            # another bank) or "American" for American International Group.
            if (len(words) > 1 and len(words[0]) >= 4 and words[0] not in _SHORT_STOP
                    and all(w in _GENERIC for w in words[1:])):
                phrases.add(words[0])
    if company.ticker and len(company.ticker) >= 3 and company.ticker.isalpha():
        abbrevs.add(company.ticker.upper())
    return sorted(phrases, key=len, reverse=True), sorted(abbrevs)


# Different companies whose names begin like one we track. No wording rule
# separates them - "East West Banking Corp" (the Philippines) reads exactly
# like a unit of East West Bancorp (the US) - so they are listed explicitly.
# A headline containing one of these never counts as naming our company.
_NOT_OUR_COMPANY = [
    "east west banking",      # EastWest Bank, Philippines - not East West Bancorp (US)
    "eastwest bank",
    "china merchants industry",
    "china merchants securities",
    "punjab & sind",
]


def _names_company(headline: str, company: Company) -> bool:
    headline = _fold(headline)
    low = headline.lower()
    if any(x in low for x in _NOT_OUR_COMPANY):
        return False
    phrases, abbrevs = name_variants(company)
    for ph in phrases:
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(ph) + r"(?![a-z0-9])", low):
            nxt = re.match(r"\s+([a-z][a-z'&.-]*)", low[m.end():])
            if not nxt or nxt.group(1).strip(".") in _ALLOWED_NEXT:
                return True
    for ab in abbrevs:
        for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(ab) + r"(?![A-Za-z0-9])", headline):
            nxt = re.match(r"\s+([A-Za-z][A-Za-z'&.-]*)", headline[m.end():])
            if not nxt or nxt.group(1).lower().strip(".") in _ALLOWED_NEXT:
                return True
    return False


# ----------------------------------------------------------------- profiles
_CREDENTIALS = {"bsc", "msc", "mba", "phd", "cfa", "cpa", "frm", "cism", "cissp", "pmp", "hons", "ceng",
                "fca", "acca", "ma", "ba", "llb", "mtech", "btech", "be", "me", "pgdm", "cciso", "cdpse",
                "crisc", "cisa", "itil", "cipp", "cipm", "faia", "fcii", "acii", "safe"}


def clean_person_name(raw: str) -> str:
    """Drop post-nominals and credentials that profiles append to the name."""
    n = (raw or "").strip()
    if n.isupper() or n.islower():
        n = n.title()
    out = []
    for w in n.replace(",", " ").split():
        bare = w.strip("().|").lower()
        if len(out) >= 2 and (bare in _CREDENTIALS or (w.isupper() and len(bare) >= 2 and not w.startswith("("))):
            break
        if w in ("|", "-", "–", "·"):
            break
        out.append(w)
    out = [w.capitalize() if w.isupper() and len(w.strip("().")) > 2 and not w.startswith("(") else w
           for w in out]
    return " ".join(out) or n


def parse_profile(result: dict) -> Optional[dict]:
    """Name, headline and location from an Exa LinkedIn-profile result."""
    url = (result.get("url") or "").strip()
    if "linkedin.com/in/" not in url:
        return None
    lines = [ln.strip() for ln in (result.get("text") or "").splitlines() if ln.strip()]
    name = lines[0].lstrip("# ").strip() if lines else ""
    headline = lines[1] if len(lines) > 1 else ""
    location = lines[2] if len(lines) > 2 and not re.search(r"connections|followers", lines[2]) else None
    title = result.get("title") or ""
    if not name:
        name = title.split(" - ")[0].strip()
    if (not headline or headline.startswith("#")) and " - " in title:
        headline = title.split(" - ", 1)[1].strip()
    if not name or not headline:
        return None
    name = clean_person_name(name)
    key = re.sub(r"^https?://(?:[a-z]{2,3}\.)?(?:www\.)?", "", url.lower()).split("?")[0].rstrip("/")
    return {"name": name[:200], "headline": headline[:500], "location": (location or "")[:200] or None,
            "linkedin_url": url.split("?")[0], "profile_key": key}


def verify(profile: dict, company: Company) -> Tuple[bool, str]:
    h = profile["headline"]
    low = h.lower()
    if _FORMER.search(low):
        return False, "headline describes a former role"
    if not _SENIOR.search(low):
        return False, "no senior title in headline"
    if not classify_title(h):
        return False, "title outside the functions we map to opportunities"
    if not _names_company(h, company):
        return False, "headline does not name this company"
    return True, "ok"


def _query_for(company: Company) -> str:
    base = _clean((company.aliases or [company.name])[0] or company.name)
    if base.isupper():
        base = base.title()
    return (f"{base} chief technology officer OR chief information officer OR chief digital officer "
            f"OR chief data officer OR CISO OR head of AI OR head of cloud OR head of technology")


def search_company(company: Company):
    """(verified or None on failure, results seen, error, rejected, all candidates)."""
    if not settings.EXA_API_KEY:
        return None, 0, "EXA_API_KEY not set", [], []
    if budget.quota_exhausted or budget.budget_exhausted:
        return None, 0, "people search budget exhausted for this month", [], []
    try:
        r = httpx.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": settings.EXA_API_KEY, "Content-Type": "application/json"},
            json={"query": _query_for(company), "category": "linkedin profile", "numResults": 10,
                  "contents": {"text": {"maxCharacters": 600}}},
            timeout=40,
        )
        budget.record_usage()
    except Exception as ex:
        return None, 0, f"request failed: {str(ex)[:120]}", [], []
    if r.status_code in (402, 429):
        budget.mark_quota_exhausted()
        return None, 0, f"Exa HTTP {r.status_code}", [], []
    if r.status_code != 200:
        return None, 0, f"Exa HTTP {r.status_code}: {r.text[:120]}", [], []

    results = r.json().get("results", [])
    candidates, seen = [], set()
    for res in results:
        prof = parse_profile(res)
        if prof and prof["profile_key"] not in seen:
            seen.add(prof["profile_key"])
            candidates.append(prof)
    verified, rejected = judge(candidates, company)
    return verified, len(results), None, rejected, candidates


def judge(candidates: List[dict], company: Company) -> Tuple[List[dict], List[dict]]:
    """Split parsed profiles into (verified, rejected-with-reason)."""
    verified, rejected = [], []
    for c in candidates:
        ok, why = verify(c, company)
        if not ok:
            rejected.append({"name": c["name"], "headline": c["headline"], "why": why})
            continue
        verified.append({**c, "functions": classify_title(c["headline"]), "seniority": seniority_of(c["headline"])})
    return verified, rejected


# ------------------------------------------------------------------ refresh
def refresh_company(db: Session, company: Company, reason: str) -> dict:
    """Search one company and store what verifies. Never wipes on failure."""
    now = datetime.datetime.utcnow()
    verified, seen, err, rejected, candidates = search_company(company)
    state = db.get(LeaderFetch, company.id) or LeaderFetch(company_id=company.id)
    state.fetched_at, state.reason, state.results_seen = now, reason, seen

    if verified is None:
        state.status, state.error, state.verified = "error", (err or "")[:300], 0
        db.merge(state)
        db.commit()
        return {"company": company.name, "status": "error", "error": err, "verified": 0}

    state.candidates = candidates
    _apply(db, company, verified, now)
    state.status = "ok" if verified else "none_verified"
    state.verified, state.error = len(verified), None
    db.merge(state)
    db.commit()
    return {"company": company.name, "status": state.status, "verified": len(verified),
            "seen": seen, "kept": [(p["name"], p["headline"]) for p in verified], "rejected": rejected}


def _apply(db: Session, company: Company, verified: List[dict], now: datetime.datetime):
    """Make `verified` the company's current leaders. A successful search is the
    current picture: people it no longer finds stop being current, kept as history."""
    existing = {l.profile_key: l for l in db.query(Leader).filter(Leader.company_id == company.id).all()}
    found = set()
    for p in verified:
        found.add(p["profile_key"])
        row = existing.get(p["profile_key"])
        if row is None:
            row = Leader(company_id=company.id, profile_key=p["profile_key"], first_seen_at=now)
            db.add(row)
        row.name, row.headline, row.location = clean_person_name(p["name"]), p["headline"], p["location"]
        row.functions, row.seniority, row.linkedin_url = p["functions"], p["seniority"], p["linkedin_url"]
        row.is_current, row.last_seen_at, row.source = True, now, "exa_linkedin"
    for key, row in existing.items():
        if key not in found:
            row.is_current = False


def reverify(db: Session) -> dict:
    """
    Re-run the verification rules over every stored search result, with no
    API call. Used after the rules change; leaders keep their original search
    date, since nothing new was fetched.
    """
    changed = 0
    for st in db.query(LeaderFetch).filter(LeaderFetch.status != "error").all():
        if not st.candidates:
            continue
        company = db.get(Company, st.company_id)
        verified, _ = judge(st.candidates, company)
        before = st.verified or 0
        _apply(db, company, verified, st.fetched_at)
        st.verified = len(verified)
        st.status = "ok" if verified else "none_verified"
        changed += int(before != len(verified))
    # Companies searched before raw results were stored: re-check the rows we
    # kept, from the headline each was kept for. Nothing can be added this way
    # (the rejected results are gone), but anything today's rules would reject
    # stops being current, and names and title tags pick up the new rules.
    dropped = 0
    legacy = {st.company_id for st in db.query(LeaderFetch).all() if not st.candidates}
    for row in db.query(Leader).filter(Leader.company_id.in_(legacy), Leader.is_current.is_(True)).all():
        ok, _ = verify({"headline": row.headline}, row.company)
        row.name = clean_person_name(row.name)
        row.functions, row.seniority = classify_title(row.headline), seniority_of(row.headline)
        if not ok:
            row.is_current = False
            dropped += 1
    db.commit()
    return {"companies_changed": changed, "legacy_rows_dropped": dropped}


def companies_in_scope(db: Session) -> Dict[str, int]:
    """Company id -> best opportunity score, for companies worth looking up."""
    from ..signals.engine import signal_engine
    end = datetime.datetime.utcnow()
    start = end - datetime.timedelta(days=settings.LEADERS_LOOKBACK_DAYS)
    best: Dict[str, int] = {}
    for s in signal_engine.compute_window(db, start, end, sector=settings.SCHEDULE_SECTOR_FILTER):
        if s["intent_score"] >= settings.LEADERS_MIN_SCORE:
            best[s["company_id"]] = max(best.get(s["company_id"], 0), s["intent_score"])
    return best


def companies_due(db: Session) -> List[Tuple[Company, str]]:
    """In-scope companies that need a search, most urgent first."""
    scope = companies_in_scope(db)
    if not scope:
        return []
    ids = list(scope)
    states = {s.company_id: s for s in db.query(LeaderFetch).filter(LeaderFetch.company_id.in_(ids)).all()}
    news = dict(db.query(Event.company_id, func.max(Event.created_at))
                .filter(Event.initiative_id == "executive_leadership_change", Event.company_id.in_(ids))
                .group_by(Event.company_id).all())
    now = datetime.datetime.utcnow()
    ttl = datetime.timedelta(days=settings.LEADERS_TTL_DAYS)
    due = []
    for cid, score in scope.items():
        st = states.get(cid)
        if st is None:
            due.append((1, -score, cid, "initial"))
        elif news.get(cid) and news[cid] > st.fetched_at:
            due.append((0, -score, cid, "leadership news"))
        elif st.status == "error" and now - st.fetched_at > datetime.timedelta(days=1):
            due.append((2, -score, cid, "retry"))
        elif st.status == "none_verified" and not st.candidates:
            # Searched before raw results were stored, and nothing verified -
            # so today's rules have never seen this company's results. One
            # more search stores them; after that, reverify() handles rule
            # changes for free and this branch no longer applies.
            due.append((2, -score, cid, "retry"))
        elif now - st.fetched_at > ttl:
            due.append((3, -score, cid, "stale"))
    due.sort()
    companies = {c.id: c for c in db.query(Company).filter(Company.id.in_([d[2] for d in due])).all()}
    return [(companies[cid], reason) for _, _, cid, reason in due if cid in companies]


def refresh_due(db: Session, max_calls: Optional[int] = None) -> dict:
    """Search the companies that are due, within budget. Used by the daily job."""
    due = companies_due(db)
    done, reasons = [], {}
    for company, reason in due:
        if max_calls is not None and len(done) >= max_calls:
            break
        if budget.budget_exhausted or budget.quota_exhausted:
            break
        done.append(refresh_company(db, company, reason))
        reasons[reason] = reasons.get(reason, 0) + 1
    return {"due": len(due), "searched": len(done),
            "verified": sum(d.get("verified", 0) for d in done),
            "by_reason": reasons, "budget": budget.get_usage_status()}


# ------------------------------------------------------------------ reading
def current_leaders(db: Session, company_ids) -> Dict[str, List[Leader]]:
    out: Dict[str, List[Leader]] = {}
    ids = list(company_ids)
    if not ids:
        return out
    for l in db.query(Leader).filter(Leader.company_id.in_(ids), Leader.is_current.is_(True)).all():
        out.setdefault(l.company_id, []).append(l)
    return out


def match_leaders(leaders: List[Leader], initiative_id: str, limit: int = 2) -> List[dict]:
    """
    Leaders relevant to one initiative, most specific first. Each carries the
    reason it is shown, so the tile never implies more than the title supports.
    """
    wanted = INITIATIVE_FUNCTIONS.get(initiative_id, ["technology"])
    ranked = []
    for l in leaders:
        fns = l.functions or []
        hits = [f for f in wanted if f in fns]
        if not hits:
            continue
        # For a leadership-change opportunity the technology team is the
        # point, so "technology" counts as a specific match there.
        if initiative_id == "executive_leadership_change":
            specific = hits[0]
        else:
            specific = next((f for f in hits if f != "technology"), None)
        rank = wanted.index(specific) if specific else len(wanted)
        reason = (f"Title covers {FUNCTION_LABEL[specific]}" if specific
                  else "Senior technology leader")
        ranked.append((rank, -(l.seniority or 1), l.name,
                       {**l.to_dict(), "match": "specific" if specific else "general", "reason": reason}))
    ranked.sort(key=lambda r: r[:3])
    return [r[3] for r in ranked[:limit]]


# ------------------------------------------------------- search on first view
import threading

_INFLIGHT: set = set()
_INFLIGHT_LOCK = threading.Lock()


def missing_ids(db: Session, company_ids) -> List[str]:
    """Companies never searched, or whose last search failed more than a day ago."""
    ids = list(company_ids)
    if not ids:
        return []
    states = {s.company_id: s for s in db.query(LeaderFetch).filter(LeaderFetch.company_id.in_(ids)).all()}
    day = datetime.timedelta(days=1)
    now = datetime.datetime.utcnow()
    return [cid for cid in ids
            if cid not in states or (states[cid].status == "error" and now - states[cid].fetched_at > day)]


def search_missing(company_ids: List[str], limit: int = 3) -> None:
    """
    Run as a background task after a page is served: look up leaders for
    companies on screen that were never searched, so they appear on the next
    load instead of the tile showing an empty "not searched" row. At most
    `limit` per call, never the same company twice at once, within budget.
    """
    from ..db.database import SessionLocal
    with _INFLIGHT_LOCK:
        todo = [c for c in company_ids if c not in _INFLIGHT][:limit]
        _INFLIGHT.update(todo)
    if not todo:
        return
    db = SessionLocal()
    try:
        for cid in todo:
            if budget.budget_exhausted or budget.quota_exhausted:
                break
            state = db.get(LeaderFetch, cid)
            if state and state.status != "error":
                continue                      # searched meanwhile
            company = db.get(Company, cid)
            if company:
                refresh_company(db, company, "on view")
    except Exception as ex:
        logger.warning(f"background leader search failed: {ex}")
    finally:
        db.close()
        with _INFLIGHT_LOCK:
            _INFLIGHT.difference_update(todo)
