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

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from dotenv import load_dotenv
import anthropic
import asyncio
import logging
import os
import sys
import tempfile

# Load .env from the backend/ directory first, then fall back to the repo root.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))
load_dotenv(os.path.join(_BACKEND_DIR, "..", ".env"))

from models import University, SessionLocal, get_db, to_usd
from config import (
    MIN_RESULTS,
    MAX_UNIVERSITIES_TO_CLAUDE,
    SCORING_WEIGHTS,
    BUDGET_HEADROOM_THRESHOLD,
    GPA_HEADROOM_THRESHOLD,
    CLAUDE_MODEL,
    CLAUDE_MAX_TOKENS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ai-sana")

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

_ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
if not _ANTHROPIC_API_KEY:
    raise RuntimeError(
    )
client = anthropic.Anthropic(api_key=_ANTHROPIC_API_KEY)

# ── Pydantic models ────────────────────────────────────────────────────────────

class StudentProfile(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    gpa: Optional[float] = Field(None, ge=0.0, le=4.0)
    ielts: Optional[float] = Field(None, ge=0.0, le=9.0)
    toefl: Optional[int] = Field(None, ge=0, le=120)
    budget_usd: Optional[int] = Field(None, ge=0, le=1_000_000)
    preferred_countries: Optional[List[str]] = Field(default_factory=list)
    preferred_programs: Optional[List[str]] = Field(default_factory=list)
    history: Optional[List[dict]] = Field(default_factory=list)

    @field_validator("preferred_countries", "preferred_programs", mode="before")
    @classmethod
    def _clean_str_list(cls, v):
        if v is None:
            return []
        return [str(item).strip() for item in v if str(item).strip()]

class ChatResponse(BaseModel):
    reply: str
    matched_universities: List[dict]

# ── Filtering logic ────────────────────────────────────────────────────────────


def filter_universities(db: Session, profile: "StudentProfile") -> List[University]:
    """Hard eligibility filter — only universities the student qualifies for.

    Two-phase filter:
      1. HARD FILTER:  Country preference (if specified) — never relaxed.
      2. SOFT FILTERS: Programs, Budget, Language, GPA — progressively relaxed
                       if results < MIN_RESULTS.

    NULL handling:
      NULL data fields are treated as eligible (university not yet scraped).
      Filters are expressed as ``or_(field.is_(None), field <= requirement)``
      so a missing value never disqualifies a row.

    Progressive relaxation:
      If results < MIN_RESULTS, filters are dropped one at a time in this
      order (softest → hardest): programs → budget → language → GPA.
      Returns the earliest result set meeting the threshold, or whatever
      remains if all optional filters are dropped.

    Args:
        db:      SQLAlchemy session.
        profile: Student profile with gpa, ielts, toefl, budget_usd,
                 preferred_countries, preferred_programs.

    Returns:
        Universities matching the (possibly relaxed) filter set.

    Example:
        >>> profile = StudentProfile(
        ...     message="CS in Europe",
        ...     gpa=3.5,
        ...     budget_usd=20000,
        ...     preferred_countries=["UK", "Germany"],
        ...     preferred_programs=["Computer Science"],
        ... )
        >>> filter_universities(db, profile)   # doctest: +SKIP
        [<University ...>, ...]
    """
    base = db.query(University)
    if profile.preferred_countries:
        countries_lower = [c.strip().lower() for c in profile.preferred_countries]
        country_filters = [University.country.ilike(f"%{c}%") for c in countries_lower]
        base = base.filter(or_(*country_filters))
        logger.info("Country filter applied: %s", profile.preferred_countries)

    # Build optional filter clauses in DROP ORDER (softest first — index 0 drops first).
    clauses = []

    if profile.preferred_programs:
        program_filters = [
            University.programs.ilike(f"%{p.strip()}%")
            for p in profile.preferred_programs if p.strip()
        ]
        if program_filters:
            clauses.append(("programs", or_(
                University.programs.is_(None),
                *program_filters,
            )))
            logger.info("Program filter queued: %s", profile.preferred_programs)

    if profile.budget_usd is not None:
        clauses.append(("budget", or_(
            University.tuition_usd.is_(None),
            University.tuition_usd <= profile.budget_usd,
        )))
        logger.info("Budget filter queued: $%s", f"{profile.budget_usd:,}")

    if profile.ielts is not None or profile.toefl is not None:
        lang_or = []
        if profile.ielts is not None:
            lang_or.append(or_(
                University.ielts_min.is_(None),
                University.ielts_min <= profile.ielts,
            ))
        if profile.toefl is not None:
            lang_or.append(or_(
                University.toefl_min.is_(None),
                University.toefl_min <= profile.toefl,
            ))
        # A student satisfying either IELTS or TOEFL is eligible.
        clauses.append(("language", or_(*lang_or)))
        logger.info("Language filter queued: ielts=%s toefl=%s", profile.ielts, profile.toefl)

    if profile.gpa is not None:
        clauses.append(("gpa", or_(
            University.gpa_min.is_(None),
            University.gpa_min <= profile.gpa,
        )))
        logger.info("GPA filter queued: %s", profile.gpa)

    # Try with all filters, then progressively drop from the front of `clauses`.
    results: List[University] = []
    for drop_count in range(len(clauses) + 1):
        active = clauses[drop_count:]
        query = base
        for _, clause in active:
            query = query.filter(clause)
        results = query.all()

        if drop_count == 0:
            logger.info("Iteration %d: applying all %d filters → %d results",
                        drop_count, len(clauses), len(results))
        else:
            dropped = [clauses[i][0] for i in range(drop_count)]
            logger.info("Iteration %d: dropped %s, %d filters remain → %d results",
                        drop_count, dropped, len(active), len(results))

        if len(results) >= MIN_RESULTS:
            logger.info("Found %d universities (>= %d threshold)", len(results), MIN_RESULTS)
            return results

    logger.warning("Final result: %d universities (below %d threshold)",
                   len(results), MIN_RESULTS)
    return results


def score_university(uni: University, profile: "StudentProfile") -> float:
    """Rank universities by how well they match the student's preferences."""
    score = 0.0

    # Country preference match
    if profile.preferred_countries:
        for country in profile.preferred_countries:
            if uni.country and country.lower() in uni.country.lower():
                score += SCORING_WEIGHTS["country_match"]
                break

    # Program keyword match
    if profile.preferred_programs and uni.programs:
        programs_lower = uni.programs.lower()
        for prog in profile.preferred_programs:
            if prog.lower() in programs_lower:
                score += SCORING_WEIGHTS["program_match"]

    # Scholarship bonus
    if uni.scholarship_available:
        s = uni.scholarship_available.lower()
        if "yes" in s:
            score += SCORING_WEIGHTS["scholarship_yes"]
        elif "partial" in s:
            score += SCORING_WEIGHTS["scholarship_partial"]

    # Budget headroom (tuition well below budget = better fit)
    if profile.budget_usd and uni.tuition_usd:
        headroom = (profile.budget_usd - uni.tuition_usd) / profile.budget_usd
        if headroom > BUDGET_HEADROOM_THRESHOLD:
            score += SCORING_WEIGHTS["budget_headroom"]

    # GPA headroom (student well above minimum = safer bet)
    if profile.gpa and uni.gpa_min:
        if (profile.gpa - uni.gpa_min) >= GPA_HEADROOM_THRESHOLD:
            score += SCORING_WEIGHTS["gpa_headroom"]

    return score


def rank_universities(unis: List[University], profile: "StudentProfile", top_n: int = 25) -> List[University]:
    scored = [(u, score_university(u, profile)) for u in unis]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [u for u, _ in scored[:top_n]]


def build_coverage_summary(db: Session) -> str:
    """One-paragraph summary of the full DB so Claude can answer coverage questions
    (e.g. "what countries do you have?") even though only the top matches are
    serialized into the detailed list."""
    rows = (
        db.query(University.country, func.count(University.id))
        .group_by(University.country)
        .order_by(func.count(University.id).desc())
        .all()
    )
    total = sum(c for _, c in rows)
    parts = [f"{country} ({count})" for country, count in rows if country]
    return (
        f"FULL DATABASE COVERAGE: {total} partner universities across "
        f"{len(parts)} countries — {', '.join(parts)}.\n"
        "The detailed list below is only the top matches for the current student; "
        "the full coverage figures above are authoritative when the consultant asks "
        "about which countries/how many universities you have."
    )


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

    # Step 2: Score and rank — top N go to Claude
    matched = rank_universities(matched, profile, top_n=MAX_UNIVERSITIES_TO_CLAUDE)

    # Step 3: Build Claude context
    coverage = build_coverage_summary(db)
    uni_context = universities_to_context(matched)
    system = (
        SYSTEM_PROMPT
        + f"\n\n## DATABASE COVERAGE OVERVIEW:\n{coverage}"
        + f"\n\n## TOP MATCHES FOR THIS STUDENT (detailed):\n{uni_context}"
    )
    messages = profile.history + [{"role": "user", "content": profile.message}]

    # Step 4: Call Claude
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
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


# ── Admin panel ────────────────────────────────────────────────────────────────
# TODO: add auth — these endpoints are open for internal use only.

REQUIRED_COMPLETE_FIELDS = ("tuition_min", "ielts_min", "gpa_min", "programs", "intakes")


def _is_complete(u: University) -> bool:
    """A row counts as complete if all five spec fields are non-null & non-empty."""
    for field in REQUIRED_COMPLETE_FIELDS:
        v = getattr(u, field, None)
        if v is None:
            return False
        if isinstance(v, str) and not v.strip():
            return False
    return True


def _university_to_dict(u: University) -> dict:
    return {
        "id": u.id,
        "name": u.name,
        "country": u.country,
        "city": u.city,
        "website": u.website,
        "qs_ranking": u.qs_ranking,
        "tuition_min": u.tuition_min,
        "tuition_max": u.tuition_max,
        "tuition_currency": u.tuition_currency,
        "tuition_usd": u.tuition_usd,
        "ielts_min": u.ielts_min,
        "toefl_min": u.toefl_min,
        "gpa_min": u.gpa_min,
        "programs": u.programs,
        "intakes": u.intakes,
        "application_deadline": u.application_deadline,
        "scholarship_available": u.scholarship_available,
        "notes": u.notes,
        "scrape_status": u.scrape_status,
        "last_scraped": u.last_scraped.isoformat() if u.last_scraped else None,
    }


class UniversityCreate(BaseModel):
    name: str = Field(..., min_length=1)
    country: Optional[str] = None
    city: Optional[str] = None
    website: Optional[str] = None
    qs_ranking: Optional[int] = None
    tuition_min: Optional[float] = None
    tuition_max: Optional[float] = None
    tuition_currency: Optional[str] = "USD"
    ielts_min: Optional[float] = None
    toefl_min: Optional[int] = None
    gpa_min: Optional[float] = None
    programs: Optional[str] = None
    intakes: Optional[str] = None
    application_deadline: Optional[str] = None
    scholarship_available: Optional[str] = None
    notes: Optional[str] = None


class UniversityUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    country: Optional[str] = None
    city: Optional[str] = None
    website: Optional[str] = None
    qs_ranking: Optional[int] = None
    tuition_min: Optional[float] = None
    tuition_max: Optional[float] = None
    tuition_currency: Optional[str] = None
    ielts_min: Optional[float] = None
    toefl_min: Optional[int] = None
    gpa_min: Optional[float] = None
    programs: Optional[str] = None
    intakes: Optional[str] = None
    application_deadline: Optional[str] = None
    scholarship_available: Optional[str] = None
    notes: Optional[str] = None


@app.get("/admin/universities")
def admin_list_universities(db: Session = Depends(get_db)):
    """Return every university grouped by country with completeness counts."""
    unis = db.query(University).all()
    by_country: dict[str, list[University]] = {}
    for u in unis:
        key = (u.country or "Unknown").strip() or "Unknown"
        by_country.setdefault(key, []).append(u)

    countries = []
    total_complete = 0
    for country_name in sorted(by_country.keys(), key=lambda s: s.lower()):
        country_unis = sorted(by_country[country_name], key=lambda x: (x.name or "").lower())
        complete = sum(1 for u in country_unis if _is_complete(u))
        total_complete += complete
        countries.append({
            "name": country_name,
            "total": len(country_unis),
            "complete": complete,
            "universities": [_university_to_dict(u) for u in country_unis],
        })

    return {
        "countries": countries,
        "total_universities": len(unis),
        "total_complete": total_complete,
    }


@app.get("/admin/countries")
def admin_countries(db: Session = Depends(get_db)):
    rows = db.query(University.country).distinct().all()
    names = sorted(
        {r[0].strip() for r in rows if r[0] and r[0].strip()},
        key=lambda s: s.lower(),
    )
    return {"countries": names}


@app.get("/admin/universities/{uni_id}")
def admin_get_university(uni_id: int, db: Session = Depends(get_db)):
    u = db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")
    return _university_to_dict(u)


@app.post("/admin/universities")
def admin_create_university(body: UniversityCreate, db: Session = Depends(get_db)):
    u = University()
    for k, v in body.model_dump().items():
        setattr(u, k, v)
    u.tuition_usd = to_usd(u.tuition_min, u.tuition_currency)
    db.add(u)
    db.commit()
    db.refresh(u)
    return _university_to_dict(u)


@app.put("/admin/universities/{uni_id}")
def admin_update_university(uni_id: int, body: UniversityUpdate, db: Session = Depends(get_db)):
    u = db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(u, k, v)
    # Recompute USD any time tuition or currency could have changed (cheap & safe).
    u.tuition_usd = to_usd(u.tuition_min, u.tuition_currency)
    db.commit()
    db.refresh(u)
    return _university_to_dict(u)


@app.delete("/admin/universities/{uni_id}")
def admin_delete_university(uni_id: int, db: Session = Depends(get_db)):
    u = db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")
    db.delete(u)
    db.commit()
    return {"deleted": True, "id": uni_id}


@app.post("/admin/import-csv")
async def admin_import_csv(file: UploadFile = File(...)):
    """Bulk-import universities from an uploaded CSV.

    Existing rows (matched by `name`) are updated; new rows are inserted.
    Returns a summary: ``{added, updated, skipped, errors}``.
    """
    filename = (file.filename or "").lower()
    if not filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")

    with tempfile.NamedTemporaryFile(mode="wb", suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
        tmp.write(await file.read())

    try:
        from import_universities import import_csv
        summary = import_csv(tmp_path, update=True)
    except Exception as exc:
        logger.exception("CSV import failed")
        raise HTTPException(status_code=500, detail=f"Import failed: {exc}")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return summary


# ── Startup: auto-import partner CSV if DB is empty ────────────────────────────

@app.on_event("startup")
async def auto_import_on_startup():
    db = SessionLocal()
    try:
        count = db.query(University).count()
        if count == 0:
            csv_path = os.path.join(_BACKEND_DIR, "data", "universities_real.csv")
            if os.path.exists(csv_path):
                try:
                    from import_universities import import_csv
                    import_csv(csv_path, update=False)
                    logger.info("Auto-imported universities_real.csv on startup")
                except Exception as exc:
                    logger.exception("Auto-import failed: %s", exc)
            else:
                logger.warning("Auto-import skipped: %s not found", csv_path)
    finally:
        db.close()