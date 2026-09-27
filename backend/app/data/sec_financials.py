"""
Headline financials from SEC XBRL company facts.

Free, no new credentials - the same data.sec.gov host and contact User-Agent the
filing adapter already uses. Only companies with a CIK have filings, which is 93
of the 199 BFSI companies (mostly Americas); everyone else reports no data, and
the UI says so rather than inventing numbers.

Nothing here is estimated. Every figure is a value the company filed, and each
one is returned with the fiscal year, period end and form it came from so it can
be checked against the filing.
"""
import datetime
import json
import logging
from pathlib import Path
from typing import Optional

import httpx

from ..config import settings

logger = logging.getLogger("market_signals.financials")

CACHE_DIR = settings.DATA_DIR / "sec_facts"
CACHE_DAYS = 7          # filings change quarterly; a week is plenty
_TIMEOUT = 30.0

# Banks report revenue net of interest expense; insurers and others use the
# generic tags. Foreign filers (BBVA, Prudential plc) file 20-F under IFRS, so
# both taxonomies are searched.
#
# A company can switch tags between years - Erie Indemnity's "Revenues" stops in
# 2017 - so these are candidates, not a priority order: the tag whose newest
# annual figure is most recent wins.
REVENUE_TAGS = {
    "us-gaap": ["RevenuesNetOfInterestExpense", "Revenues",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "InterestAndDividendIncomeOperating"],
    "ifrs-full": ["Revenue", "RevenueFromContractsWithCustomers"],
}
INCOME_TAGS = {
    "us-gaap": ["NetIncomeLoss", "ProfitLoss"],
    "ifrs-full": ["ProfitLoss", "ProfitLossAttributableToOwnersOfParent"],
}
ASSET_TAGS = {"us-gaap": ["Assets"], "ifrs-full": ["Assets"]}
EQUITY_TAGS = {
    "us-gaap": ["StockholdersEquity",
                "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "ifrs-full": ["EquityAttributableToOwnersOfParent", "Equity"],
}

ANNUAL_FORMS = ("10-K", "20-F", "40-F")


def _cache_path(cik: str) -> Path:
    return CACHE_DIR / f"CIK{cik.zfill(10)}.json"


def fetch_facts(cik: str, force: bool = False) -> Optional[dict]:
    """Company facts for a CIK, cached on disk for CACHE_DAYS."""
    if not cik:
        return None
    path = _cache_path(cik)
    if path.exists() and not force:
        age = datetime.datetime.now() - datetime.datetime.fromtimestamp(path.stat().st_mtime)
        if age.days < CACHE_DAYS:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass                      # corrupt cache - refetch below
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json"
    try:
        r = httpx.get(url, headers={"User-Agent": settings.SEC_USER_AGENT}, timeout=_TIMEOUT)
        if r.status_code != 200:
            logger.info(f"SEC facts {r.status_code} for CIK {cik}")
            return None
        data = r.json()
    except Exception as ex:
        logger.warning(f"SEC facts failed for CIK {cik}: {ex}")
        return None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass                              # cache is an optimisation, not a requirement
    return data


def _points_for_tag(facts: dict, taxonomy: str, tag: str, flow: bool) -> list:
    """
    Annual values for one tag, newest first.

    `flow` metrics (revenue, income) cover a period, so a point must span a full
    year - without that check a quarterly figure filed inside a 10-K would be
    read as the annual number. `stock` metrics (assets, equity) are point-in-time.
    """
    node = (facts or {}).get("facts", {}).get(taxonomy, {}).get(tag)
    if not node:
        return []
    # Each currency is its own series - BBVA and Santander file in EUR. Mixing
    # currencies in one series would produce a meaningless year-on-year change.
    series = []
    for unit, entries in node.get("units", {}).items():
        if len(unit) != 3 or not unit.isalpha():
            continue                      # skip per-share and other non-currency units
        out = []
        for p in entries:
            if p.get("form") not in ANNUAL_FORMS or p.get("fp") != "FY":
                continue
            if flow:
                start, end = p.get("start"), p.get("end")
                if not start or not end:
                    continue
                days = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days
                if not (350 <= days <= 380):
                    continue
            out.append({"value": p["val"], "fy": p.get("fy"), "end": p.get("end"),
                        "form": p.get("form"), "tag": tag, "taxonomy": taxonomy,
                        "currency": unit.upper()})
        if not out:
            continue
        # One fiscal year can be filed repeatedly (restatements, comparatives);
        # keep one value per period end.
        by_end = {}
        for p in out:
            by_end.setdefault(p["end"], p)
        series.append(sorted(by_end.values(), key=lambda p: p["end"], reverse=True))
    return series


def _annual_points(facts: dict, tag_map: dict, flow: bool) -> list:
    """
    Best series across taxonomies, tags and currencies: the one whose newest
    annual figure is most recent, preferring USD when two are equally current.
    """
    best = []
    for taxonomy, tags in tag_map.items():
        for tag in tags:
            for pts in _points_for_tag(facts, taxonomy, tag, flow):
                if not best:
                    best = pts
                    continue
                newer = pts[0]["end"] > best[0]["end"]
                same_date_usd = (pts[0]["end"] == best[0]["end"]
                                 and pts[0]["currency"] == "USD" != best[0]["currency"])
                if newer or same_date_usd:
                    best = pts
    return best


def _metric(points: list, label: str) -> Optional[dict]:
    if not points:
        return None
    cur = points[0]
    prev = points[1] if len(points) > 1 else None
    change = None
    if prev and prev["value"]:
        change = round(100 * (cur["value"] - prev["value"]) / abs(prev["value"]), 1)
    return {
        "label": label,
        "value": cur["value"],
        "currency": cur.get("currency", "USD"),
        "fy": cur["fy"],
        "period_end": cur["end"],
        "form": cur["form"],
        "tag": cur["tag"],
        "change_pct": change,
        "previous_value": prev["value"] if prev else None,
    }


def financials_for(cik: Optional[str]) -> dict:
    """Headline financials for one company. Always returns a dict."""
    if not cik:
        return {"available": False, "reason": "No SEC filings: this company does not file with the SEC."}
    facts = fetch_facts(cik)
    if not facts:
        return {"available": False, "reason": "SEC filings could not be read for this company."}

    revenue = _metric(_annual_points(facts, REVENUE_TAGS, flow=True), "Revenue")
    income = _metric(_annual_points(facts, INCOME_TAGS, flow=True), "Net income")
    assets = _metric(_annual_points(facts, ASSET_TAGS, flow=False), "Total assets")
    equity_pts = _annual_points(facts, EQUITY_TAGS, flow=False)
    equity = _metric(equity_pts, "Shareholders' equity")

    roe = None
    if income and equity_pts and income["currency"] == equity_pts[0].get("currency"):
        # Return on average equity, the way banks report it.
        cur_eq = equity_pts[0]["value"]
        prev_eq = equity_pts[1]["value"] if len(equity_pts) > 1 else cur_eq
        avg_eq = (cur_eq + prev_eq) / 2
        if avg_eq:
            roe = round(100 * income["value"] / avg_eq, 1)

    metrics = [m for m in (revenue, income, assets, equity) if m]
    # Drop anything materially older than the rest. Santander last tagged
    # revenue in FY2017; showing it beside FY2025 assets would read as current.
    if metrics:
        newest = max(m["period_end"] for m in metrics)
        cutoff = (datetime.date.fromisoformat(newest) - datetime.timedelta(days=400)).isoformat()
        metrics = [m for m in metrics if m["period_end"] >= cutoff]
    return {
        "available": bool(metrics),
        "entity_name": facts.get("entityName"),
        "cik": cik,
        "metrics": metrics,
        "roe_pct": roe,
        "source": "SEC XBRL company facts",
        "reason": None if metrics else "No annual figures found in this company's filings.",
    }
