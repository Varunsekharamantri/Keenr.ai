"""
Language-model calls with a fallback chain: Groq, then OpenRouter, then Gemini.

Groq's free tier allows 200,000 tokens a day, and a heavy refresh has spent
it more than once, leaving tiles on their plain computed sentence until the
next morning. When Groq cannot answer - daily budget spent, rate limited, an
error, or a draft our checks reject - the next provider gets a turn.

`answers()` yields one provider's reply at a time and only calls the next if
the caller asks for more, so a summary that fails validation (an invented
figure, say) can be retried elsewhere, while a good first answer costs one
call. Callers keep their own guardrails: every provider's text goes through
the same checks.

Free tiers are small (OpenRouter's free models allow about 50 requests a
day), so each fallback has a daily cap, and extraction - which runs on
hundreds of documents - may not use the last slice of it: that is kept for the
user-facing summaries, the same rule as the Groq reserve in quota.py.
Counts live in the database (app_state), shared with the GitHub Actions run.
"""
import datetime
import logging
import re
import time
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

import httpx

from ..config import settings

logger = logging.getLogger("market_signals.providers")
_TZ = ZoneInfo(getattr(settings, "SCHEDULE_TIMEZONE", "Asia/Kolkata"))
_TIMEOUT = 45


# ----------------------------------------------------------- daily counters
def _today() -> str:
    return datetime.datetime.now(_TZ).date().isoformat()


def _usage(name: str) -> dict:
    try:
        from ..db.state import get_state
        u = get_state(f"llm_usage:{name}") or {}
    except Exception:
        u = {}
    return u if u.get("date") == _today() else {"date": _today(), "requests": 0, "tokens": 0}


def _count(name: str, tokens: int = 0):
    try:
        from ..db.state import set_state
        u = _usage(name)
        u["requests"] += 1
        u["tokens"] += int(tokens or 0)
        set_state(f"llm_usage:{name}", u)
    except Exception as ex:
        logger.debug(f"usage for {name} not saved: {ex}")


def _mark_exhausted(name: str, why: str):
    try:
        from ..db.state import set_state
        u = _usage(name)
        u["exhausted"] = True
        u["why"] = why[:160]
        set_state(f"llm_usage:{name}", u)
    except Exception:
        pass
    logger.info(f"{name} unavailable for the rest of today: {why[:120]}")


def _fallback_open(name: str, cap: int, reserve: int, purpose: str) -> bool:
    u = _usage(name)
    if u.get("exhausted"):
        return False
    ceiling = cap if purpose == "summary" else max(0, cap - reserve)
    return u["requests"] < ceiling


# --------------------------------------------------------------- providers
def _groq(system, user, max_tokens, temperature, json_mode, purpose) -> Optional[dict]:
    from .quota import can_spend, mark_exhausted, record
    if not settings.GROQ_API_KEY or not can_spend(purpose, 2500 if purpose == "summary" else 1800):
        return None
    model = settings.GROQ_SUMMARY_MODEL if purpose == "summary" else settings.GROQ_MODEL
    # gpt-oss reasons before it writes; if that uses the whole budget the reply
    # is empty (finish_reason=length), so it gets one larger retry. A short
    # per-minute rate limit is waited out once - the alternative is a tile on
    # plain text, or a document extracted by rules alone.
    budget, grew, waited = max_tokens, False, False
    while True:
        body = {"model": model, "temperature": temperature, "max_tokens": budget,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if purpose == "summary":
            body["reasoning_effort"] = "low"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = httpx.post("https://api.groq.com/openai/v1/chat/completions", json=body, timeout=_TIMEOUT,
                       headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"})
        if r.status_code == 429:
            text = (r.text or "").lower()
            if "per day" in text or "tpd" in text:
                mark_exhausted(r.text[:200])
                return None
            wait = _retry_wait(r)
            if waited or not 0 < wait <= 15:
                return None
            waited = True
            time.sleep(wait + 0.5)
            continue
        if r.status_code != 200:
            logger.info(f"Groq {r.status_code}: {r.text[:140]}")
            return None
        data = r.json()
        record((data.get("usage") or {}).get("total_tokens", 0), purpose)
        choice = data["choices"][0]
        text = (choice["message"].get("content") or "").strip()
        if text:
            return {"text": text, "provider": "groq", "model": model}
        if choice.get("finish_reason") != "length" or grew:
            return None
        budget, grew = max(budget * 2, 2600), True


def _retry_wait(r) -> float:
    try:
        wait = float(r.headers.get("retry-after", "") or 0)
    except ValueError:
        wait = 0.0
    if not wait:
        m = re.search(r"try again in ([\d.]+)\s*s", r.text or "")
        wait = float(m.group(1)) if m else 0.0
    return wait


def _openrouter(system, user, max_tokens, temperature, json_mode, purpose) -> Optional[dict]:
    if not settings.OPENROUTER_API_KEY:
        return None
    if not _fallback_open("openrouter", settings.OPENROUTER_DAILY_MAX, settings.OPENROUTER_SUMMARY_RESERVE, purpose):
        return None
    for model in [m.strip() for m in settings.OPENROUTER_MODELS.split(",") if m.strip()]:
        body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                # Reasoning off. With it on, Nemotron wrote its thinking into
                # the reply itself ("We need to rewrite the true statements...")
                # despite exclude=True, and took 36s; off, it answered cleanly
                # in 2s on 394 tokens. The tasks are rewriting and tagging,
                # not problems that need thinking through.
                "reasoning": {"enabled": False}}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions", json=body, timeout=_TIMEOUT,
                           headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                                    "HTTP-Referer": "https://github.com/Varunsekharamantri/Keenr.ai",
                                    "X-Title": "Keenr.ai"})
        except Exception as ex:
            logger.info(f"OpenRouter {model} failed: {ex}")
            continue
        if r.status_code in (402, 429):
            _count("openrouter")
            # 402 = out of credit; a 429 naming the per-day free limit = done
            # for today. Any other 429 is a per-minute limit on this one model.
            if r.status_code == 402 or "per-day" in (r.text or "").lower():
                _mark_exhausted("openrouter", r.text or "")
                return None
            logger.info(f"OpenRouter {model} rate limited; trying the next model")
            continue
        if r.status_code != 200:
            logger.info(f"OpenRouter {model} {r.status_code}: {r.text[:140]}")
            continue                      # an unavailable model: try the next
        data = r.json()
        _count("openrouter", (data.get("usage") or {}).get("total_tokens", 0))
        try:
            text = (data["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError):
            text = ""
        if text:
            return {"text": text, "provider": "openrouter", "model": model}
    return None


_GEMINI_ENDPOINTS = {
    # The Gemini API first: the key supplied (an "AQ." key) works there, while
    # Vertex answers 403 until its API is enabled on the key's Google Cloud
    # project. Vertex stays as the second try for a Vertex-only key.
    "studio": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    "vertex": "https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent",
}
_gemini_working: Optional[str] = None


def _gemini(system, user, max_tokens, temperature, json_mode, purpose) -> Optional[dict]:
    global _gemini_working
    key = settings.GEMINI_API_KEY
    if not key:
        return None
    if not _fallback_open("gemini", settings.GEMINI_DAILY_MAX, settings.GEMINI_SUMMARY_RESERVE, purpose):
        return None
    gen = {"temperature": temperature, "maxOutputTokens": max_tokens,
           # Flash thinks by default; a tile summary or an extraction does not need it.
           "thinkingConfig": {"thinkingBudget": 0}}
    if json_mode:
        gen["responseMimeType"] = "application/json"
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
    endpoints = [_gemini_working] if _gemini_working else ["studio", "vertex"]
    # Several models, because Google retires them for new keys without notice
    # (2.5-flash and 2.0-flash both answer 404 for this key) and a busy one
    # answers 503 "high demand".
    for model in [m.strip() for m in settings.GEMINI_MODELS.split(",") if m.strip()]:
        for kind in endpoints:
            try:
                r = httpx.post(_GEMINI_ENDPOINTS[kind].format(model=model), json=body, timeout=_TIMEOUT,
                               headers={"x-goog-api-key": key})
            except Exception as ex:
                logger.info(f"Gemini {model} ({kind}) failed: {ex}")
                continue
            if r.status_code in (401, 403) and not _gemini_working:
                logger.info(f"Gemini {kind} endpoint refused the key ({r.status_code}); trying the other")
                continue
            if r.status_code == 429:
                _count("gemini")
                if "perday" in (r.text or "").lower().replace(" ", "").replace("_", ""):
                    _mark_exhausted("gemini", r.text or "429")
                    return None
                break                          # per-minute limit on this model
            if r.status_code != 200:
                logger.info(f"Gemini {model} ({kind}) {r.status_code}: {r.text[:140]}")
                break                          # retired or busy model: next model
            _gemini_working = kind
            data = r.json()
            _count("gemini", (data.get("usageMetadata") or {}).get("totalTokenCount", 0))
            try:
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            except (KeyError, IndexError, TypeError):
                text = ""
            if text:
                return {"text": text, "provider": "gemini", "model": model}
            break
    return None


_CHAIN = [("groq", _groq), ("openrouter", _openrouter), ("gemini", _gemini)]


def answers(system: str, user: str, *, purpose: str = "summary", max_tokens: int = 1200,
            temperature: float = 0.3, json_mode: bool = False) -> Iterator[dict]:
    """Replies from each provider in turn; the next is called only if you ask for it."""
    for name, call in _CHAIN:
        try:
            out = call(system, user, max_tokens, temperature, json_mode, purpose)
        except Exception as ex:
            logger.info(f"{name} call failed: {ex}")
            out = None
        if out:
            yield out


def complete(system: str, user: str, **kw) -> Optional[dict]:
    """The first provider's reply, or None if none could answer."""
    return next(answers(system, user, **kw), None)


def can_serve(purpose: str) -> bool:
    """Whether any provider could take a request of this kind right now."""
    from .quota import can_spend
    if settings.GROQ_API_KEY and can_spend(purpose, 2500 if purpose == "summary" else 1800):
        return True
    if settings.OPENROUTER_API_KEY and _fallback_open(
            "openrouter", settings.OPENROUTER_DAILY_MAX, settings.OPENROUTER_SUMMARY_RESERVE, purpose):
        return True
    if settings.GEMINI_API_KEY and _fallback_open(
            "gemini", settings.GEMINI_DAILY_MAX, settings.GEMINI_SUMMARY_RESERVE, purpose):
        return True
    return False


def status() -> dict:
    from .quota import status as groq_status
    return {"groq": groq_status(), "openrouter": _usage("openrouter"), "gemini": _usage("gemini"),
            "configured": {"groq": bool(settings.GROQ_API_KEY), "openrouter": bool(settings.OPENROUTER_API_KEY),
                           "gemini": bool(settings.GEMINI_API_KEY)}}
