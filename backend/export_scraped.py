"""
export_scraped.py
─────────────────
Export all successfully-scraped rows from the LOCAL SQLite database to a CSV
file you can then import into production (see import_scraped.py).

Reads directly from backend/universities.db via sqlite3 — does NOT honor
DATABASE_URL, so the export is always against your local copy regardless of
what's in .env or your shell.

Usage (from backend/):
  python export_scraped.py
  python export_scraped.py --out custom_path.csv
"""

import argparse
import csv
import os
import sqlite3
import sys

EXPORT_COLUMNS = [
    "name",
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


def export(db_path: str, out_path: str) -> int:
    if not os.path.exists(db_path):
        print(f"ERROR: SQLite DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = ", ".join(EXPORT_COLUMNS)
        cur = conn.execute(
            f"SELECT {cols} FROM universities WHERE scrape_status = 'success'"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row[c] for c in EXPORT_COLUMNS})

    return len(rows)


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default=os.path.join(here, "universities.db"),
        help="Path to the local SQLite DB (default: backend/universities.db)",
    )
    parser.add_argument(
        "--out",
        default=os.path.join(here, "scraped_export.csv"),
        help="Output CSV path (default: backend/scraped_export.csv)",
    )
    args = parser.parse_args()

    n = export(args.db, args.out)
    print(f"Exported {n} scraped universities -> {args.out}")
