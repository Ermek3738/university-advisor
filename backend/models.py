from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

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

    # Meta
    scrape_status = Column(String, default="pending")   # pending / success / failed
    last_scraped = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

engine = create_engine("sqlite:///./universities.db", connect_args={"check_same_thread": False})
Base.metadata.create_all(bind=engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
