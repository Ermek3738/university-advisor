"""
scraper.py
──────────
Scrapes each university website using httpx + the Anthropic API directly.
No headless browser required — works on Render free tier.

Pipeline per university:
  1. httpx GET the website (follow redirects, 30s timeout)
  2. Strip scripts/styles/tags → plain text, truncate to ~20k chars
  3. Send the text to Claude with a strict JSON-only extraction prompt
  4. Parse the JSON and update the DB row

Usage:
  python scraper.py              # scrape all pending universities
  python scraper.py --all        # re-scrape everything (refresh)
  python scraper.py --id 42      # scrape specific university IDs
  python scraper.py --stale      # re-scrape records past their re_scrape_after date
"""

import os
import re
import sys
import json
import asyncio
import argparse
from datetime import datetime, timedelta
from typing import Optional

import httpx
from anthropic import AsyncAnthropic

# Make `models` importable whether this file is run from scraper/ or imported
# from backend/main.py.
_BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from models import University, SessionLocal  # noqa: E402

# ── Configuration ──────────────────────────────────────────────────────────────

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

MAX_PAGE_CHARS = 20_000     # plain-text characters sent to Claude per page
FETCH_TIMEOUT = 30.0        # seconds
DEFAULT_CONCURRENCY = 5
PER_TASK_DELAY = 1.0        # polite delay per worker

USER_AGENT = (
    "Mozilla/5.0 (compatible; AI-Sana-Scraper/1.0; +https://ai-sana.example)"
)

# ── Currency normalization ─────────────────────────────────────────────────────

EXCHANGE_RATES = {
    "USD": 1.00, "GBP": 1.27, "EUR": 1.08, "CAD": 0.74,
    "AUD": 0.65, "NZD": 0.60, "SGD": 0.74, "JPY": 0.0067,
    "CHF": 1.13, "SEK": 0.095, "NOK": 0.093, "DKK": 0.145,
}

def to_usd(amount, currency: str) -> Optional[float]:
    if amount is None:
        return None
    rate = EXCHANGE_RATES.get((currency or "USD").upper().strip(), 1.0)
    try:
        return round(float(amount) * rate, 2)
    except (TypeError, ValueError):
        return None

# ── HTML → text ────────────────────────────────────────────────────────────────

_BLOCK_TAGS_RE = re.compile(r"<(script|style|noscript)[^>]*>[\s\S]*?</\1>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

def html_to_text(html: str, max_chars: int = MAX_PAGE_CHARS) -> str:
    text = _BLOCK_TAGS_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:max_chars]

# ── Extraction prompt ──────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """You extract international-student admissions information from a university website.

Read the page text I provide and return a SINGLE JSON object — no commentary, no markdown fences — with EXACTLY these keys:

{
  "tuition_min": number | null,
  "tuition_max": number | null,
  "tuition_currency": string | null,
  "ielts_min": number | null,
  "toefl_min": number | null,
  "gpa_min": number | null,
  "programs": string | null,
  "intakes": string | null,
  "application_deadline": string | null,
  "scholarship_available": string | null,
  "notes": string | null
}

Rules:
- If a value is not clearly stated on the page, use null. Do NOT guess.
- Keep tuition in the original currency. Put the ISO currency code in tuition_currency (USD, GBP, EUR, ...).
- Convert GPA to a 4.0 scale if stated otherwise.
- scholarship_available: one of "Yes", "No", "Partial", or null.
- programs: short comma-separated list of major programs/majors offered.
- notes: 1-3 sentences of anything else useful for international students.
"""

_JSON_RE = re.compile(r"\{[\s\S]*\}")

def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        # strip ```json ... ``` fence
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    m = _JSON_RE.search(raw)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}

# ── Per-university scrape ──────────────────────────────────────────────────────

async def fetch_page(http: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        resp = await http.get(url, timeout=FETCH_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"  ❌ fetch failed {url}: {e}")
        return None

async def extract_with_claude(api: AsyncAnthropic, page_text: str, uni_name: str) -> dict:
    try:
        resp = await api.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=EXTRACTION_PROMPT,
            messages=[{
                "role": "user",
                "content": f"University: {uni_name}\n\nPage text:\n{page_text}",
            }],
        )
        raw = resp.content[0].text if resp.content else ""
        return _parse_json(raw)
    except Exception as e:
        print(f"  ❌ extract failed {uni_name}: {e}")
        return {}

async def scrape_university(http: httpx.AsyncClient, api: AsyncAnthropic, uni: University) -> dict:
    if not uni.website:
        return {}
    html = await fetch_page(http, uni.website)
    if not html:
        return {}
    text = html_to_text(html)
    if not text:
        return {}
    return await extract_with_claude(api, text, uni.name or "")

# ── DB write ───────────────────────────────────────────────────────────────────

def update_university(db, uni: University, data: dict):
    fields = [
        "tuition_min", "tuition_max", "tuition_currency",
        "ielts_min", "toefl_min", "gpa_min",
        "programs", "intakes", "application_deadline",
        "scholarship_available", "notes",
    ]
    for field in fields:
        if data.get(field) is not None:
            setattr(uni, field, data[field])

    uni.tuition_usd = to_usd(
        data.get("tuition_min"),
        data.get("tuition_currency", "USD"),
    )

    uni.last_scraped = datetime.utcnow()
    uni.re_scrape_after = datetime.utcnow() + timedelta(days=90)
    uni.scrape_status = "success" if data else "failed"
    db.commit()

# ── Runner ─────────────────────────────────────────────────────────────────────

async def run_scraper(
    target_ids=None,
    refresh_all: bool = False,
    stale_only: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
):
    """Scrape universities and write results into the DB.

    target_ids:    list of IDs to scrape (overrides other selectors)
    refresh_all:   re-scrape every university in the DB
    stale_only:    re-scrape rows whose re_scrape_after has passed
    default:       scrape rows with scrape_status == "pending"
    """
    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY is not set — aborting scrape.")
        return

    db = SessionLocal()
    try:
        if target_ids:
            unis = db.query(University).filter(University.id.in_(target_ids)).all()
        elif refresh_all:
            unis = db.query(University).all()
        elif stale_only:
            unis = db.query(University).filter(
                University.re_scrape_after <= datetime.utcnow()
            ).all()
        else:
            unis = db.query(University).filter(
                University.scrape_status == "pending"
            ).all()

        print(f"\n🔍 Scraping {len(unis)} universities (concurrency={concurrency})...\n")

        sem = asyncio.Semaphore(concurrency)
        api = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT,
        ) as http:

            async def worker(uni: University):
                async with sem:
                    print(f"  ▶ {uni.name} — {uni.website}")
                    data = await scrape_university(http, api, uni)
                    update_university(db, uni, data)
                    if data:
                        filled = len([v for v in data.values() if v is not None])
                        usd = f"${uni.tuition_usd:,.0f}" if uni.tuition_usd else "—"
                        currency = data.get("tuition_currency", "USD")
                        print(f"  ✅ {uni.name}: {filled} fields | tuition_usd={usd} ({currency})")
                    else:
                        print(f"  ❌ {uni.name}: no data extracted")
                    await asyncio.sleep(PER_TASK_DELAY)

            await asyncio.gather(*(worker(u) for u in unis))

    finally:
        db.close()

    print("\n✅ Scraping complete!")

# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all",   action="store_true", help="Re-scrape all universities")
    parser.add_argument("--stale", action="store_true", help="Re-scrape rows past their re_scrape_after date")
    parser.add_argument("--id",    type=int, nargs="+",   help="Scrape specific university IDs")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    args = parser.parse_args()

    asyncio.run(run_scraper(
        target_ids=args.id,
        refresh_all=args.all,
        stale_only=args.stale,
        concurrency=args.concurrency,
    ))
