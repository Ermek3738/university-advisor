from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timezone
from typing import Optional
import os


def utcnow() -> datetime:
    # tz-aware UTC, then stripped to naive — matches the existing naive DateTime columns.
    return datetime.now(timezone.utc).replace(tzinfo=None)


EXCHANGE_RATES = {
    "USD": 1.00, "GBP": 1.27, "EUR": 1.08, "CAD": 0.74,
    "AUD": 0.65, "NZD": 0.60, "SGD": 0.74, "JPY": 0.0067,
    "CHF": 1.13, "SEK": 0.095, "NOK": 0.093, "DKK": 0.145,
}


def to_usd(amount, currency: Optional[str]) -> Optional[float]:
    if amount is None:
        return None
    rate = EXCHANGE_RATES.get((currency or "USD").upper().strip(), 1.0)
    try:
        return round(float(amount) * rate, 2)
    except (TypeError, ValueError):
        return None


Base = declarative_base()

class University(Base):
    __tablename__ = "universities"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    country = Column(String)
    city = Column(String)
    website = Column(String)

    # Scraped fields
    tuition_min = Column(Float, nullable=True)   # USD per year
    tuition_max = Column(Float, nullable=True)
    tuition_currency = Column(String, default="USD")

    ielts_min = Column(Float, nullable=True)      # e.g. 6.5
    toefl_min = Column(Integer, nullable=True)    # e.g. 80
    gpa_min = Column(Float, nullable=True)        # e.g. 3.0 out of 4.0

    programs = Column(Text, nullable=True)        # comma-separated list
    intakes = Column(String, nullable=True)       # e.g. "September, January"
    application_deadline = Column(String, nullable=True)

    scholarship_available = Column(String, nullable=True)  # "Yes / No / Partial"
    notes = Column(Text, nullable=True)           # any extra info from scrape

    tuition_usd = Column(Float, nullable=True)           # always USD, populated post-scrape

    # Meta
    scrape_status = Column(String, default="pending")   # pending / success / failed
    last_scraped = Column(DateTime, nullable=True)
    re_scrape_after = Column(DateTime, nullable=True)   # auto-requeue after this date
    created_at = Column(DateTime, default=utcnow)

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    # Supabase/Postgres: use direct connection (port 5432), not pooler
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
else:
    # Anchor the SQLite file to backend/ so it doesn't move with cwd
    # (otherwise running the scraper from scraper/ creates a second empty DB).
    _SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "universities.db")
    engine = create_engine(f"sqlite:///{_SQLITE_PATH}", connect_args={"check_same_thread": False})

Base.metadata.create_all(bind=engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
