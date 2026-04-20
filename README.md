# UniAdvisor — Study Abroad Consultant Chatbot

AI-powered chatbot that helps consultants match students to the right universities from your partner database.

---

## 📁 Project Structure

```
university-advisor/
├── backend/
│   ├── main.py              ← FastAPI server (chat API)
│   ├── models.py            ← Database schema (SQLite)
│   └── import_universities.py ← Load your CSV into DB
├── scraper/
│   └── scraper.py           ← Crawl university websites
├── frontend/
│   └── index.html           ← Chat UI (open in browser)
├── data/
│   └── sample_universities.csv ← Sample data to test with
└── README.md
```

---

## 🚀 Setup (One-time)

### 1. Install dependencies
```bash
pip install fastapi uvicorn anthropic sqlalchemy crawl4ai
crawl4ai-setup   # run once after installing crawl4ai
```

### 2. Set your API key
```bash
# Mac/Linux
export ANTHROPIC_API_KEY=sk-ant-...

# Windows
set ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Load your university data
```bash
cd backend

# Test with sample data first:
python import_universities.py --file ../data/sample_universities.csv

# Later, replace with your real spreadsheet:
python import_universities.py --file your_universities.csv
```

Your CSV needs at minimum: `name`, `website`
Optional columns: `country`, `city`, `programs`, `ielts_min`, `toefl_min`, `gpa_min`, `tuition_min`, `tuition_max`, `intakes`, `scholarship_available`, `notes`

### 4. Run the backend
```bash
cd backend
uvicorn main:app --reload
# → API running at http://localhost:8000
```

### 5. Open the chat UI
Just open `frontend/index.html` in your browser. That's it!

---

## 🕷️ Scraping University Websites

After importing your university list, run the scraper to auto-fill missing data:

```bash
cd scraper

# Scrape all pending universities (newly imported)
python scraper.py

# Re-scrape everything (weekly refresh)
python scraper.py --all

# Scrape a specific university by ID
python scraper.py --id 5 12 23
```

The scraper uses `crawl4ai` with Claude to intelligently extract:
- Tuition fees
- IELTS/TOEFL requirements
- Minimum GPA
- Available programs
- Application deadlines
- Scholarship info

> ⚠️ Some websites block scrapers. For those, fill in the data manually in `universities.db` using a tool like [DB Browser for SQLite](https://sqlitebrowser.org/).

---

## 💬 How to Use the Chatbot

1. Fill in the **Student Profile** sidebar (GPA, IELTS, budget, etc.)
2. Click **"Use Profile in Chat →"** or type freely
3. The AI will return Top 3-5 matching universities with full details

**Example inputs:**
- *"Student with GPA 3.2, IELTS 6.5, budget $20k/year, wants Computer Science in Europe"*
- *"Which universities offer scholarships for Business students with low IELTS?"*
- *"Compare University of Edinburgh vs Maastricht for this student"*

---

## 📅 4-Week Development Plan

| Week | Task |
|------|------|
| **Week 1** | Import your full 300-400 uni list → Run scraper on all |
| **Week 2** | Test & fix bad scrapes manually → Tune Claude system prompt |
| **Week 3** | Add features: save consultations, export to PDF, user login |
| **Week 4** | Deploy to Railway/Render → Train your team |

---

## 🔧 Deployment (Railway)

```bash
# Install Railway CLI
npm install -g @railway/cli

# Deploy
railway login
railway init
railway up
```

Set env variable in Railway dashboard: `ANTHROPIC_API_KEY`

---

## 📌 Tips

- **Speed**: Pre-filtering means Claude only sees 10-20 universities per query → responses in ~2-3 seconds
- **Data quality**: The better your scraped data, the better the recommendations
- **Prompt tuning**: Edit `SYSTEM_PROMPT` in `main.py` to match your company's consulting style
- **Weekly refresh**: Set a cron job: `0 2 * * 1 python scraper.py --all`
