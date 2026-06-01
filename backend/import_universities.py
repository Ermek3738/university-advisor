"""
import_universities.py
─────────────────────
Load or refresh your partner university spreadsheet into the SQLite database.

Expected CSV columns (rename your spreadsheet headers to match):
  name, country, city, website, programs, notes,
  ielts_min, toefl_min, gpa_min, tuition_min, tuition_max,
  intakes, scholarship_available

Any missing column is safely ignored.

Usage:
  python import_universities.py --file universities.csv           # insert new rows only
  python import_universities.py --file universities.csv --update  # also overwrite existing rows
"""

import csv
import argparse
from models import University, SessionLocal, to_usd

REQUIRED = ["name", "website"]

def parse_row(row: dict) -> dict:
    """Extract and type-cast all known fields from a CSV row."""
    tuition_min = float(row["tuition_min"]) if row.get("tuition_min") else None
    tuition_max = float(row["tuition_max"]) if row.get("tuition_max") else None
    tuition_currency = (row.get("tuition_currency") or "USD").upper().strip() or "USD"

    return {
        "name":                  row.get("name"),
        "country":               row.get("country", ""),
        "city":                  row.get("city", ""),
        "website":               row.get("website"),
        "programs":              row.get("programs", ""),
        "notes":                 row.get("notes", ""),
        "ielts_min":             float(row["ielts_min"]) if row.get("ielts_min") else None,
        "toefl_min":             int(row["toefl_min"])   if row.get("toefl_min") else None,
        "gpa_min":               float(row["gpa_min"])   if row.get("gpa_min")   else None,
        "tuition_min":           tuition_min,
        "tuition_max":           tuition_max,
        "tuition_currency":      tuition_currency,
        "tuition_usd":           to_usd(tuition_min, tuition_currency),
        "intakes":               row.get("intakes", ""),
        "scholarship_available": row.get("scholarship_available", ""),
    }

def apply_fields(uni: University, fields: dict):
    """Write parsed fields onto a University instance."""
    for key, value in fields.items():
        setattr(uni, key, value)

def import_csv(filepath: str, update: bool = False) -> dict:
    db = SessionLocal()
    added = 0
    updated = 0
    skipped = 0
    errors: list[str] = []

    try:
        with open(filepath, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                errors.append("CSV is empty or has no header row")
                return {"added": 0, "updated": 0, "skipped": 0, "errors": errors}
            reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]

            # row_num: header is line 1, first data row is line 2
            for row_num, raw_row in enumerate(reader, start=2):
                row = {
                    k.strip().lower(): (v.strip() if v else "")
                    for k, v in raw_row.items()
                    if k is not None
                }

                if not row.get("name") or not row.get("website"):
                    print(f"  ⚠️  Skipping row — missing name or website: {row}")
                    skipped += 1
                    errors.append(f"Row {row_num}: missing name or website")
                    continue

                try:
                    fields = parse_row(row)
                except (ValueError, TypeError) as exc:
                    print(f"  ⚠️  Skipping row — parse error: {row.get('name','?')}: {exc}")
                    skipped += 1
                    errors.append(f"Row {row_num} ({row.get('name', '?')}): {exc}")
                    continue

                exists = db.query(University).filter_by(name=row["name"]).first()

                if exists:
                    if update:
                        apply_fields(exists, fields)
                        # Reset scrape status so the scraper refreshes this record
                        exists.scrape_status = "pending"
                        updated += 1
                        print(f"  🔄 Updated: {row['name']}")
                    else:
                        skipped += 1
                    continue

                uni = University()
                apply_fields(uni, fields)
                db.add(uni)
                added += 1

        db.commit()
    finally:
        db.close()

    print(f"\n✅ Done! Added: {added} | Updated: {updated} | Skipped/duplicates: {skipped}")
    return {"added": added, "updated": updated, "skipped": skipped, "errors": errors}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Path to your CSV file")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Overwrite existing records instead of skipping them",
    )
    args = parser.parse_args()
    import_csv(args.file, update=args.update)
