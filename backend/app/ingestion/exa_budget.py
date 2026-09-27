import datetime
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def with_date_floor(payload: dict, start_published_date: Optional[str]) -> dict:
    """
    Add Exa's `startPublishedDate` bound when a floor is requested, so a clean
    rebuild can guarantee "all news from date X onward" rather than whatever
    the index happened to surface.
    """
    if start_published_date:
        payload = dict(payload)
        payload["startPublishedDate"] = f"{start_published_date}T00:00:00.000Z"
    return payload


class ExaBudgetTracker:
    """
    Self-imposed monthly request budget for an Exa-backed adapter, enforced
    BEFORE any network call is made. Exa is metered (pay-as-you-go); this is
    a self-imposed cap, not Exa's own billing signal — it does not by itself
    guarantee zero charges if the Exa account has a card on file with
    auto-recharge enabled (disable that on Exa's own dashboard for an
    airtight guarantee).

    Each adapter that uses this should pass its OWN usage_file so budgets
    are tracked independently (e.g. news vs. IR press) rather than one
    adapter silently eating into another's allowance.
    """
    def __init__(self, usage_file: Path, monthly_budget: int):
        self.usage_file = usage_file
        self.monthly_budget = monthly_budget
        self.quota_exhausted_at: datetime.datetime | None = None
        self._quota_retry_after = datetime.timedelta(hours=6)

    @property
    def quota_exhausted(self) -> bool:
        """True if Exa itself rejected a recent call as out-of-credits (HTTP 402/429)."""
        if self.quota_exhausted_at is None:
            return False
        if datetime.datetime.utcnow() - self.quota_exhausted_at > self._quota_retry_after:
            self.quota_exhausted_at = None
            return False
        return True

    def mark_quota_exhausted(self):
        self.quota_exhausted_at = datetime.datetime.utcnow()

    @property
    def quota_retry_after(self) -> datetime.timedelta:
        return self._quota_retry_after

    def _current_month(self) -> str:
        return datetime.datetime.utcnow().strftime("%Y-%m")

    def _load_usage(self) -> dict:
        try:
            if self.usage_file.exists():
                return json.loads(self.usage_file.read_text())
        except Exception:
            pass
        return {"month": self._current_month(), "count": 0}

    def _save_usage(self, state: dict):
        try:
            self.usage_file.write_text(json.dumps(state))
        except Exception as e:
            logger.warning(f"Could not persist Exa usage counter ({self.usage_file.name}): {e}")

    def record_usage(self):
        state = self._load_usage()
        if state.get("month") != self._current_month():
            state = {"month": self._current_month(), "count": 0}
        state["count"] = state.get("count", 0) + 1
        self._save_usage(state)

    @property
    def budget_exhausted(self) -> bool:
        """Self-imposed monthly cap, checked BEFORE any request is sent."""
        state = self._load_usage()
        if state.get("month") != self._current_month():
            return False
        return state.get("count", 0) >= self.monthly_budget

    def get_usage_status(self) -> dict:
        state = self._load_usage()
        used = state.get("count", 0) if state.get("month") == self._current_month() else 0
        return {
            "month": self._current_month(),
            "used": used,
            "budget": self.monthly_budget,
            "remaining": max(0, self.monthly_budget - used),
            "budget_exhausted": used >= self.monthly_budget
        }
