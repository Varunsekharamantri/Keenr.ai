"""
The Companies solar system: BFSI verticals (stars), their sub-industries
(planets) and the companies in each (satellites).

Each company carries its strongest opportunity in the selected window, so a
satellite can be coloured by Strength and the planet view tells a salesperson
at a glance where the activity is. Leadership changes are not opportunities
and do not count (see NON_OPPORTUNITY_INITIATIVES).
"""
import re
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import bfsi_taxonomy
from ..db.database import get_db
from ..models.schema import Company
from ..models.taxonomy import taxonomy_manager
from ..signals.engine import signal_engine
from ..signals.scoring import NON_OPPORTUNITY_INITIATIVES, strength_of
from .date_range import describe_window, resolve_window

router = APIRouter(prefix="", tags=["Galaxy"])

_SUFFIX = re.compile(r"[,\s]+(?:inc\.?|corp\.?|corporation|co\.?|ltd\.?|limited|plc|n\.v\.|s\.a\.|ag|group|"
                     r"holdings?|financial corp|bancorporation|incorporated)$", re.I)


def short_name(name: str) -> str:
    """ "TRUIST FINANCIAL CORP" -> "Truist Financial", for a satellite label."""
    n = (name or "").strip()
    if n.isupper():
        n = n.title()
    prev = None
    while prev != n:
        prev = n
        n = _SUFFIX.sub("", n).strip(" ,.")
    return n or name


@router.get("/galaxy")
def galaxy(start: Optional[str] = None, end: Optional[str] = None, db: Session = Depends(get_db)):
    if db.query(Company).filter(Company.sector == "BFSI", Company.sub_industry.is_(None)).count() > 1:
        bfsi_taxonomy.apply(db)          # first run, or newly seeded companies

    start_dt, end_dt = resolve_window(start, end)
    best = {}
    for s in signal_engine.compute_window(db, start_dt, end_dt, sector="BFSI"):
        if s["initiative_id"] in NON_OPPORTUNITY_INITIATIVES:
            continue
        cur = best.get(s["company_id"])
        if cur is None or s["intent_score"] > cur["intent_score"]:
            best[s["company_id"]] = s

    by_sub = {}
    for c in db.query(Company).filter(Company.sector == "BFSI", Company.sub_industry.isnot(None)).all():
        b = best.get(c.id)
        init = taxonomy_manager.get_initiative(b["initiative_id"]) if b else None
        by_sub.setdefault(c.sub_industry, []).append({
            "id": c.id,
            "name": c.name,
            "label": short_name(c.name),
            "ticker": c.ticker,
            "country": c.country,
            "region": c.region,
            "strength": strength_of(b["intent_score"]) if b else None,
            "points": b["intent_score"] if b else 0,
            "top_theme": (init.name if init else b["initiative_name"]) if b else None,
        })

    verticals = []
    for vname, subs in bfsi_taxonomy.VERTICALS:
        planets = []
        for sname in subs:
            cos = sorted(by_sub.get(sname, []), key=lambda x: (-x["points"], x["label"]))
            planets.append({
                "name": sname,
                "companies": cos,
                "count": len(cos),
                "high": sum(1 for x in cos if x["strength"] == "High"),
                "medium": sum(1 for x in cos if x["strength"] == "Medium"),
            })
        verticals.append({
            "name": vname,
            "planets": planets,
            "count": sum(p["count"] for p in planets),
            "high": sum(p["high"] for p in planets),
            "medium": sum(p["medium"] for p in planets),
        })
    return {"window": describe_window(start_dt, end_dt), "verticals": verticals,
            "total": sum(v["count"] for v in verticals)}
