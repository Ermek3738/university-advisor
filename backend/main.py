"""
main.py — FastAPI Backend
─────────────────────────
Endpoints:
  POST /chat          → main chatbot endpoint
  GET  /universities  → list all universities (with filters)
  GET  /stats         → database stats
  POST /scrape/{id}   → trigger scrape for one university

Install:
  pip install fastapi uvicorn anthropic sqlalchemy

Run:
  uvicorn main:app --reload
"""

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
import anthropic
import os
import sys

sys.path.append("../backend")
from models import University, SessionLocal, get_db

app = FastAPI(title="University Advisor API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ── Pydantic models ────────────────────────────────────────────────────────────

class StudentProfile(BaseModel):
    message: str                         # free-text from consultant
    gpa: Optional[float] = None          # 0-4.0
    ielts: Optional[float] = None        # e.g. 6.5
    toefl: Optional[int] = None          # e.g. 80
    budget_usd: Optional[int] = None     # annual budget
    preferred_countries: Optional[List[str]] = []
    preferred_programs: Optional[List[str]] = []
    history: Optional[List[dict]] = []   # conversation history

class ChatResponse(BaseModel):
    reply: str
    matched_universities: List[dict]

# ── Filtering logic ────────────────────────────────────────────────────────────

def filter_universities(db: Session, profile: StudentProfile) -> List[University]:
    """Pre-filter universities before sending to Claude."""
    from sqlalchemy import or_
    query = db.query(University)

    # GPA filter
    if profile.gpa is not None:
        query = query.filter(
            (University.gpa_min == None) | (University.gpa_min <= profile.gpa)
        )

    # English filter (IELTS)
    if profile.ielts is not None:
        query = query.filter(
            (University.ielts_min == None) | (University.ielts_min <= profile.ielts)
        )

    # English filter (TOEFL)
    if profile.toefl is not None:
        query = query.filter(
            (University.toefl_min == None) | (University.toefl_min <= profile.toefl)
        )

    # Budget filter (use tuition_usd if available, fall back to tuition_min)
    if profile.budget_usd is not None:
        query = query.filter(
            or_(
                University.tuition_usd == None,
                University.tuition_usd <= profile.budget_usd,
                University.tuition_min == None,
                University.tuition_min <= profile.budget_usd,
            )
        )

    # Country filter — fixed: individual OR clauses instead of joined ILIKE
    if profile.preferred_countries:
        countries_lower = [c.strip().lower() for c in profile.preferred_countries]
        country_filters = [University.country.ilike(f"%{c}%") for c in countries_lower]
        query = query.filter(or_(*country_filters))

    results = query.limit(93).all()

    # If too few results, relax all filters and return everything
    if len(results) < 5:
        results = db.query(University).all()

    return results

def universities_to_context(unis: List[University]) -> str:
    """Convert university list to text for Claude."""
    lines = []
    for u in unis:
        line = f"- **{u.name}** ({u.country}, {u.city})"
        line += f"\n  Website: {u.website}"
        if u.tuition_min:
            line += f"\n  Tuition: ${u.tuition_min:,.0f}"
            if u.tuition_max:
                line += f" – ${u.tuition_max:,.0f}/year"
        if u.ielts_min:
            line += f"\n  IELTS: {u.ielts_min}"
        if u.toefl_min:
            line += f"\n  TOEFL: {u.toefl_min}"
        if u.gpa_min:
            line += f"\n  Min GPA: {u.gpa_min}"
        if u.programs:
            line += f"\n  Programs: {u.programs[:120]}"
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

# ── Chat endpoint ──────────────────────────────────────────────────────────────

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

@app.post("/chat", response_model=ChatResponse)
async def chat(profile: StudentProfile, db: Session = Depends(get_db)):
    # Step 1: Filter universities from DB
    matched = filter_universities(db, profile)
    uni_context = universities_to_context(matched)

    # Step 2: Build messages for Claude
    system = SYSTEM_PROMPT + f"\n\n## YOUR PARTNER UNIVERSITIES DATABASE:\n{uni_context}"

    messages = profile.history + [{"role": "user", "content": profile.message}]

    # Step 3: Call Claude
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

    # Step 4: Return response
    matched_dicts = [
        {
            "id": u.id,
            "name": u.name,
            "country": u.country,
            "website": u.website,
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
