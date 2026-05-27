"""
fill_from_knowledge.py
──────────────────────
Fills missing university data using:
  1. Notion content text file (high accuracy for covered universities)
  2. Claude's training knowledge (for all remaining universities)

Usage:
  python fill_from_knowledge.py                    # run both passes
  python fill_from_knowledge.py --pass1-only       # only notion-sourced
  python fill_from_knowledge.py --pass2-only       # only knowledge-based
  python fill_from_knowledge.py --dry-run          # print what would be written, don't save
  python fill_from_knowledge.py --force            # overwrite even already-filled fields
  python fill_from_knowledge.py --country Germany  # only process universities in a country
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from typing import Any, Iterable, Optional

from dotenv import load_dotenv

# Force UTF-8 stdout so Unicode box-drawing / em-dashes render on Windows cp1252 consoles.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, ".env"))
load_dotenv(os.path.join(_HERE, "..", ".env"))

from anthropic import AsyncAnthropic

from models import SessionLocal, University, to_usd
from config import CLAUDE_MODEL

DEFAULT_NOTION_PATH = os.path.join(_HERE, "data", "notion_content.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("fill")

PASS1_SYSTEM = """You are extracting structured university data from a text document.

Given a university name and a reference text, extract ONLY information that is explicitly stated or clearly implied in the text for that specific university.

Return a JSON object with EXACTLY these keys (use null for anything not mentioned):
{
  "tuition_min": number or null,
  "tuition_max": number or null,
  "tuition_currency": string or null,
  "ielts_min": number or null,
  "toefl_min": number or null,
  "gpa_min": number or null,
  "programs": string or null,
  "intakes": string or null,
  "application_deadline": string or null,
  "scholarship_available": string or null,
  "notes": string or null
}

RULES:
- Only use information from the provided text. Do NOT add knowledge from elsewhere.
- If a range is given like "5.5-6.5", use 5.5 as tuition_min equivalent and 6.5 as max (or use the lower bound for ielts_min).
- For notes: write in English, focus on what makes this university valuable for international students.
- Return ONLY the JSON object. No markdown, no explanation."""

PASS2_SYSTEM = """You are a university data expert. For each university listed, provide accurate admissions data based on your training knowledge.

Return a JSON array where each element corresponds to one university in the input list, in the same order.

Each element must have EXACTLY these keys (use null if genuinely unknown):
{
  "name": string,
  "tuition_min": number or null,
  "tuition_max": number or null,
  "tuition_currency": string or null,
  "ielts_min": number or null,
  "toefl_min": number or null,
  "gpa_min": number or null,
  "programs": string or null,
  "intakes": string or null,
  "application_deadline": string or null,
  "scholarship_available": string or null,
  "notes": string or null
}

RULES:
- Provide your best estimate based on knowledge. Do NOT make up specific numbers you are uncertain about — use null instead.
- For universities accessed through pathway providers (Kaplan, Navitas, Shorelight, Study Group), provide the requirements for the pathway/foundation program, not the main university.
- tuition should be in the university's LOCAL currency (GBP for UK, CAD for Canada, AUD for Australia, etc.)
- Return ONLY the JSON array. No markdown fences, no explanation, no preamble."""


FIELD_MAP: dict[str, type] = {
    "tuition_min": float,
    "tuition_max": float,
    "tuition_currency": str,
    "ielts_min": float,
    "toefl_min": int,
    "gpa_min": float,
    "programs": str,
    "intakes": str,
    "application_deadline": str,
    "scholarship_available": str,
    "notes": str,
}


# ── JSON parsing helpers ─────────────────────────────────────────────────

def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # remove opening fence (possibly ```json)
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def parse_json_object(text: str) -> Optional[dict]:
    raw = _strip_code_fences(text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def parse_json_array(text: str) -> Optional[list]:
    raw = _strip_code_fences(text)
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            return None
    return None


# ── DB write logic ───────────────────────────────────────────────────────

def apply_extracted_data(uni: University, data: dict, force: bool = False) -> int:
    """Write extracted fields to university record.

    Only overwrites a field if force=True or the current value is None / empty.
    Always recomputes tuition_usd. Sets scrape_status="success" when at least
    3 fields were written. Returns count of fields written.
    """
    fields_written = 0

    for field, cast in FIELD_MAP.items():
        value = data.get(field)
        if value is None:
            continue
        current = getattr(uni, field, None)
        if not force and current not in (None, "", 0):
            continue
        try:
            setattr(uni, field, cast(value))
            fields_written += 1
        except (TypeError, ValueError):
            pass

    if uni.tuition_min and uni.tuition_currency:
        uni.tuition_usd = to_usd(uni.tuition_min, uni.tuition_currency)

    if fields_written >= 3:
        uni.scrape_status = "success"

    return fields_written


# ── Notion matching ──────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def find_notion_match(uni: University, notion_text: str) -> bool:
    """Return True if the university name (or a meaningful prefix) appears in the notion text."""
    if not uni.name:
        return False
    name = uni.name.strip()
    norm_notion = _normalize(notion_text)

    if _normalize(name) and _normalize(name) in norm_notion:
        return True

    # Strip parenthetical suffixes like "(UNNC)", "(INTO)", "(via Navitas)" and try again.
    stripped = re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()
    if stripped and stripped != name and _normalize(stripped) in norm_notion:
        return True

    # Try first 3-4 significant words for long names.
    words = stripped.split()
    if len(words) >= 3:
        prefix = " ".join(words[:4])
        if _normalize(prefix) and _normalize(prefix) in norm_notion and len(_normalize(prefix)) >= 8:
            return True

    return False


def is_already_complete(uni: University) -> bool:
    """A university is considered complete if its key fields are filled."""
    required = ["tuition_min", "ielts_min", "programs", "notes"]
    return all(getattr(uni, f, None) not in (None, "", 0) for f in required)


# ── Pass 1 ───────────────────────────────────────────────────────────────

async def run_pass1(
    client: AsyncAnthropic,
    notion_text: str,
    universities: list[University],
    *,
    dry_run: bool,
    force: bool,
    session,
) -> tuple[int, int, int]:
    print("\nPASS 1 — Filling from Notion content")
    print("─────────────────────────────────────")
    print(f"Found {len(universities)} universities in database")
    print(f"Notion text loaded: {len(notion_text):,} characters\n")
    print("Processing universities that match Notion content...")

    matched = [u for u in universities if find_notion_match(u, notion_text)]
    if not matched:
        print("  (no universities matched Notion content)")
        return 0, 0, 0

    updated = 0
    skipped = 0
    errors = 0

    for uni in matched:
        user_msg = (
            f"University name: {uni.name}\n"
            f"Country: {uni.country or 'Unknown'}\n\n"
            f"Reference text:\n{notion_text}"
        )
        try:
            resp = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=2000,
                system=PASS1_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(
                block.text for block in resp.content if getattr(block, "type", None) == "text"
            )
            data = parse_json_object(text)
            if data is None:
                logger.warning("Pass1 JSON parse failed for %s — raw: %s", uni.name, text[:300])
                errors += 1
                continue

            written = apply_extracted_data(uni, data, force=force)
            if written > 0:
                if dry_run:
                    print(f"  [dry-run] {uni.name:50s} → {written} fields would be written")
                else:
                    session.add(uni)
                    session.commit()
                    print(f"  ✓ {uni.name:50s} → {written} fields written")
                updated += 1
            else:
                skipped += 1
                print(f"  · {uni.name:50s} → no new fields to write")

        except Exception as exc:  # noqa: BLE001
            logger.error("Pass1 API error for %s: %s", uni.name, exc)
            errors += 1

        await asyncio.sleep(0.5)

    print(f"\nPass 1 complete: {updated} universities updated, {skipped} skipped, {errors} errors")
    return updated, skipped, errors


# ── Pass 2 ───────────────────────────────────────────────────────────────

def _chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


async def run_pass2(
    client: AsyncAnthropic,
    universities: list[University],
    *,
    batch_size: int,
    dry_run: bool,
    force: bool,
    session,
) -> tuple[int, int, int]:
    print("\nPASS 2 — Filling from Claude knowledge")
    print("───────────────────────────────────────")

    remaining = [u for u in universities if force or not is_already_complete(u)]
    print(f"{len(remaining)} universities still have missing fields")
    if not remaining:
        return 0, 0, 0

    print(f"Processing in batches of {batch_size}...\n")

    updated = 0
    skipped = 0
    errors = 0
    batches = list(_chunks(remaining, batch_size))

    for idx, batch in enumerate(batches, start=1):
        by_name: dict[str, University] = {}
        lines = []
        for i, u in enumerate(batch, start=1):
            location = f"{u.country or 'Unknown'}"
            if u.city:
                location += f", {u.city}"
            lines.append(f"{i}. {u.name} ({location})")
            by_name[u.name.strip().lower()] = u

        user_msg = f"Fill data for these {len(batch)} universities:\n\n" + "\n".join(lines)
        preview = ", ".join(u.name for u in batch[:3]) + ("..." if len(batch) > 3 else "")

        try:
            resp = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4000,
                system=PASS2_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(
                block.text for block in resp.content if getattr(block, "type", None) == "text"
            )
            arr = parse_json_array(text)
            if arr is None:
                logger.warning("Pass2 JSON parse failed (batch %d) — raw: %s", idx, text[:400])
                errors += len(batch)
                print(f"  Batch {idx}/{len(batches)}: {preview}  ✗ parse error")
                continue

            batch_updated = 0
            for i, item in enumerate(arr):
                if not isinstance(item, dict):
                    continue
                returned_name = (item.get("name") or "").strip().lower()
                uni = by_name.get(returned_name)
                if uni is None and i < len(batch):
                    uni = batch[i]
                if uni is None:
                    continue
                written = apply_extracted_data(uni, item, force=force)
                if written > 0:
                    if not dry_run:
                        session.add(uni)
                    batch_updated += 1
                else:
                    skipped += 1

            if not dry_run and batch_updated > 0:
                session.commit()

            updated += batch_updated
            marker = "✓" if batch_updated > 0 else "·"
            tag = "[dry-run] " if dry_run else ""
            print(
                f"  Batch {idx}/{len(batches)}: {preview}  {marker} "
                f"{tag}{batch_updated}/{len(batch)} updated"
            )

        except Exception as exc:  # noqa: BLE001
            logger.error("Pass2 API error (batch %d): %s", idx, exc)
            errors += len(batch)
            print(f"  Batch {idx}/{len(batches)}: {preview}  ✗ API error")

        await asyncio.sleep(0.5)

    print(f"\nPass 2 complete: {updated} universities updated, {skipped} skipped, {errors} errors")
    return updated, skipped, errors


# ── Entrypoint ───────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill university data from Notion + Claude knowledge.")
    parser.add_argument("--pass1-only", action="store_true", help="Only run Notion-sourced pass")
    parser.add_argument("--pass2-only", action="store_true", help="Only run knowledge-based pass")
    parser.add_argument("--dry-run", action="store_true", help="Print extracted data but don't save to DB")
    parser.add_argument("--force", action="store_true", help="Overwrite fields even if already filled")
    parser.add_argument("--country", type=str, help="Only process universities in this country")
    parser.add_argument("--batch-size", type=int, default=10, help="Universities per API call in pass 2 (default 10)")
    parser.add_argument("--notion-file", type=str, default=None, help="Path to notion content txt file")
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set in the environment.", file=sys.stderr)
        return 2

    client = AsyncAnthropic(api_key=api_key)

    notion_path = args.notion_file or DEFAULT_NOTION_PATH
    if not os.path.exists(notion_path):
        print(f"ERROR: Notion content file not found at {notion_path}", file=sys.stderr)
        return 2
    with open(notion_path, "r", encoding="utf-8") as f:
        notion_text = f.read()

    session = SessionLocal()
    try:
        query = session.query(University)
        if args.country:
            query = query.filter(University.country.ilike(args.country))
        universities = query.all()

        print("=== AI-Sana University Data Filler ===")
        if args.dry_run:
            print("(dry-run: no changes will be written to the database)")
        if args.force:
            print("(force: existing fields will be overwritten)")

        already_complete = sum(1 for u in universities if is_already_complete(u))

        p1_updated = p1_skipped = p1_errors = 0
        p2_updated = p2_skipped = p2_errors = 0

        run_p1 = not args.pass2_only
        run_p2 = not args.pass1_only

        if run_p1:
            p1_updated, p1_skipped, p1_errors = await run_pass1(
                client,
                notion_text,
                universities,
                dry_run=args.dry_run,
                force=args.force,
                session=session,
            )

        if run_p2:
            # Refresh from DB so Pass 2 sees Pass 1's writes.
            if not args.dry_run and run_p1:
                for u in universities:
                    session.refresh(u)
            p2_updated, p2_skipped, p2_errors = await run_pass2(
                client,
                universities,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
                force=args.force,
                session=session,
            )

        print("\n=== SUMMARY ===")
        print(f"Total universities: {len(universities)}")
        print(f"Updated in Pass 1: {p1_updated}")
        print(f"Updated in Pass 2: {p2_updated}")
        print(f"Already complete (skipped): {already_complete}")
        print(f"Errors: {p1_errors + p2_errors}")
        db_url = os.environ.get("DATABASE_URL", "sqlite:///backend/universities.db")
        print(f"Database: {db_url}")

        return 0
    finally:
        session.close()


def main() -> int:
    args = parse_args()
    if args.pass1_only and args.pass2_only:
        print("ERROR: --pass1-only and --pass2-only are mutually exclusive.", file=sys.stderr)
        return 2
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
