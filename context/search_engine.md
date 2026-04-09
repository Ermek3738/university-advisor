# Search Engine — Implementation Breakdown

Each stage below is self-contained and can be built independently. Work through them in order — each stage feeds the next.

---

## Stage 1: Data Model

**File:** `backend/models.py`
**Status:** Done — but needs one new column for scoring.

### What it does
Defines the SQLite schema via SQLAlchemy. Every university record lives here. The scraper writes to it; the filter reads from it.

### Current schema
```python
class University(Base):
    __tablename__ = "universities"

    id                   = Column(Integer, primary_key=True)
    name                 = Column(String, nullable=False)
    country              = Column(String)
    city                 = Column(String)
    website              = Column(String)

    tuition_min          = Column(Float)      # USD/year
    tuition_max          = Column(Float)
    tuition_currency     = Column(String, default="USD")

    ielts_min            = Column(Float)      # e.g. 6.5
    toefl_min            = Column(Integer)    # e.g. 80
    gpa_min              = Column(Float)      # out of 4.0

    programs             = Column(Text)       # comma-separated
    intakes              = Column(String)     # "September, January"
    application_deadline = Column(String)
    scholarship_available= Column(String)     # "Yes / No / Partial"
    notes                = Column(Text)

    scrape_status        = Column(String, default="pending")  # pending/success/failed
    last_scraped         = Column(DateTime)
    created_at           = Column(DateTime, default=datetime.utcnow)
```

### What to add
Add a `tuition_usd` column to store the currency-normalized value. This decouples the scraper's raw value from the filter's comparison logic:

```python
tuition_usd = Column(Float, nullable=True)   # always USD, populated post-scrape
```

Add a `re_scrape_after` column for staleness tracking:

```python
re_scrape_after = Column(DateTime, nullable=True)  # auto-requeue after this date
```

### How to apply changes
After editing `models.py`, delete `universities.db` and let SQLAlchemy recreate it, or use Alembic for migrations if data must be preserved:
```bash
# Quick reset (dev only)
rm backend/universities.db
python -c "from models import Base, engine; Base.metadata.create_all(engine)"
```

---

## Stage 2: CSV Import

**File:** `backend/import_universities.py`
**Status:** Done — works correctly as-is.

### What it does
One-time (or repeatable) script that reads your partner university spreadsheet and inserts rows into SQLite. Deduplicates by `website` URL so re-runs are safe.

### How to run
```bash
cd backend
python import_universities.py --file ../data/universities_real.csv
```

### Expected CSV columns
| Column | Required | Notes |
|---|---|---|
| `name` | Yes | Full university name |
| `website` | Yes | Used as unique key |
| `country` | No | e.g. `UK`, `Germany` |
| `city` | No | |
| `programs` | No | Comma-separated list |
| `ielts_min` | No | Float e.g. `6.5` |
| `toefl_min` | No | Integer e.g. `80` |
| `gpa_min` | No | Float on 4.0 scale |
| `tuition_min` | No | USD/year |
| `tuition_max` | No | USD/year |
| `intakes` | No | `September, January` |
| `scholarship_available` | No | `Yes / No / Partial` |
| `notes` | No | Free text |

### What to improve
Add a `--update` flag to overwrite existing records instead of skipping them, for when you refresh the spreadsheet:

```python
if exists:
    if args.update:
        # overwrite fields
        exists.name = row.get("name")
        # ... etc
        db.commit()
    else:
        skipped += 1
    continue
```

---

## Stage 3: Web Scraper

**File:** `scraper/scraper.py`
**Status:** Done — runs correctly, but sequential (slow for large sets).

### What it does
Crawls each university's website using a headless browser (`crawl4ai`) and passes the page content to Claude with a JSON schema. Claude extracts structured fields and the results are written back to the DB.

### How to run
```bash
cd scraper
python scraper.py                  # scrape all pending
python scraper.py --all            # re-scrape everything
python scraper.py --id 1 2 3       # scrape specific IDs
```

### Extraction schema (what Claude pulls from each page)
```python
EXTRACTION_SCHEMA = {
    "tuition_min":          number   # annual USD
    "tuition_max":          number
    "tuition_currency":     string   # GBP, EUR, USD, etc.
    "ielts_min":            number   # e.g. 6.5
    "toefl_min":            number   # e.g. 80
    "gpa_min":              number   # on 4.0 scale
    "programs":             string   # comma-separated
    "intakes":              string   # e.g. "September, January"
    "application_deadline": string
    "scholarship_available":string   # Yes / No / Partial
    "notes":                string
}
```

### Status lifecycle
```
pending → (scrape runs) → success
                       → failed   (network error or no content)
```

### What to improve

**1. Currency normalization** — After extracting `tuition_min` + `tuition_currency`, convert to USD and store in `tuition_usd`:
```python
EXCHANGE_RATES = {"GBP": 1.27, "EUR": 1.08, "CAD": 0.74, "AUD": 0.65, "USD": 1.0}

def to_usd(amount, currency):
    if not amount:
        return None
    rate = EXCHANGE_RATES.get(currency.upper(), 1.0)
    return round(amount * rate, 2)

# After extraction:
uni.tuition_usd = to_usd(data.get("tuition_min"), data.get("tuition_currency", "USD"))
```

**2. Set re-scrape date** — Mark each record to be refreshed in 90 days:
```python
from datetime import timedelta
uni.re_scrape_after = datetime.utcnow() + timedelta(days=90)
```

**3. Parallel scraping** — The current loop is sequential with a 2s sleep. For large databases, use `asyncio.gather` with a semaphore to limit concurrency:
```python
sem = asyncio.Semaphore(5)  # max 5 concurrent

async def scrape_with_limit(uni):
    async with sem:
        data = await scrape_university(uni)
        await asyncio.sleep(1)
        return uni, data

tasks = [scrape_with_limit(u) for u in unis]
results = await asyncio.gather(*tasks)
```

---

## Stage 4: Hard Filter (SQL Pre-filter)

**File:** `backend/main.py` — `filter_universities()` (line 59)
**Status:** Partially working — country filter has a bug, programs not filtered.

### What it does
Takes the student profile and queries the DB with hard eligibility constraints. Only universities the student qualifies for (or where the requirement is unknown) pass through. Returns up to 40 results to feed into the scorer.

### Current filter logic
```python
def filter_universities(db, profile):
    query = db.query(University).filter(University.scrape_status != "pending")

    if profile.gpa:
        query = query.filter(
            (University.gpa_min == None) | (University.gpa_min <= profile.gpa)
        )
    if profile.ielts:
        query = query.filter(
            (University.ielts_min == None) | (University.ielts_min <= profile.ielts)
        )
    if profile.toefl:
        query = query.filter(
            (University.toefl_min == None) | (University.toefl_min <= profile.toefl)
        )
    if profile.budget_usd:
        query = query.filter(
            (University.tuition_min == None) | (University.tuition_min <= profile.budget_usd)
        )

    results = query.limit(40).all()
    if len(results) < 5:
        results = db.query(University).limit(40).all()  # fallback: no filters
    return results
```

### What to fix

**Fix 1 — Country filter (broken):**
The current code joins all countries into one ILIKE string (`%uk%germany%`) which never matches. Replace with proper OR clauses:

```python
from sqlalchemy import or_

if profile.preferred_countries:
    countries_lower = [c.strip().lower() for c in profile.preferred_countries]
    country_filters = [University.country.ilike(f"%{c}%") for c in countries_lower]
    query = query.filter(or_(*country_filters))
```

**Fix 2 — Program keyword filter (missing):**
`preferred_programs` is collected but never used. Add keyword matching:

```python
if profile.preferred_programs:
    program_filters = [
        University.programs.ilike(f"%{p.strip()}%")
        for p in profile.preferred_programs
    ]
    query = query.filter(or_(*program_filters))
```

**Fix 3 — Graceful filter relaxation (too aggressive fallback):**
Instead of dropping all filters when fewer than 5 results, relax them one at a time. Replace the fallback block with:

```python
results = query.limit(40).all()

if len(results) < 5:
    # Relax: drop country filter first
    query2 = base_query_without_country(db, profile)
    results = query2.limit(40).all()

if len(results) < 5:
    # Relax further: drop budget filter
    query3 = base_query_gpa_language_only(db, profile)
    results = query3.limit(40).all()

if len(results) < 5:
    # Last resort: return all scraped
    results = db.query(University).filter(
        University.scrape_status == "success"
    ).limit(40).all()
```

**Fix 4 — Use `tuition_usd` for budget comparison (if Stage 3 currency fix is done):**
```python
if profile.budget_usd:
    query = query.filter(
        (University.tuition_usd == None) | (University.tuition_usd <= profile.budget_usd)
    )
```

---

## Stage 5: Pre-LLM Scorer

**File:** `backend/main.py` — new function `score_universities()`
**Status:** Not implemented — currently all 40 filtered results go straight to Claude.

### What it does
After the SQL filter narrows the pool to ≤40 universities, the scorer ranks them by how well they match the student's *preferences* (not just hard eligibility). Only the top 10-15 are sent to Claude, reducing token cost and improving response quality.

### Scoring model
Each university starts at 0 points. Points are added for positive signals:

| Signal | Points |
|---|---|
| Each country preference match | +3 |
| Each program keyword match in `programs` field | +2 |
| Scholarship available (`Yes`) | +2 |
| Scholarship partial | +1 |
| Budget headroom > 20% (tuition well below budget) | +1 |
| GPA headroom > 0.5 (well above minimum) | +1 |

### Implementation

```python
def score_university(uni: University, profile: StudentProfile) -> float:
    score = 0.0

    # Country preference
    if profile.preferred_countries:
        for country in profile.preferred_countries:
            if uni.country and country.lower() in uni.country.lower():
                score += 3
                break

    # Program match
    if profile.preferred_programs and uni.programs:
        programs_lower = uni.programs.lower()
        for prog in profile.preferred_programs:
            if prog.lower() in programs_lower:
                score += 2

    # Scholarship
    if uni.scholarship_available:
        s = uni.scholarship_available.lower()
        if "yes" in s:
            score += 2
        elif "partial" in s:
            score += 1

    # Budget headroom
    if profile.budget_usd and uni.tuition_usd:
        headroom = (profile.budget_usd - uni.tuition_usd) / profile.budget_usd
        if headroom > 0.2:
            score += 1

    # GPA headroom
    if profile.gpa and uni.gpa_min:
        if (profile.gpa - uni.gpa_min) >= 0.5:
            score += 1

    return score


def rank_universities(unis: list, profile: StudentProfile, top_n: int = 15) -> list:
    scored = [(u, score_university(u, profile)) for u in unis]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [u for u, _ in scored[:top_n]]
```

### How it plugs in
In the `chat()` endpoint, between the filter call and the Claude call:

```python
matched = filter_universities(db, profile)     # Stage 4: up to 40
matched = rank_universities(matched, profile)  # Stage 5: top 15
uni_context = universities_to_context(matched) # Stage 6: serialize
```

---

## Stage 6: Context Builder

**File:** `backend/main.py` — `universities_to_context()` (line 103)
**Status:** Done — works, minor improvements possible.

### What it does
Serializes the list of scored universities into a plain-text block that gets injected into Claude's system prompt. This is the "database" Claude reasons over.

### Current output format (per university)
```
- **University Name** (Country, City)
  Website: https://...
  Tuition: $8,000 – $12,000/year
  IELTS: 6.0
  TOEFL: 80
  Min GPA: 2.8
  Programs: Business, Law, Psychology, Health Sciences
  Intakes: September
  Deadline: March 1
  Scholarship: Yes
  Notes: Problem-based learning approach. Affordable for EU/EEA.
```

### What to improve

Add a **match score line** so Claude knows which universities the pre-filter already ranked highest:

```python
line += f"\n  Match Score: {score:.1f}"  # requires passing score through
```

Truncate programs more aggressively — the current 120-char limit can still overflow the context window when there are 15 universities. 80 chars is safer:

```python
if u.programs:
    line += f"\n  Programs: {u.programs[:80]}"
```

---

## Stage 7: Claude Integration (Chat Endpoint)

**File:** `backend/main.py` — `chat()` endpoint (line 182) + `SYSTEM_PROMPT` (line 134)
**Status:** Done — works, but two improvements are high value.

### What it does
Assembles the full prompt, calls the Claude API, and returns the response + matched university metadata to the frontend.

### Request flow
```
Frontend sends:
  {
    message: "Student needs CS, GPA 3.5, IELTS 7.0, budget $15k",
    gpa: 3.5,
    ielts: 7.0,
    budget_usd: 15000,
    preferred_countries: ["Germany", "Netherlands"],
    preferred_programs: ["Computer Science"],
    history: [ {role: "user", content: "..."}, ... ]
  }

Backend builds:
  system = SYSTEM_PROMPT + "\n\n## YOUR PARTNER UNIVERSITIES DATABASE:\n" + uni_context

  messages = [
    ...profile.history,
    { role: "user", content: profile.message }
  ]

Claude receives:
  - System prompt with full university context
  - Conversation history (last 20 turns)
  - Current user message

Claude returns:
  - Top 5 university cards (Russian/English bilingual)
  - Comparison table
  - 3-sentence consultant recommendation
```

### System prompt behavior
Claude is instructed to produce a rigid 11-section card per university:

| Section | Content |
|---|---|
| Header | University name + country flag |
| Website | Clickable link |
| Location | City, Country |
| Ranking | QS World Ranking |
| About | 2-3 sentence summary |
| Tuition | Foundation / Bachelor / Master costs |
| Living costs | Monthly estimate (rent + food + transport) |
| Requirements | GPA, IELTS, TOEFL, other |
| Deadlines | Application deadlines + intake dates |
| Scholarships | Available options |
| Visa | Type + brief process note |
| Career | Post-study work rights, industries |
| Why this student | 2-3 specific reasons for this profile |
| Warnings | Budget, competition, visa difficulty |

### What to improve

**1. Streaming response (high impact UX):**
Replace the blocking `client.messages.create` call with a streaming version:

```python
from fastapi.responses import StreamingResponse

@app.post("/chat/stream")
async def chat_stream(profile: StudentProfile, db: Session = Depends(get_db)):
    matched = filter_universities(db, profile)
    matched = rank_universities(matched, profile)
    uni_context = universities_to_context(matched)
    system = SYSTEM_PROMPT + f"\n\n## YOUR PARTNER UNIVERSITIES DATABASE:\n{uni_context}"
    messages = profile.history + [{"role": "user", "content": profile.message}]

    def generate():
        with client.messages.stream(
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
            max_tokens=2000,
            system=system,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield f"data: {text}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

**2. Externalize model version:**
```python
# Top of main.py
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# In chat():
response = client.messages.create(model=MODEL, ...)
```

**3. Cap history more carefully:**
The current cap is 20 messages by count. Cap by token estimate instead to be safe:

```python
def trim_history(history: list, max_chars: int = 8000) -> list:
    total = 0
    trimmed = []
    for msg in reversed(history):
        total += len(msg.get("content", ""))
        if total > max_chars:
            break
        trimmed.insert(0, msg)
    return trimmed
```

---

## Stage 8: Frontend

**File:** `frontend/index.html`
**Status:** Done — single-file vanilla JS app.

### What it does
- **Sidebar** — collects student profile (GPA, IELTS, TOEFL, budget, countries, programs, notes)
- **Chat area** — sends messages, shows typing indicator, renders AI responses as markdown
- **"Use Profile in Chat"** button — formats sidebar fields into a pre-filled prompt
- **"+ New Consultation"** — clears history and resets the chat

### Data sent to backend on each message
```javascript
{
  message: "free text from the consultant",
  gpa: 3.5,
  ielts: 7.0,
  toefl: null,
  budget_usd: 15000,
  preferred_countries: ["Germany", "Netherlands"],
  preferred_programs: ["Computer Science"],
  history: [ { role: "user", content: "..." }, { role: "assistant", content: "..." } ]
}
```

### What to improve

**1. Switch to streaming** — Once the backend streaming endpoint is live (Stage 7), update `sendMessage()` to consume SSE:

```javascript
const response = await fetch(`${API_BASE}/chat/stream`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = "";

const msgDiv = addMessage("ai", "");

while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  const chunk = decoder.decode(value);
  const lines = chunk.split("\n");
  for (const line of lines) {
    if (line.startsWith("data: ") && line !== "data: [DONE]") {
      buffer += line.slice(6);
      msgDiv.querySelector(".bubble").innerHTML = renderMarkdown(buffer);
    }
  }
}
```

**2. Save consultations to localStorage** — Persist history across page reloads:

```javascript
function saveSession() {
  localStorage.setItem("uniAdvisorHistory", JSON.stringify(history));
}

function loadSession() {
  const saved = localStorage.getItem("uniAdvisorHistory");
  if (saved) history = JSON.parse(saved);
}
```

**3. "Copy recommendation" button** — Add a copy button to each AI bubble so consultants can paste the result into an email or Word doc.

---

## Build Order Summary

| # | Stage | File | Effort | Depends On |
|---|---|---|---|---|
| 1 | Data Model | `backend/models.py` | Small | — |
| 2 | CSV Import | `backend/import_universities.py` | Small | Stage 1 |
| 3 | Web Scraper | `scraper/scraper.py` | Medium | Stage 1 |
| 4 | Hard Filter | `backend/main.py` | Small | Stage 1 |
| 5 | Pre-LLM Scorer | `backend/main.py` (new fn) | Medium | Stage 4 |
| 6 | Context Builder | `backend/main.py` | Small | Stage 5 |
| 7 | Claude Integration | `backend/main.py` | Medium | Stage 6 |
| 8 | Frontend | `frontend/index.html` | Medium | Stage 7 |

Start with Stage 1 → 2 → 4 to get a working end-to-end flow with existing data. Add Stage 3 when you need to expand or refresh the database. Add Stage 5 when you want better match quality. Add streaming (Stages 7 + 8) last as a UX polish step.
