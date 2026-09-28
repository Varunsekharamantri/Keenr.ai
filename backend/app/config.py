import os
from pathlib import Path
from dotenv import load_dotenv

# Base Paths
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent
DATA_DIR = BACKEND_DIR / "data"
RAW_STORE_DIR = DATA_DIR / "raw_store"
RAW_STORE_DIR.mkdir(parents=True, exist_ok=True)

# Load .env
load_dotenv(PROJECT_DIR / ".env")
load_dotenv(BACKEND_DIR / ".env")

def _database_url(url: str) -> str:
    """
    Name the Postgres driver explicitly. SQLAlchemy 2.1 changed the default for
    a bare postgresql:// URL from psycopg2 to psycopg (v3), so a fresh install
    - Render, the GitHub Actions runner - failed with "No module named
    'psycopg'" while an older local install kept working. psycopg2-binary is
    the driver in requirements.txt. Also accepts Heroku-style postgres://.
    """
    url = (url or "").strip()
    for bare in ("postgres://", "postgresql://"):
        if url.startswith(bare):
            return "postgresql+psycopg2://" + url[len(bare):]
    return url


class Settings:
    PROJECT_NAME: str = "Keenr.ai"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api"
    
    # SQLite Database URL
    DATABASE_URL: str = _database_url(os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'signals.db'}"))
    
    # SEC EDGAR Requirements
    # SEC requires User-Agent in format: <App Name> <Contact Email>
    SEC_USER_AGENT: str = os.getenv(
        "SEC_USER_AGENT",
        "MarketSignalsPlatform admin@marketsignals.io"
    )
    SEC_RATE_LIMIT_DELAY: float = float(os.getenv("SEC_RATE_LIMIT_DELAY", "0.15")) # seconds between requests
    
    # Paths
    DATA_DIR: Path = DATA_DIR
    TAXONOMY_PATH: Path = DATA_DIR / "taxonomy.yaml"
    COMPANIES_SEED_PATH: Path = DATA_DIR / "companies_seed.json"
    RAW_STORE_DIR: Path = RAW_STORE_DIR
    
    # Scheduler Settings (automated daily ingestion refresh)
    # Public mode: the deployed, shareable copy. Read-only - nothing a visitor
    # does can fetch, write, or spend an API budget: every POST/PUT/DELETE under
    # /api is refused, missing AI summaries are not written on demand, People to
    # Tap does not search in the background, and the in-process scheduler is
    # off. The GitHub Actions daily refresh does all of that instead, into the
    # same database. Off by default, so the local copy keeps working as before.
    PUBLIC_MODE: bool = os.getenv("PUBLIC_MODE", "false").lower() == "true"
    SCHEDULE_ENABLED: bool = (os.getenv("SCHEDULE_ENABLED", "true").lower() == "true"
                              and os.getenv("PUBLIC_MODE", "false").lower() != "true")
    SCHEDULE_INTERVAL_HOURS: float = float(os.getenv("SCHEDULE_INTERVAL_HOURS", "24"))
    SCHEDULE_LIMIT_PER_COMPANY: int = int(os.getenv("SCHEDULE_LIMIT_PER_COMPANY", "2"))
    SCHEDULE_RUN_ON_STARTUP: bool = os.getenv("SCHEDULE_RUN_ON_STARTUP", "false").lower() == "true"
    # Phase 1 scope is narrowed to BFSI (Americas + RoW). Set to empty/"all" to widen back out.
    _raw_sector_filter = os.getenv("SCHEDULE_SECTOR_FILTER", "BFSI")
    SCHEDULE_SECTOR_FILTER: str | None = None if not _raw_sector_filter or _raw_sector_filter.lower() == "all" else _raw_sector_filter

    # LLM Settings (Optional)
    OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    # Accept both casings since .env files in the wild use either
    GROQ_API_KEY: str | None = os.getenv("GROQ_API_KEY") or os.getenv("Groq_API_KEY")
    # Two settings so extraction (JSON from a filing) and narration (the tile
    # sentences) can diverge, but both default to gpt-oss-120b - which, after
    # benchmarking everything this key can reach, is the only model that does
    # either job reliably.
    #
    # What the alternatives measured, same prompts, same guardrails:
    #   gpt-oss-120b  993 tok extraction / 829 tok summary - always returned
    #                 usable output; 370-600 of those tokens are reasoning.
    #   gpt-oss-20b   1318 / 871 - strictly worse: a smaller model that thinks
    #                 *longer* (744 tokens) for no gain in quality.
    #   qwen3.8-27b   443 / 398 when it works, and it looked like a clear win
    #                 until two things showed up: it reasons too, without
    #                 reporting it, so the same prompt that cost 398 tokens once
    #                 burned 1,057 and returned an empty string the next time
    #                 (finish_reason=length); and it enforces a 1,000
    #                 output-tokens-per-minute ceiling, so a burst of tile
    #                 summaries 429s. Cheaper per call, but it silently drops
    #                 tiles back to their computed sentence, which is the exact
    #                 failure we were trying to avoid.
    # Everything else on this key is speech (whisper), Arabic (allam, orpheus),
    # or a safety classifier (llama-prompt-guard, gpt-oss-safeguard). No Llama
    # chat model and no embedding model is available, which is why the planned
    # RAG work will need embeddings from somewhere other than Groq.
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    GROQ_SUMMARY_MODEL: str = os.getenv("GROQ_SUMMARY_MODEL", "openai/gpt-oss-120b")

    # Fallbacks when Groq cannot answer (its 200k tokens/day spent, rate
    # limited, down, or a draft the guardrails reject) - see app/ai/providers.py.
    # Both are optional; with neither key set, Groq is the only provider.
    # Free tiers are small, so each has a daily request cap, and the last
    # `*_SUMMARY_RESERVE` requests are kept for the dashboard's summaries
    # rather than spent on bulk extraction.
    OPENROUTER_API_KEY: str | None = os.getenv("OPENROUTER_API_KEY") or os.getenv("Open_router_API")
    # Tried in order; an unavailable model falls through to the next.
    # Nemotron measured best: a clean, valid tile summary in 2s. The others
    # were rate-limited upstream when tested and are kept as backups.
    OPENROUTER_MODELS: str = os.getenv(
        "OPENROUTER_MODELS",
        "nvidia/nemotron-3-super-120b-a12b:free,google/gemma-4-31b-it:free,qwen/qwen3.8-27b:free")
    OPENROUTER_DAILY_MAX: int = int(os.getenv("OPENROUTER_DAILY_MAX", "45"))
    OPENROUTER_SUMMARY_RESERVE: int = int(os.getenv("OPENROUTER_SUMMARY_RESERVE", "15"))
    GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("Gemini_API_Key")
    # Tried in order. 2.5-flash and 2.0-flash are closed to new keys (404).
    GEMINI_MODELS: str = os.getenv("GEMINI_MODELS", "gemini-3.8-flash,gemini-flash-latest,gemini-3.5-flash-lite")
    GEMINI_DAILY_MAX: int = int(os.getenv("GEMINI_DAILY_MAX", "200"))
    GEMINI_SUMMARY_RESERVE: int = int(os.getenv("GEMINI_SUMMARY_RESERVE", "40"))

    # Exa (neural search) — richer full-text news alternative to the free RSS adapter
    EXA_API_KEY: str | None = os.getenv("EXA_API_KEY") or os.getenv("Exa_API_key")
    EXA_MAX_CHARACTERS: int = int(os.getenv("EXA_MAX_CHARACTERS", "6000"))
    # Hard self-imposed cap on Exa requests/month, enforced BEFORE any call is
    # made. Exa's recurring free credit is $10/mo at $7/1000 requests (~1428
    # requests); default here stays well under that so a full month never
    # gets close to spending real money, even with content-fetch bundled in.
    # Rebalanced 2026-09-08: the three Exa consumers share one ~1428 req/mo
    # free-tier pool, so the TOTAL must stay under it — 700+350+350 = 1400,
    # the same ceiling as the old 1000+200+200. News was capped at 1000 but
    # only ever used ~74/mo, while IR and career were too low to cover a
    # full 199-company pass (199 calls each). Redistributing costs nothing
    # and lets every company get every source.
    EXA_MONTHLY_BUDGET: int = int(os.getenv("EXA_MONTHLY_BUDGET", "700"))
    # Spreads the monthly budget evenly across days instead of burning it all
    # in the first few days of the month. Companies rotate fairly via
    # Company.last_exa_ingest_at (least-recently-touched go first each day).
    EXA_DAILY_BUDGET: int = int(os.getenv("EXA_DAILY_BUDGET", str(max(1, EXA_MONTHLY_BUDGET // 30))))

    # IR press-release adapter also uses Exa, but with its OWN separate budget
    # (own usage-counter file) rather than sharing exa_news's pool — keeps
    # each source independently rationed while the combined worst-case spend
    # (1000 + 200 = 1200 req/mo) still stays under Exa's ~1428 free-tier cap.
    EXA_IR_MONTHLY_BUDGET: int = int(os.getenv("EXA_IR_MONTHLY_BUDGET", "350"))
    EXA_IR_DAILY_BUDGET: int = int(os.getenv("EXA_IR_DAILY_BUDGET", str(max(1, EXA_IR_MONTHLY_BUDGET // 30))))

    # Career-page adapter — third independent Exa consumer, own budget pool.
    # Combined worst case: 1000 (news) + 200 (IR) + 200 (career) = 1400 req/mo,
    # still just under Exa's ~1428 free-tier ceiling.
    EXA_CAREER_MONTHLY_BUDGET: int = int(os.getenv("EXA_CAREER_MONTHLY_BUDGET", "350"))
    EXA_CAREER_DAILY_BUDGET: int = int(os.getenv("EXA_CAREER_DAILY_BUDGET", str(max(1, EXA_CAREER_MONTHLY_BUDGET // 30))))

    # People to Tap - fourth Exa consumer, own pool. One search per company
    # (category "linkedin profile"), so the first pull over every company with
    # a live opportunity costs about one call each, and after that a company is
    # only searched again when a leadership-change story arrives or its data
    # passes LEADERS_TTL_DAYS.
    EXA_PEOPLE_MONTHLY_BUDGET: int = int(os.getenv("EXA_PEOPLE_MONTHLY_BUDGET", "200"))
    LEADERS_TTL_DAYS: int = int(os.getenv("LEADERS_TTL_DAYS", "120"))
    LEADERS_MIN_SCORE: int = int(os.getenv("LEADERS_MIN_SCORE", "3"))        # points: "Medium" and up
    LEADERS_LOOKBACK_DAYS: int = int(os.getenv("LEADERS_LOOKBACK_DAYS", "90"))
    LEADERS_DAILY_MAX_CALLS: int = int(os.getenv("LEADERS_DAILY_MAX_CALLS", "15"))

    # PatentsView (via USPTO Open Data Portal, api.uspto.gov) — free API key,
    # request at https://data.uspto.gov/apis/getting-started. Optional: the
    # patents adapter is a no-op until this is set.
    PATENTSVIEW_API_KEY: str | None = os.getenv("PATENTSVIEW_API_KEY")
    PATENTSVIEW_RATE_LIMIT_DELAY: float = float(os.getenv("PATENTSVIEW_RATE_LIMIT_DELAY", "0.2"))

    # Signal engine (Phase 3) — events older than the lookback window don't
    # contribute to a company's intent score; signals with no in-window
    # events are marked stale rather than deleted.
    SIGNAL_LOOKBACK_DAYS: int = int(os.getenv("SIGNAL_LOOKBACK_DAYS", "180"))
    # Default date window the dashboard opens on. Distinct from
    # SIGNAL_LOOKBACK_DAYS, which stays the canonical window for the stored
    # signals that alerting evaluates against.
    SIGNAL_DEFAULT_WINDOW_DAYS: int = int(os.getenv("SIGNAL_DEFAULT_WINDOW_DAYS", "30"))
    SIGNAL_DEFAULT_ALERT_THRESHOLD: int = int(os.getenv("SIGNAL_DEFAULT_ALERT_THRESHOLD", "6"))  # points: High
    SIGNAL_RECOMPUTE_ON_STARTUP: bool = os.getenv("SIGNAL_RECOMPUTE_ON_STARTUP", "true").lower() == "true"
    # Optional Slack incoming-webhook for threshold alerts; in-app alerts
    # always fire regardless.
    SLACK_WEBHOOK_URL: str | None = os.getenv("SLACK_WEBHOOK_URL")

    # Phase 4 — productization
    # Generic outbound webhook for pushing ranked leads to a CRM (works with
    # HubSpot workflows, Zapier, Make). Export endpoint reports "not
    # configured" rather than failing when this is unset.
    CRM_WEBHOOK_URL: str | None = os.getenv("CRM_WEBHOOK_URL")
    # A source with no successfully ingested document in this many hours is
    # flagged stale on the source-health panel.
    SOURCE_FRESHNESS_SLA_HOURS: int = int(os.getenv("SOURCE_FRESHNESS_SLA_HOURS", "48"))

    # Phase 5 — procurement tenders (real RFPs).
    # TED (EU Tenders Electronic Daily) needs no credentials at all; CPV
    # 72000000 is "IT services: consulting, software development, Internet".
    TED_ENABLED: bool = os.getenv("TED_ENABLED", "true").lower() == "true"
    TED_CPV_CODES: str = os.getenv("TED_CPV_CODES", "72000000")
    TED_LOOKBACK_DAYS: int = int(os.getenv("TED_LOOKBACK_DAYS", "30"))
    TED_MAX_NOTICES_PER_RUN: int = int(os.getenv("TED_MAX_NOTICES_PER_RUN", "100"))
    # SAM.gov (US federal) requires a free but manually-requested key; the
    # adapter is a no-op until this is set.
    SAMGOV_API_KEY: str | None = os.getenv("SAMGOV_API_KEY")

    # Phase 6 — rebuild & deployment
    # Date floor for Exa-sourced news during a clean rebuild, so the dashboard
    # can honestly claim "complete coverage from this date". SEC filings are
    # deliberately NOT bounded — a Q2 10-Q filed in July is still worth having.
    REBUILD_START_DATE: str = os.getenv("REBUILD_START_DATE", "2026-08-01")
    # Cloud runners have ephemeral disks; skip archiving raw text there.
    RAW_STORE_ENABLED: bool = os.getenv("RAW_STORE_ENABLED", "true").lower() == "true"

settings = Settings()
