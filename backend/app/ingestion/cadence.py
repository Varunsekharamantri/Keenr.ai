"""
Which sources are worth polling on a given day.

News moves daily; filings do not. Polling SEC and earnings decks for 199
companies every morning mostly re-reads pages that cannot have changed, and the
Exa-backed sources (IR releases, job postings) draw on a monthly budget. So each
source runs on the rhythm its releases actually follow.

  News (Google News RSS, Exa news) ... every day
  Tenders (EU TED) ................... every day (free, fast)
  Earnings decks + SEC filings ....... daily in results season, weekly otherwise
  IR press releases .................. daily in results season, twice weekly otherwise
  Job postings ....................... weekly

Results season: quarterly results land roughly 2-8 weeks after a quarter closes
(10-Qs are due within 40-45 days; banks report earliest, insurers later). Annual
reports (10-K / 20-F) for December year-ends run through March. So:

  Jan 10 - Mar 31   Q4 results + annual reports
  Apr 10 - May 25   Q1 results
  Jul 10 - Aug 25   Q2 results
  Oct 10 - Nov 25   Q3 results

Outside those windows filings still happen (8-Ks for deals, leadership, off-cycle
fiscal years such as Japanese and Indian banks closing in March), which is why the
slow cadence is weekly rather than never.
"""
import datetime
from typing import Dict, Set, Tuple

DAILY = {"news_rss", "exa_news"}

# (start_month, start_day, end_month, end_day), inclusive
RESULTS_WINDOWS = [
    (1, 10, 3, 31),
    (4, 10, 5, 25),
    (7, 10, 8, 25),
    (10, 10, 11, 25),
]

WEEKLY_DAY = 0            # Monday
IR_OFF_SEASON_DAYS = {0, 3}  # Monday, Thursday


def in_results_season(day: datetime.date) -> bool:
    for sm, sd, em, ed in RESULTS_WINDOWS:
        if datetime.date(day.year, sm, sd) <= day <= datetime.date(day.year, em, ed):
            return True
    return False


def plan_for(day: datetime.date) -> Tuple[Set[str], Dict[str, str]]:
    """Sources to poll on `day`, and a one-line reason for each decision."""
    season = in_results_season(day)
    weekday = day.weekday()
    sources = set(DAILY)
    why = {s: "daily" for s in DAILY}

    if season or weekday == WEEKLY_DAY:
        sources |= {"sec_edgar", "earnings_deck"}
        reason = "results season" if season else "weekly check (Monday)"
        why["sec_edgar"] = why["earnings_deck"] = reason
    else:
        why["sec_edgar"] = why["earnings_deck"] = "skipped: no filings due, next check Monday"

    if season or weekday in IR_OFF_SEASON_DAYS:
        sources.add("ir_press")
        why["ir_press"] = "results season" if season else "off-season check (Mon/Thu)"
    else:
        why["ir_press"] = "skipped: off-season, next check Mon/Thu"

    if weekday == WEEKLY_DAY:
        sources.add("career_pages")
        why["career_pages"] = "weekly check (Monday)"
    else:
        why["career_pages"] = "skipped: job postings checked weekly"

    return sources, why


def describe(day: datetime.date) -> str:
    sources, why = plan_for(day)
    season = "results season" if in_results_season(day) else "off-season"
    lines = [f"{day.isoformat()} ({day.strftime('%A')}, {season}):"]
    for name in ("news_rss", "exa_news", "sec_edgar", "earnings_deck", "ir_press", "career_pages"):
        mark = "RUN " if name in sources else "skip"
        lines.append(f"  {mark} {name:<14} {why[name]}")
    return "\n".join(lines)
