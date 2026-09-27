import datetime
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..models.schema import Company, Event, Signal
from ..models.taxonomy import taxonomy_manager
from .scoring import score_event_group
from .alerts import generate_alerts

logger = logging.getLogger(__name__)


class SignalEngine:
    """
    Aggregates Events into one ranked Signal per (company, initiative), then
    runs the peer-context and alert passes. Idempotent: re-running upserts.
    """

    def recompute(self, db: Session, sector_filter: Optional[str] = None) -> dict:
        now = datetime.datetime.utcnow()
        cutoff = now - datetime.timedelta(days=settings.SIGNAL_LOOKBACK_DAYS)

        query = db.query(Event).options(joinedload(Event.company)).filter(Event.occurred_at >= cutoff)
        if sector_filter:
            query = query.join(Company).filter(Company.sector == sector_filter)
        events = query.all()

        groups: Dict[Tuple[str, str], List[Event]] = defaultdict(list)
        for e in events:
            groups[(e.company_id, e.initiative_id)].append(e)

        existing: Dict[Tuple[str, str], Signal] = {
            (s.company_id, s.initiative_id): s for s in db.query(Signal).all()
        }

        created = updated = 0
        touched: List[Signal] = []
        for (company_id, initiative_id), group in groups.items():
            scored = score_event_group(group, now=now)
            sample = group[0]
            initiative_def = taxonomy_manager.get_initiative(initiative_id)
            it_offering = initiative_def.it_offering if initiative_def else sample.it_offering

            sig = existing.get((company_id, initiative_id))
            if sig is None:
                sig = Signal(company_id=company_id, initiative_id=initiative_id)
                sig.previous_intent_score = None
                db.add(sig)
                created += 1
            else:
                sig.previous_intent_score = sig.intent_score
                updated += 1
            sig.initiative_name = sample.initiative_name
            sig.category_id = sample.category_id
            sig.category_name = sample.category_name
            sig.it_offering = it_offering
            sig.intent_score = scored["intent_score"]
            sig.score_breakdown = scored["score_breakdown"]
            sig.event_count = scored["event_count"]
            sig.source_types = scored["source_types"]
            sig.distinct_source_count = scored["distinct_source_count"]
            sig.first_seen_at = scored["first_seen_at"]
            sig.last_seen_at = scored["last_seen_at"]
            sig.timing_window = scored["timing_window"]
            sig.timing_estimate = scored["timing_estimate"]
            sig.stated_timing = scored["stated_timing"]
            sig.stated_spend = scored["stated_spend"]
            sig.top_event_ids = scored["top_event_ids"]
            sig.status = "active"
            sig.computed_at = now
            touched.append(sig)

        # Signals with no in-window events go stale (kept for history).
        stale = 0
        in_scope_company_ids = None
        if sector_filter:
            in_scope_company_ids = {r[0] for r in db.query(Company.id).filter(Company.sector == sector_filter).all()}
        for key, sig in existing.items():
            if key in groups or sig.status == "stale":
                continue
            if in_scope_company_ids is not None and sig.company_id not in in_scope_company_ids:
                continue
            sig.status = "stale"
            sig.computed_at = now
            stale += 1

        db.flush()
        self._compute_peer_context(db)
        alerts_created = generate_alerts(db, touched)
        db.commit()

        result = {
            "events_considered": len(events),
            "signals_created": created,
            "signals_updated": updated,
            "signals_stale": stale,
            "alerts_created": alerts_created,
            "lookback_days": settings.SIGNAL_LOOKBACK_DAYS,
            "computed_at": now.isoformat(),
        }
        logger.info(f"Signal recompute: {result}")
        return result

    def _compute_peer_context(self, db: Session):
        """Top-3 same-industry (fallback: same-sector) companies with an active
        signal on the same initiative — the competitive context for a lead."""
        active = db.query(Signal).options(joinedload(Signal.company)).filter(Signal.status == "active").all()
        peers = _peer_context_for(
            [
                {
                    "company_id": s.company_id,
                    "initiative_id": s.initiative_id,
                    "intent_score": s.intent_score,
                    "name": s.company.name if s.company else None,
                    "ticker": s.company.ticker if s.company else None,
                    "industry": s.company.industry if s.company else None,
                    "sector": s.company.sector if s.company else None,
                }
                for s in active
            ]
        )
        for s in active:
            s.peer_context = peers.get((s.company_id, s.initiative_id), [])

    def compute_window(
        self,
        db: Session,
        start: Optional[datetime.datetime],
        end: datetime.datetime,
        sector: Optional[str] = None,
        industry: Optional[str] = None,
        region: Optional[str] = None,
        country: Optional[str] = None,
        category_id: Optional[str] = None,
        initiative_id: Optional[str] = None,
        company_id: Optional[str] = None,
        ticker: Optional[str] = None,
    ) -> List[dict]:
        """
        Score signals over an arbitrary date window, computed live rather than
        read from the stored `signals` table.

        The stored table is a fixed SIGNAL_LOOKBACK_DAYS aggregate used for
        alerting; filtering it by date would mislabel a 180-day score as a
        30-day one. Re-scoring from events is cheap (~25ms across the full
        dataset) and is the only way the number can honestly match the label.
        """
        query = db.query(Event).options(joinedload(Event.company)).filter(Event.occurred_at <= end)
        if start is not None:
            query = query.filter(Event.occurred_at >= start)

        needs_company_join = bool(sector or industry or ticker or region or country)
        if needs_company_join:
            query = query.join(Company)
            if sector:
                query = query.filter(Company.sector == sector)
            if industry:
                query = query.filter(Company.industry == industry)
            if region:
                query = query.filter(Company.region == region)
            if country:
                query = query.filter(Company.country == country)
            if ticker:
                query = query.filter(Company.ticker == ticker.upper())
        if company_id:
            query = query.filter(Event.company_id == company_id)
        if category_id:
            query = query.filter(Event.category_id == category_id)
        if initiative_id:
            query = query.filter(Event.initiative_id == initiative_id)

        events = query.all()

        groups: Dict[Tuple[str, str], List[Event]] = defaultdict(list)
        for e in events:
            groups[(e.company_id, e.initiative_id)].append(e)

        now = datetime.datetime.utcnow()
        signals: List[dict] = []
        for (cid, init_id), group in groups.items():
            scored = score_event_group(group, now=now)
            sample = group[0]
            company = sample.company
            initiative_def = taxonomy_manager.get_initiative(init_id)
            signals.append({
                # Deterministic and window-independent, so an expanded
                # "Why this signal?" panel survives a date-range change.
                "id": f"{cid}__{init_id}",
                "company_id": cid,
                "company_name": company.name if company else None,
                "company_ticker": company.ticker if company else None,
                "company_sector": company.sector if company else None,
                "company_industry": company.industry if company else None,
                "company_region": company.region if company else None,
                "company_country": company.country if company else None,
                "initiative_id": init_id,
                "initiative_name": sample.initiative_name,
                "category_id": sample.category_id,
                "category_name": sample.category_name,
                "it_offering": initiative_def.it_offering if initiative_def else sample.it_offering,
                "intent_score": scored["intent_score"],
                "strength": scored["strength"],
                "points_explained": scored["points_explained"],
                # A delta against the previous *recompute* is meaningless in a
                # windowed view, so windowed cards don't show one.
                "previous_intent_score": None,
                "score_breakdown": scored["score_breakdown"],
                "event_count": scored["event_count"],
                "document_count": scored["document_count"],
                "source_types": scored["source_types"],
                "distinct_source_count": scored["distinct_source_count"],
                "first_seen_at": scored["first_seen_at"].isoformat() if scored["first_seen_at"] else None,
                "last_seen_at": scored["last_seen_at"].isoformat() if scored["last_seen_at"] else None,
                "timing_window": scored["timing_window"],
                "timing_estimate": scored["timing_estimate"],
                "stated_timing": scored["stated_timing"],
                "stated_spend": scored["stated_spend"],
                "top_event_ids": scored["top_event_ids"],
                "status": "active",
                "computed_at": now.isoformat(),
                # filled in below
                "peer_context": [],
            })

        peers = _peer_context_for([
            {
                "company_id": s["company_id"],
                "initiative_id": s["initiative_id"],
                "intent_score": s["intent_score"],
                "name": s["company_name"],
                "ticker": s["company_ticker"],
                "industry": s["company_industry"],
                "sector": s["company_sector"],
            }
            for s in signals
        ])
        for s in signals:
            s["peer_context"] = peers.get((s["company_id"], s["initiative_id"]), [])

        signals.sort(key=lambda s: (s["intent_score"], s["last_seen_at"] or ""), reverse=True)
        return signals


def _peer_context_for(rows: List[dict]) -> Dict[Tuple[str, str], List[dict]]:
    """
    Shared peer-context computation for both the stored recompute and the
    windowed path. `rows` are lightweight dicts with company_id, initiative_id,
    intent_score, name, ticker, industry, sector. Returns a lookup keyed by
    (company_id, initiative_id).
    """
    by_initiative: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        by_initiative[r["initiative_id"]].append(r)

    out: Dict[Tuple[str, str], List[dict]] = {}
    for r in rows:
        if not r.get("name"):
            out[(r["company_id"], r["initiative_id"])] = []
            continue
        candidates = [p for p in by_initiative[r["initiative_id"]] if p["company_id"] != r["company_id"] and p.get("name")]
        same_industry = [p for p in candidates if p["industry"] == r["industry"]]
        pool = same_industry if len(same_industry) >= 2 else [p for p in candidates if p["sector"] == r["sector"]]
        pool.sort(key=lambda p: p["intent_score"], reverse=True)
        out[(r["company_id"], r["initiative_id"])] = [
            {
                "company_id": p["company_id"],
                "name": p["name"],
                "ticker": p["ticker"],
                "intent_score": p["intent_score"],
                "scope": "industry" if p["industry"] == r["industry"] else "sector",
            }
            for p in pool[:3]
        ]
    return out


signal_engine = SignalEngine()
