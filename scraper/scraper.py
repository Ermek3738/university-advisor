"""
scraper.py
──────────
Scrapes each university website and updates the database with:
  tuition, IELTS/TOEFL requirements, GPA, deadlines, programs

Uses crawl4ai for intelligent content extraction.

Install:
  pip install crawl4ai
  crawl4ai-setup   ← run once after install

Usage:
  python scraper.py              # scrape all pending universities
  python scraper.py --all        # re-scrape everything (refresh)
  python scraper.py --id 42      # scrape a specific university by ID
  python scraper.py --stale      # re-scrape records past their re_scrape_after date
"""

import asyncio
import json
import argparse
import os
from datetime import datetime, timedelta
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from crawl4ai.extraction_strategy import LLMExtractionStrategy
from models import University, SessionLocal

# ── Model ─────────────────────────────────────────────────────────────────────

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# ── Currency normalization ─────────────────────────────────────────────────────

EXCHANGE_RATES = {
    "USD": 1.00,
    "GBP": 1.27,
    "EUR": 1.08,
    "CAD": 0.74,
    "AUD": 0.65,
    "NZD": 0.60,
    "SGD": 0.74,
    "JPY": 0.0067,
    "CHF": 1.13,
    "SEK": 0.095,
    "NOK": 0.093,
    "DKK": 0.145,
}

def to_usd(amount, currency: str) -> float | None:
    """Convert a tuition amount to USD using fixed exchange rates."""
    if amount is None:
        return None
    rate = EXCHANGE_RATES.get((currency or "USD").upper().strip(), 1.0)
    return round(amount * rate, 2)

# ── Extraction schema ──────────────────────────────────────────────────────────

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "tuition_min":             {"type": "number",  "description": "Minimum annual tuition in local currency"},
        "tuition_max":             {"type": "number",  "description": "Maximum annual tuition in local currency"},
        "tuition_currency":        {"type": "string",  "description": "Currency of tuition (USD, GBP, EUR, etc.)"},
        "ielts_min":               {"type": "number",  "description": "Minimum IELTS score required (e.g. 6.5)"},
        "toefl_min":               {"type": "number",  "description": "Minimum TOEFL iBT score required (e.g. 80)"},
        "gpa_min":                 {"type": "number",  "description": "Minimum GPA required on 4.0 scale"},
        "programs":                {"type": "string",  "description": "Comma-separated list of available programs/majors"},
        "intakes":                 {"type": "string",  "description": "Available intake months e.g. September, January"},
        "application_deadline":    {"type": "string",  "description": "Application deadline date or description"},
        "scholarship_available":   {"type": "string",  "description": "Yes, No, or Partial"},
        "notes":                   {"type": "string",  "description": "Any other important info for international students"},
    }
}

EXTRACTION_PROMPT = """
You are extracting international student admissions information from a university website.
Extract ONLY information that is clearly stated on the page.
If a value is not found, leave it null — do NOT guess.
Keep tuition in the original currency (do not convert) — include the currency code separately.
GPA: convert to 4.0 scale if stated differently.
"""

# ── Scrape a single university ─────────────────────────────────────────────────

async def scrape_university(uni: University) -> dict:
    """Scrape a single university and return extracted data dict."""
    strategy = LLMExtractionStrategy(
        provider=f"anthropic/{MODEL}",
        schema=EXTRACTION_SCHEMA,
        extraction_type="schema",
        instruction=EXTRACTION_PROMPT,
    )

    browser_cfg = BrowserConfig(headless=True, verbose=False)
    run_cfg = CrawlerRunConfig(extraction_strategy=strategy, page_timeout=30000)

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        try:
            result = await crawler.arun(url=uni.website, config=run_cfg)
            if result.success and result.extracted_content:
                data = json.loads(result.extracted_content)
                if isinstance(data, list):
                    data = data[0] if data else {}
                return data
            else:
                print(f"  ❌ Failed to scrape {uni.name}: {result.error_message}")
                return {}
        except Exception as e:
            print(f"  ❌ Exception scraping {uni.name}: {e}")
            return {}

# ── Write results to DB ────────────────────────────────────────────────────────

def update_university(db, uni: University, data: dict):
    """Apply scraped data to the university record."""
    fields = [
        "tuition_min", "tuition_max", "tuition_currency",
        "ielts_min", "toefl_min", "gpa_min",
        "programs", "intakes", "application_deadline",
        "scholarship_available", "notes",
    ]
    for field in fields:
        if data.get(field) is not None:
            setattr(uni, field, data[field])

    # Normalize tuition to USD and store separately
    uni.tuition_usd = to_usd(
        data.get("tuition_min"),
        data.get("tuition_currency", "USD"),
    )

    uni.last_scraped = datetime.utcnow()
    uni.re_scrape_after = datetime.utcnow() + timedelta(days=90)
    uni.scrape_status = "success" if data else "failed"
    db.commit()

# ── Parallel runner ────────────────────────────────────────────────────────────

async def run_scraper(target_ids=None, refresh_all=False, stale_only=False):
    db = SessionLocal()

    if target_ids:
        unis = db.query(University).filter(University.id.in_(target_ids)).all()
    elif refresh_all:
        unis = db.query(University).all()
    elif stale_only:
        unis = db.query(University).filter(
            University.re_scrape_after <= datetime.utcnow()
        ).all()
    else:
        unis = db.query(University).filter(University.scrape_status == "pending").all()

    print(f"\n🔍 Scraping {len(unis)} universities...\n")

    sem = asyncio.Semaphore(5)  # max 5 concurrent browser sessions

    async def scrape_with_limit(uni):
        async with sem:
            print(f"  ▶ {uni.name} — {uni.website}")
            data = await scrape_university(uni)
            update_university(db, uni, data)
            if data:
                filled = len([v for v in data.values() if v is not None])
                currency = data.get("tuition_currency", "USD")
                usd_val = f"${uni.tuition_usd:,.0f}" if uni.tuition_usd else "—"
                print(f"  ✅ {uni.name}: {filled} fields | tuition_usd={usd_val} ({currency})")
            else:
                print(f"  ❌ {uni.name}: no data extracted")
            await asyncio.sleep(1)  # polite delay per worker

    tasks = [scrape_with_limit(u) for u in unis]
    await asyncio.gather(*tasks)

    db.close()
    print("\n✅ Scraping complete!")

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all",   action="store_true", help="Re-scrape all universities")
    parser.add_argument("--stale", action="store_true", help="Re-scrape records past their re_scrape_after date")
    parser.add_argument("--id",    type=int, nargs="+", help="Scrape specific university IDs")
    args = parser.parse_args()

    asyncio.run(run_scraper(
        target_ids=args.id,
        refresh_all=args.all,
        stale_only=args.stale,
    ))
