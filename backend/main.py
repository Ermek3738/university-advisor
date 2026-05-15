"""
main.py — FastAPI Backend
─────────────────────────
Endpoints:
  POST /chat          → main chatbot endpoint
  GET  /universities  → list all universities (with filters)
  GET  /stats         → database stats

Environment variables required:
  ANTHROPIC_API_KEY   → your Anthropic key
  DATABASE_URL        → Supabase PostgreSQL URL (falls back to SQLite locally)

Run locally:
  uvicorn main:app --reload
"""

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import or_
import anthropic
import asyncio
import os
import sys

from models import University, SessionLocal, get_db

# Make the sibling scraper/ package importable for the /scrape endpoint.
_SCRAPER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scraper")
if _SCRAPER_DIR not in sys.path:
    sys.path.insert(0, _SCRAPER_DIR)

app = FastAPI(title="AI-Sana API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ── Pydantic models ────────────────────────────────────────────────────────────

class StudentProfile(BaseModel):
    message: str
    gpa: Optional[float] = None
    ielts: Optional[float] = None
    toefl: Optional[int] = None
    budget_usd: Optional[int] = None
    preferred_countries: Optional[List[str]] = []
    preferred_programs: Optional[List[str]] = []
    history: Optional[List[dict]] = []

class ChatResponse(BaseModel):
    reply: str
    matched_universities: List[dict]

# ── Filtering logic ────────────────────────────────────────────────────────────

def filter_universities(db: Session, profile: "StudentProfile") -> List[University]:
    """Hard eligibility filter — only universities the student qualifies for.

    NULL data fields are treated as eligible: if a university hasn't been scraped
    yet (tuition/IELTS/TOEFL/GPA are NULL), it still passes academic + budget
    filters. Country preference is preserved even in the relaxed fallback.
    """
    # Country lives in the base query so the fallback can reuse it.
    base = db.query(University)
    if profile.preferred_countries:
        countries_lower = [c.strip().lower() for c in profile.preferred_countries]
        country_filters = [University.country.ilike(f"%{c}%") for c in countries_lower]
        base = base.filter(or_(*country_filters))

    query = base

    if profile.gpa is not None:
        query = query.filter(or_(
            University.gpa_min.is_(None),
            University.gpa_min <= profile.gpa,
        ))

    if profile.ielts is not None:
        query = query.filter(or_(
            University.ielts_min.is_(None),
            University.ielts_min <= profile.ielts,
        ))

    if profile.toefl is not None:
        query = query.filter(or_(
            University.toefl_min.is_(None),
            University.toefl_min <= profile.toefl,
        ))

    if profile.budget_usd is not None:
        query = query.filter(or_(
            University.tuition_usd.is_(None),
            University.tuition_usd <= profile.budget_usd,
            University.tuition_min.is_(None),
            University.tuition_min <= profile.budget_usd,
        ))

    results = query.all()

    # Fallback: too few hits — drop the academic/budget filters but keep country.
    if len(results) < 5:
        results = base.all()

    return results


def score_university(uni: University, profile: "StudentProfile") -> float:
    """Rank universities by how well they match the student's preferences."""
    score = 0.0

    # Country preference match
    if profile.preferred_countries:
        for country in profile.preferred_countries:
            if uni.country and country.lower() in uni.country.lower():
                score += 3
                break

    # Program keyword match
    if profile.preferred_programs and uni.programs:
        programs_lower = uni.programs.lower()
        for prog in profile.preferred_programs:
            if prog.lower() in programs_lower:
                score += 2

    # Scholarship bonus
    if uni.scholarship_available:
        s = uni.scholarship_available.lower()
        if "yes" in s:
            score += 2
        elif "partial" in s:
            score += 1

    # Budget headroom (tuition well below budget = better fit)
    if profile.budget_usd and uni.tuition_usd:
        headroom = (profile.budget_usd - uni.tuition_usd) / profile.budget_usd
        if headroom > 0.2:
            score += 1

    # GPA headroom (student well above minimum = safer bet)
    if profile.gpa and uni.gpa_min:
        if (profile.gpa - uni.gpa_min) >= 0.5:
            score += 1

    return score


def rank_universities(unis: List[University], profile: "StudentProfile", top_n: int = 15) -> List[University]:
    scored = [(u, score_university(u, profile)) for u in unis]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [u for u, _ in scored[:top_n]]


def universities_to_context(unis: List[University]) -> str:
    """Serialize universities to text for Claude's context."""
    lines = []
    for u in unis:
        line = f"- **{u.name}** ({u.country}, {u.city})"
        line += f"\n  Website: {u.website}"
        if u.tuition_usd:
            line += f"\n  Tuition: ~${u.tuition_usd:,.0f}/year (USD)"
        elif u.tuition_min:
            line += f"\n  Tuition: {u.tuition_currency or 'USD'} {u.tuition_min:,.0f}"
            if u.tuition_max:
                line += f" – {u.tuition_max:,.0f}/year"
        if u.ielts_min:
            line += f"\n  IELTS: {u.ielts_min}"
        if u.toefl_min:
            line += f"\n  TOEFL: {u.toefl_min}"
        if u.gpa_min:
            line += f"\n  Min GPA: {u.gpa_min}"
        if u.programs:
            line += f"\n  Programs: {u.programs[:80]}"
        if u.intakes:
            line += f"\n  Intakes: {u.intakes}"
        if u.application_deadline:
            line += f"\n  Deadline: {u.application_deadline}"
        if u.scholarship_available:
            line += f"\n  Scholarship: {u.scholarship_available}"
        if u.notes:
            line += f"\n  Notes: {u.notes[:150]}"
        lines.append(line)
    return "\n\n".join(lines)

# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert university admissions consultant assistant for a study abroad agency based in Central Asia.
Your job is to help consultants quickly find the best matching universities for their students.
Respond in BOTH Russian and English: section headers and labels in Russian, university names and links in English.

When given a student profile and a list of partner universities, recommend the TOP 5 best matches.

For EACH university use EXACTLY this format:

---
🏛️ [Number]. [University Name] [Country Flag Emoji]
🌐 [website link]
📍 Город/Страна: [City, Country]

🏆 Рейтинг: [QS World Ranking if known, otherwise leave blank]

ℹ️ О университете: [2-3 sentences about the university strengths and why international students choose it]

💰 Стоимость обучения:
  • Foundation: [amount + currency, or Нет программы]
  • Direct Entry (Bachelor): [amount + currency per year]
  • Master: [amount + currency per year if relevant]

🏠 Средние расходы на проживание: [monthly estimate, include rent + food + transport in USD]

📋 Требования для поступления:
  • GPA: [minimum or Не указано]
  • IELTS: [minimum score or Не указано]
  • TOEFL: [minimum score or Не указано]
  • Другие требования: [any other notable requirements]

📅 Дедлайны и intake: [application deadlines and start dates]

🎓 Стипендии: [scholarship options or Уточнить]

🛂 Виза: [visa type required e.g. Tier 4 Student Visa UK, F-1 USA, Study Permit Canada + brief note on process]

🚀 Карьерные перспективы: [post-study work rights, graduate employment, key industries]

✅ Почему подходит этому студенту: [2-3 specific reasons matching THIS student profile]

⚠️ Важно учесть: [warnings - budget, competition, visa difficulty etc]

---

After all 5 add a comparison table and a 3-sentence consultant recommendation in Russian.
Use your own knowledge for rankings, living costs, visa info and career prospects.
Be specific and actionable. The consultant will share this directly with the student."""

# ── Chat endpoint ──────────────────────────────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat(profile: StudentProfile, db: Session = Depends(get_db)):
    # Step 1: Hard filter
    matched = filter_universities(db, profile)

    # Step 2: Score and rank — top 15 go to Claude
    matched = rank_universities(matched, profile, top_n=15)

    # Step 3: Build Claude context
    uni_context = universities_to_context(matched)
    system = SYSTEM_PROMPT + f"\n\n## YOUR PARTNER UNIVERSITIES DATABASE:\n{uni_context}"
    messages = profile.history + [{"role": "user", "content": profile.message}]

    # Step 4: Call Claude
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=system,
            messages=messages,
        )
        reply = response.content[0].text
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Claude API error: {str(e)}")

    # Step 5: Return
    matched_dicts = [
        {
            "id": u.id,
            "name": u.name,
            "country": u.country,
            "website": u.website,
            "tuition_usd": u.tuition_usd,
            "tuition_min": u.tuition_min,
            "ielts_min": u.ielts_min,
            "gpa_min": u.gpa_min,
        }
        for u in matched[:10]
    ]

    return ChatResponse(reply=reply, matched_universities=matched_dicts)

# ── Other endpoints ────────────────────────────────────────────────────────────

@app.get("/universities")
def list_universities(
    country: Optional[str] = None,
    min_gpa: Optional[float] = None,
    db: Session = Depends(get_db)
):
    query = db.query(University)
    if country:
        query = query.filter(University.country.ilike(f"%{country}%"))
    if min_gpa:
        query = query.filter(University.gpa_min <= min_gpa)
    return query.all()


@app.get("/stats")
def stats(db: Session = Depends(get_db)):
    total = db.query(University).count()
    scraped = db.query(University).filter(University.scrape_status == "success").count()
    failed = db.query(University).filter(University.scrape_status == "failed").count()
    pending = db.query(University).filter(University.scrape_status == "pending").count()
    return {"total": total, "scraped": scraped, "failed": failed, "pending": pending}


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Scrape trigger ─────────────────────────────────────────────────────────────

_scrape_task: Optional[asyncio.Task] = None


@app.post("/scrape")
async def trigger_scrape(
    refresh_all: bool = False,
    stale_only: bool = False,
    concurrency: int = 5,
):
    """Kick off a background scrape of pending universities.

    Returns immediately. Progress is printed to the server logs.

    Query params:
      refresh_all=true  → re-scrape every university
      stale_only=true   → re-scrape rows past their re_scrape_after date
      concurrency=N     → number of concurrent workers (default 5)
    """
    global _scrape_task

    if _scrape_task and not _scrape_task.done():
        return {"status": "already_running"}

    from scraper import run_scraper  # imports scraper/scraper.py

    _scrape_task = asyncio.create_task(run_scraper(
        refresh_all=refresh_all,
        stale_only=stale_only,
        concurrency=concurrency,
    ))

    return {
        "status": "started",
        "refresh_all": refresh_all,
        "stale_only": stale_only,
        "concurrency": concurrency,
    }


@app.get("/scrape/status")
def scrape_status():
    """Check whether a background scrape is currently running."""
    running = bool(_scrape_task and not _scrape_task.done())
    return {"running": running}