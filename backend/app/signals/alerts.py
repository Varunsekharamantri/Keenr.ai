import logging
from typing import List, Optional, Set
import httpx
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..models.schema import Alert, Company, Signal, Watchlist

logger = logging.getLogger(__name__)


def _matching_company_ids(db: Session, watchlist: Watchlist) -> Set[str]:
    if watchlist.company_ids:
        return set(watchlist.company_ids)
    if watchlist.icp_sectors:
        rows = db.query(Company.id).filter(Company.sector.in_(watchlist.icp_sectors)).all()
        return {r[0] for r in rows}
    return set()


def _post_slack(message: str) -> bool:
    if not settings.SLACK_WEBHOOK_URL:
        return False
    try:
        resp = httpx.post(settings.SLACK_WEBHOOK_URL, json={"text": message}, timeout=10.0)
        return resp.status_code < 300
    except Exception as e:
        logger.warning(f"Slack alert delivery failed: {e}")
        return False


def build_alert_message(signal: Signal, threshold: int, initial: bool = False) -> str:
    company = signal.company.name if signal.company else signal.company_id
    top_reasons = (signal.score_breakdown or {}).get("reasons", [])[:3]
    reason_str = "; ".join(top_reasons) if top_reasons else f"{signal.event_count} supporting events"

    if initial:
        # The score didn't move — this account was already above the bar when
        # it came into the watchlist's scope.
        headline = f"already at {signal.intent_score}, above your {threshold} threshold"
    else:
        prev = f" from {signal.previous_intent_score}" if signal.previous_intent_score is not None else ""
        headline = f"crossed {threshold}{prev} — now {signal.intent_score}"

    return (
        f"{company} — {signal.initiative_name} {headline} · "
        f"{signal.timing_window or 'timing n/a'} · {reason_str}"
    )


def _alerts_for_watchlist(db: Session, wl: Watchlist, signals: List[Signal], initial: bool) -> int:
    company_ids = _matching_company_ids(db, wl)
    if not company_ids:
        return 0  # empty watchlist with no ICP sectors matches nothing
    initiative_filter = set(wl.icp_initiative_ids or [])
    threshold = wl.alert_threshold if wl.alert_threshold is not None else settings.SIGNAL_DEFAULT_ALERT_THRESHOLD

    already_alerted: Set[str] = set()
    if initial:
        already_alerted = {
            r[0] for r in db.query(Alert.signal_id).filter(Alert.watchlist_id == wl.id).all()
        }

    created = 0
    for sig in signals:
        if sig.status != "active" or sig.intent_score < threshold:
            continue
        if sig.company_id not in company_ids:
            continue
        if initiative_filter and sig.initiative_id not in initiative_filter:
            continue
        if initial:
            if sig.id in already_alerted:
                continue
        else:
            crossed = sig.previous_intent_score is None or sig.previous_intent_score < threshold
            if not crossed:
                continue

        message = build_alert_message(sig, threshold, initial=initial)
        channels = ["in_app"]
        if _post_slack(f"[{wl.name}] {message}"):
            channels.append("slack")

        db.add(Alert(
            signal_id=sig.id,
            watchlist_id=wl.id,
            company_id=sig.company_id,
            initiative_id=sig.initiative_id,
            intent_score=sig.intent_score,
            previous_score=sig.previous_intent_score,
            message=message,
            delivered_channels=channels,
        ))
        created += 1
    return created


def generate_alerts(db: Session, recomputed_signals: List[Signal]) -> int:
    """
    Recompute-time pass: for every watchlist, alert on each matching signal that
    crossed the threshold in this recompute (previous score below it, or brand
    new). Returns the number of alerts created. Commits nothing — caller does.
    """
    watchlists = db.query(Watchlist).all()
    return sum(_alerts_for_watchlist(db, wl, recomputed_signals, initial=False) for wl in watchlists)


def initial_alerts_for_watchlist(db: Session, wl: Watchlist) -> int:
    """
    Called when a watchlist is created or its ICP/threshold changes: alert on
    every currently-matching signal already at/above threshold, since those
    accounts are "new" to this watchlist even though their scores didn't move.
    Skips signals this watchlist has already alerted on. Commits nothing.
    """
    signals = db.query(Signal).options(joinedload(Signal.company)).filter(Signal.status == "active").all()
    return _alerts_for_watchlist(db, wl, signals, initial=True)
