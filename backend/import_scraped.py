"""
import_scraped.py
──────────────────
Read backend/scraped_export.csv and update matching universities (by name) in
the PRODUCTION database pointed to by DATABASE_URL.

Required env:
  DATABASE_URL  — Postgres connection string. Must be set; this script refuses
                  to run against SQLite to prevent accidentally overwriting
                  your local copy.

Usage (from backend/):
  python import_scraped.py
  python import_scraped.py --file scraped_export.csv
"""

import argparse
import csv
import os
import sys

from dotenv import load_dotenv

# Load .env from backend/ first, then repo root — same precedence as main.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, ".env"))
load_dotenv(os.path.join(_HERE, "..", ".env"))

if not os.environ.get("DATABASE_URL"):
    print(
        "ERROR: DATABASE_URL is not set. This script writes to your PRODUCTION "
        "database and refuses to fall back to local SQLite. Export DATABASE_URL "
        "or put it in .env, then re-run.",
        file=sys.stderr,
    )
    sys.exit(1)

# Safe to import now — models will pick up DATABASE_URL.
from models import University, SessionLocal  # noqa: E402

UPDATE_FIELDS = [
    "tuition_min",
    "tuition_max",
    "tuition_currency",
    "tuition_usd",
    "ielts_min",
    "toefl_min",
    "gpa_min",
    "programs",
    "intakes",
    "application_deadline",
    "scholarship_available",
    "notes",
    "scrape_status",
]

FLOAT_FIELDS = {"tuition_min", "tuition_max", "tuition_usd", "ielts_min", "gpa_min"}
INT_FIELDS = {"toefl_min"}


def _coerce(field: str, raw: str):
    """CSV gives us strings; convert numeric fields and treat empty as NULL."""
    if raw is None or raw == "":
        return None
    if field in FLOAT_FIELDS:
        try:
            return float(raw)
        except ValueError:
            return None
    if field in INT_FIELDS:
        try:
            return int(float(raw))
        except ValueError:
            return None
    return raw


def import_csv(filepath: str) -> tuple[int, int]:
    db = SessionLocal()
    updated = 0
    missing = 0

    try:
        with open(filepath, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = (row.get("name") or "").strip()
                if not name:
                    continue

                uni = db.query(University).filter_by(name=name).first()
                if uni is None:
                    print(f"  No match in production: {name}")
                    missing += 1
                    continue

                for field in UPDATE_FIELDS:
                    setattr(uni, field, _coerce(field, row.get(field)))
                updated += 1

        db.commit()
    finally:
        db.close()

    return updated, missing


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--file",
        default=os.path.join(_HERE, "scraped_export.csv"),
        help="CSV path (default: backend/scraped_export.csv)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"ERROR: CSV not found at {args.file}", file=sys.stderr)
        sys.exit(1)

    updated, missing = import_csv(args.file)
    print(f"Updated {updated} universities in production. "
          f"{missing} CSV rows had no matching name.")
