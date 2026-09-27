"""
One- or two-sentence narration for dashboard tiles, written by Groq (or, when
Groq cannot answer, OpenRouter or Gemini - see providers.py).

The division of labour is the point: **we compute every number and own every
meaning; the model only writes the sentence.** It receives finished, already-true
statements and rewrites them as prose. It is never asked to count, rank or infer.

That design came from a failure. Passing raw field names let the model guess at
meaning, and it wrote "39% of market share" for what was a share of *companies*,
and "many of which have 26 vendor partnerships" for 26 companies that were in one.
The numbers were right and the sentence was false.

Guardrails, because a fluent wrong sentence is worse than a plain right one:

  1. Number check - every numeric token in the reply must appear in the facts we
     supplied. A model that writes "up 40%" when we never said 40 is rejected.
  2. Format check - one or two plain sentences, no markdown, no lists, <= 42 words.
  3. Fallback - on rejection, rate limit, timeout or missing key, the caller's
     own computed sentence is returned, marked source="computed" so the UI can
     say which is which.

Replies are cached on disk keyed by the facts themselves, so a tile costs one
call per change of data rather than one per page load. Groq's free tier is capped
by tokens per minute (8,000), which a live dashboard would otherwise exhaust.
"""
import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from ..config import settings

logger = logging.getLogger("market_signals.summarizer")

CACHE_PATH = settings.DATA_DIR / "ai_summaries.json"
_LOCK = threading.Lock()
_CACHE: Optional[dict] = None
_MAX_WORDS = 90

SYSTEM = (
    "You rewrite already-true statements into a short paragraph for a dashboard "
    "tile, read by salespeople who sell IT services to banks and insurers. "
    "What to write: lead with the headline finding, then make it concrete. When "
    "the statements name companies, technologies or examples of what is being "
    "built, work at least one of them in, so the reader learns what the technology "
    "is actually being used for and where. Close with what it means for someone "
    "selling into it, but only if the statements support that. "
    "Hard rules: keep each statement's meaning exactly. Do not reinterpret what a "
    "number counts. Do not relate two numbers that were not related. Never add a "
    "fact, number, company, product or conclusion that is not in the statements. "
    "A share of companies is not market share. "
    "Style: 45 to 70 words in complete, grammatical sentences - never drop words "
    "to hit a limit. Plain prose, no markdown, no bullets, no headings, no preamble."
)

_NUM = re.compile(r"\d+(?:[.,]\d+)*")


# Summaries live in the database (ai_summaries), so the ones the morning
# GitHub Actions run writes reach the app. Held in memory once read; a key not
# in memory is looked up in the database, because another process (that run)
# may have written it since. Keys are hashes of the facts, so a remembered
# summary can never be stale - a changed fact is a different key.
def _load_cache() -> dict:
    global _CACHE
    if _CACHE is None:
        _CACHE = {}
        try:
            from ..db.database import SessionLocal
            from ..models.schema import AISummary
            db = SessionLocal()
            try:
                for row in db.query(AISummary).all():
                    _CACHE[row.key] = {"text": row.text, "source": row.source, "kind": row.kind}
            finally:
                db.close()
        except Exception as ex:
            logger.debug(f"summary cache not loaded: {ex}")
    return _CACHE


def _lookup(key: str) -> Optional[dict]:
    cache = _load_cache()
    if key in cache:
        return cache[key]
    try:
        from ..db.database import SessionLocal
        from ..models.schema import AISummary
        db = SessionLocal()
        try:
            row = db.get(AISummary, key)
            if row is not None:
                cache[key] = {"text": row.text, "source": row.source, "kind": row.kind}
                return cache[key]
        finally:
            db.close()
    except Exception:
        pass
    return None


def _store(key: str, entry: dict):
    try:
        from ..db.database import SessionLocal
        from ..models.schema import AISummary
        db = SessionLocal()
        try:
            db.merge(AISummary(key=key, kind=entry.get("kind"), text=entry["text"], source=entry.get("source", "ai")))
            db.commit()
        finally:
            db.close()
    except Exception as ex:
        logger.debug(f"summary not stored: {ex}")


def _key(kind: str, facts: dict) -> str:
    blob = json.dumps({"k": kind, "f": facts, "m": settings.GROQ_SUMMARY_MODEL},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _allowed_numbers(facts: dict) -> set:
    """Every number that appears anywhere in the facts, as written."""
    blob = json.dumps(facts, default=str)
    out = set()
    for tok in _NUM.findall(blob):
        out.add(tok)
        out.add(tok.replace(",", ""))
        if tok.endswith(".0"):
            out.add(tok[:-2])
    return out



# Words a summary may use even though they are not in the facts: generic terms,
# acronyms of the domain, and calendar words. Anything else that looks like a
# proper noun has to have come from the statements.
_SAFE_WORDS = {
    "ai", "genai", "it", "llm", "llms", "mlops", "ml", "rfp", "rfps", "bfsi", "cio",
    "cto", "ciso", "cdo", "coo", "cfo", "ceo", "api", "apis", "saas", "erp", "crm",
    "esg", "us", "uk", "eu", "americas", "rest", "world", "global", "the", "a", "an",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "q1", "q2", "q3", "q4",
}


def _invented_entities(text: str, facts: dict) -> list:
    """Capitalised terms in the reply that never appeared in the facts."""
    # Normalise curly apostrophes first. The model writes "Visa’s"; without this
    # the possessive was not stripped, the name never matched the facts, and a
    # perfectly good summary was rejected for naming a company it had been given.
    text = text.replace("’", "'")
    blob = json.dumps(facts, default=str).lower().replace("’", "'")
    # Same problem, different punctuation: a title we wrote as "CIO / CTO / CDO
    # / CISO" came back as "CIO/CTO/CDO/CISO" - correct, just tighter spacing -
    # and was rejected as an invented entity because the spaced and unspaced
    # forms don't match as substrings. Collapse " / " to "/" on both sides so
    # either spacing compares equal.
    text = re.sub(r"\s*/\s*", "/", text)
    blob = re.sub(r"\s*/\s*", "/", blob)
    bad = []
    # Skip the first word of each sentence: it is capitalised by grammar, not because
    # it names something.
    for sentence in re.split(r"(?<=[.;!?])\s+", text):
        words = sentence.split()
        for w in words[1:]:
            token = w.strip(".,;:!?()[]'\"").removesuffix("'s").removesuffix("'")
            if len(token) < 2 or not token[0].isupper():
                continue
            low = token.lower()
            if low in _SAFE_WORDS or low in blob:
                continue
            # "Cloud-based" is an adjective and "CIO/CTO" is a slash-joined
            # title, neither a name. Accept a hyphenated or slash-joined token
            # when any part of it is known; it cannot smuggle in a new company,
            # because a real name would fail on every part.
            parts = [x for x in re.split(r"[-/]", low) if x]
            if len(parts) > 1 and any(x in blob or x in _SAFE_WORDS for x in parts):
                continue
            bad.append(token)
    return bad


def _validate(text: str, facts: dict) -> Optional[str]:
    """Return a clean sentence, or None if it breaks a rule."""
    if not text:
        return None
    t = " ".join(text.strip().split())
    t = re.sub(r"^[\"'`*#\-\s]+", "", t).replace("**", "").replace("`", "")
    # Typography the model emits that reads wrong in a dense tile: non-breaking
    # hyphen and space, en/em dashes, and "58 %" with a gap.
    for bad, good in (("‑", "-"), (" ", " "), ("–", "-"), ("—", "-")):
        t = t.replace(bad, good)
    t = re.sub(r"(\d)\s+%", r"\1%", t)

    if not t or len(t.split()) > _MAX_WORDS:
        return None
    if any(m in t for m in ("- ", "* ", "\n", "|")):
        return None

    allowed = _allowed_numbers(facts)
    for tok in _NUM.findall(t):
        if tok in allowed or tok.replace(",", "") in allowed:
            continue
        logger.info(f"summary rejected, number not in facts: {tok!r} in {t[:80]!r}")
        return None

    invented = _invented_entities(t, facts)
    if invented:
        logger.info(f"summary rejected, names not in facts: {invented} in {t[:80]!r}")
        return None
    return t


def _build_prompt(kind: str, facts: dict) -> str:
    statements = facts.get("statements") if isinstance(facts, dict) else None
    if statements:
        body = "\n".join("- " + str(x) for x in statements)
    else:
        body = json.dumps(facts, indent=1, default=str)
    extras = []
    if isinstance(facts, dict):
        extras = [f"{k}: {v}" for k, v in facts.items()
                  if k != "statements" and not isinstance(v, (dict, list))]
    context = ("\nContext: " + "; ".join(extras)) if extras else ""
    return (f"Tile: {kind}\n\nTrue statements to rewrite:\n{body}{context}\n\n"
            "Rewrite these as one or two sentences for this tile, keeping each "
            "meaning exactly. Use only the numbers above.")


def summarize(kind: str, facts: dict, fallback: str, force: bool = False) -> dict:
    """
    Narrate `facts`. Always returns {"text", "source"}; source is "ai" when the
    model wrote it, "computed" when the caller's own sentence was used.
    """
    fallback = " ".join((fallback or "").split())
    from . import providers
    if not providers.can_serve("summary"):
        return {"text": fallback, "source": "computed"}

    key = _key(kind, facts)
    with _LOCK:
        hit = _lookup(key)
    if hit and not force:
        return {"text": hit["text"], "source": hit.get("source", "ai")}

    # Groq first, then OpenRouter, then Gemini (app/ai/providers.py). A reply
    # that fails the guardrails - an invented number, a list, too long - is
    # thrown away and the next provider is asked, so one model's bad draft does
    # not cost the tile its summary. The next call happens only if needed.
    #
    # 1,200 tokens of room: gpt-oss reasons before it writes (500-900 tokens
    # on a simple tile, past 1,200 on the three-account comparison), and the
    # provider retries once larger if the reasoning used it all.
    clean, used = None, None
    for reply in providers.answers(SYSTEM, _build_prompt(kind, facts), purpose="summary",
                                   max_tokens=1200, temperature=0.3):
        clean = _validate(reply["text"], facts)
        if clean:
            used = reply
            break
        logger.info(f"{reply['provider']} draft for {kind} failed the checks; trying the next provider")
    if not clean:
        return {"text": fallback, "source": "computed"}
    if used["provider"] != "groq":
        logger.info(f"summary for {kind} written by {used['provider']} ({used['model']})")

    with _LOCK:
        cache = _load_cache()
        cache[key] = {"text": clean, "source": "ai", "kind": kind}
        _store(key, cache[key])
        if len(cache) > 400:                       # keep the file small
            for k in list(cache)[:100]:
                cache.pop(k, None)
    return {"text": clean, "source": "ai"}


def cached_only(kind: str, facts: dict) -> Optional[dict]:
    """The stored summary for these exact facts, without calling the API."""
    with _LOCK:
        hit = _lookup(_key(kind, facts))
    return {"text": hit["text"], "source": hit.get("source", "ai")} if hit else None


# ---- Writing summaries off the request path --------------------------------
# A page request used to call the model for any tile without a cached summary
# and wait for it. When a call failed - a rejected draft, a rate limit, a
# timeout - nothing was cached, so every click on a region button paid the same
# 2-7 seconds again. Now a request never waits: it gets the cached summary or
# the computed sentence immediately, and the missing ones are written by one
# background worker, to be served from the cache on a later load. A summary
# that failed is not retried for a while, so a stubborn one cannot keep the
# worker busy. The morning warm-up (budget=None) still writes synchronously.
_BG_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="summaries")
_BG_LOCK = threading.Lock()
_BG_PENDING: set = set()
_BG_FAILED: dict = {}                  # cache key -> when it last failed
_BG_RETRY_AFTER = 6 * 3600


def _write_in_background(job):
    kind, facts, fallback = job
    key = _key(kind, facts)
    try:
        result = summarize(kind, facts, fallback)
        if result.get("source") != "ai":
            with _BG_LOCK:
                _BG_FAILED[key] = time.time()
    except Exception as ex:
        logger.info(f"background summary failed for {kind}: {ex}")
        with _BG_LOCK:
            _BG_FAILED[key] = time.time()
    finally:
        with _BG_LOCK:
            _BG_PENDING.discard(key)


def queue_summaries(jobs: list) -> int:
    """Hand summaries to the background worker; returns how many were queued."""
    queued, now = 0, time.time()
    with _BG_LOCK:
        for job in jobs:
            key = _key(job[0], job[1])
            if key in _BG_PENDING or now - _BG_FAILED.get(key, 0) < _BG_RETRY_AFTER:
                continue
            _BG_PENDING.add(key)
            _BG_POOL.submit(_write_in_background, job)
            queued += 1
    return queued


def summarize_many(jobs: list, budget: Optional[int] = 3) -> list:
    """
    Narrate several tiles at once. Each job is (kind, facts, fallback).

    With a budget (every page request): never blocks. Cached summaries are
    returned; the rest get their computed sentence now and are queued for the
    background worker. With budget=None (the morning warm-up): writes them all
    synchronously, paced to stay under the per-minute token cap.
    """
    if not jobs:
        return []

    # Anything already written for these exact facts costs nothing.
    results, to_write = [None] * len(jobs), []
    for i, (kind, facts, fallback) in enumerate(jobs):
        hit = cached_only(kind, facts)
        if hit:
            results[i] = hit
        else:
            to_write.append(i)

    if budget is not None:
        queue_summaries([jobs[i] for i in to_write[:budget]])
    else:
        # Seven tiles generated at once needed ~12,000 tokens against an
        # 8,000/minute cap, so the warm-up writes two at a time with a pause.
        with ThreadPoolExecutor(max_workers=2) as pool:
            for i in range(0, len(to_write), 2):
                chunk = to_write[i:i + 2]
                futures = {idx: pool.submit(summarize, *jobs[idx]) for idx in chunk}
                for idx, f in futures.items():
                    results[idx] = f.result()
                if i + 2 < len(to_write):
                    time.sleep(12.0)

    for i in to_write:
        if results[i] is None:
            results[i] = {"text": " ".join((jobs[i][2] or "").split()), "source": "computed"}
    return results
