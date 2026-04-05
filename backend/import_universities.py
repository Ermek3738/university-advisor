"""
import_universities.py
─────────────────────
Run this ONCE to load your Excel/CSV spreadsheet into the SQLite database.

Expected CSV columns (rename your spreadsheet headers to match):
  name, country, city, website, programs, notes

Any missing column is safely ignored.

Usage:
  python import_universities.py --file universities.csv
"""

import csv
import argparse
from models import University, SessionLocal

REQUIRED = ["name", "website"]

def import_csv(filepath: str):
    db = SessionLocal()
    added = 0
    skipped = 0

    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # Normalize headers (strip spaces, lowercase)
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]

        for row in reader:
            row = {k.strip().lower(): (v.strip() if v else "") for k, v in row.items() if k is not None}

            if not row.get("name") or not row.get("website"):
                print(f"  ⚠️  Skipping row — missing name or website: {row}")
                skipped += 1
                continue

            # Check for duplicates
            exists = db.query(University).filter_by(website=row["website"]).first()
            if exists:
                skipped += 1
                continue

            uni = University(
                name=row.get("name"),
                country=row.get("country", ""),
                city=row.get("city", ""),
                website=row.get("website"),
                programs=row.get("programs", ""),
                notes=row.get("notes", ""),
                # If you already have some data filled in the sheet, map here:
                ielts_min=float(row["ielts_min"]) if row.get("ielts_min") else None,
                toefl_min=int(row["toefl_min"]) if row.get("toefl_min") else None,
                gpa_min=float(row["gpa_min"]) if row.get("gpa_min") else None,
                tuition_min=float(row["tuition_min"]) if row.get("tuition_min") else None,
                tuition_max=float(row["tuition_max"]) if row.get("tuition_max") else None,
                intakes=row.get("intakes", ""),
                scholarship_available=row.get("scholarship_available", ""),
            )
            db.add(uni)
            added += 1

    db.commit()
    db.close()
    print(f"\n✅ Done! Added: {added} | Skipped/duplicates: {skipped}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Path to your CSV file")
    args = parser.parse_args()
    import_csv(args.file)
