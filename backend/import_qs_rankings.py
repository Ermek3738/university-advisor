"""
import_qs_rankings.py
─────────────────────
One-shot importer that loads the QS 2026 rankings spreadsheets into the
database (Supabase in production, SQLite locally — whichever ``models.py``
resolves from ``DATABASE_URL``):

  • 2026 QS World University Rankings.xlsx          → University.qs_ranking(_display)
  • 2026 QS World University Rankings by Subject.xlsx → UniversitySubjectRanking rows

Run once, manually:

    python backend/import_qs_rankings.py

Name matching is the critical part — DB partner names carry provider suffixes
like "(INTO)" / "(Navitas)" that QS never uses, so we strip a trailing
parenthetical, normalise both sides, then try exact → substring → fuzzy
(difflib @ 0.82). Every world-ranking match is logged with both names so a
human can eyeball the mapping.
"""

import os
import re
import sys
import difflib

# Windows consoles default to cp1252 and choke on the ✅/❌ log emojis.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# Mirror main.py's env loading so a manual run targets the same DB (Supabase).
from dotenv import load_dotenv
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))
load_dotenv(os.path.join(_BACKEND_DIR, "..", ".env"))

import openpyxl

from models import (
    University,
    UniversitySubjectRanking,
    SessionLocal,
    utcnow,
)

# ── File locations ─────────────────────────────────────────────────────────────
DATA_DIR = os.path.join(_BACKEND_DIR, "data")
WORLD_FILE = os.path.join(DATA_DIR, "2026 QS World University Rankings.xlsx")
SUBJECT_FILE = os.path.join(DATA_DIR, "2026 QS World University Rankings by Subject.xlsx")

# Subject sheets to skip entirely.
SKIP_SHEETS = {"Index", "methodology"}

# Truncated sheet name → clean subject name (per spec). For every other sheet we
# fall back to the full subject name embedded at row index 1 / col 0, then the
# raw sheet name.
SUBJECT_NAME_FIXES = {
    "Computer Science & Information ": "Computer Science & Information Systems",
    "Architecture _ Built Environmen": "Architecture & Built Environment",
    "Engineering - Civil & Structura": "Engineering - Civil & Structural",
    "Engineering - Electrical & Elec": "Engineering - Electrical & Electronic",
    "Engineering - Mechanical, Aeron": "Engineering - Mechanical, Aeronautical",
    "Theology, Divinity & Religious ": "Theology, Divinity & Religious Studies",
    "Hospitality & Leisure Managemen": "Hospitality & Leisure Management",
    "Library & Information Managemen": "Library & Information Management",
    "Data Science and Artificial Int": "Data Science and Artificial Intelligence",
    "Politics & International Studie": "Politics & International Studies",
    "Statistics & Operational Resear": "Statistics & Operational Research",
    "History_Subject": "History",
}

# ── Name normalisation ─────────────────────────────────────────────────────────
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def strip_provider(name: str) -> str:
    """Drop a trailing parenthetical (provider suffix) from a DB name:
    'University of Manchester (INTO)' → 'University of Manchester'.
    Applied repeatedly so 'X (Navitas) (UP Education)' collapses fully."""
    if not name:
        return ""
    prev = None
    out = name
    while out != prev:
        prev = out
        out = _TRAILING_PAREN_RE.sub("", out).strip()
    return out


def clean(name: str) -> str:
    """Lowercase, strip punctuation→space, collapse whitespace, drop a leading
    'the '. Punctuation removal makes 'University of Massachusetts, Amherst' and
    'University of Massachusetts Amherst' compare equal."""
    if not name:
        return ""
    s = str(name).lower()
    s = re.sub(r"[^\w\s]", " ", s)      # punctuation/dashes/commas → space
    s = re.sub(r"\s+", " ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return s


def parse_rank(rank_display) -> "int | None":
    """'312' → 312, '=400' → 400, '801-850' → 801, '1401+' → 1401."""
    if rank_display is None:
        return None
    s = str(rank_display).strip().lstrip("=").strip()
    if not s:
        return None
    if s.endswith("+"):
        s = s[:-1]
    if "-" in s:
        s = s.split("-", 1)[0]
    s = s.strip()
    return int(s) if s.isdigit() else None


def display_rank(rank_raw) -> "str | None":
    """Raw stored display, minus the QS '=' tie marker: 35 → '35',
    '=696' → '696', '801-850' → '801-850', '1401+' → '1401+'."""
    if rank_raw is None:
        return None
    if isinstance(rank_raw, float) and rank_raw.is_integer():
        rank_raw = int(rank_raw)
    s = str(rank_raw).strip().lstrip("=").strip()
    return s or None


# Generic connector tokens that never distinguish one institution from another.
_GENERIC_TOKENS = {
    "university", "of", "the", "and", "for", "at", "in", "a", "an", "&",
}

# Location / campus qualifiers. A branch or alternate listing of the SAME
# institution differs from its parent ONLY by these ("Heriot-Watt University
# Dubai" vs "Heriot-Watt University", "University at Buffalo SUNY" vs "University
# at Buffalo"), so a difference made up purely of these is treated as a match.
# Deliberately narrow — it EXCLUDES overloaded words that form part of a
# distinct institution's identity ("international" in "Tokyo International
# University", "city" in "Oklahoma City University", "york"/"england"/"new",
# "birmingham", "chicago", "art"/"design"). Erring toward exclusion: a missed
# branch is far safer than a wrong famous-university rank.
_LOCATION_TOKENS = {
    "uk", "dubai", "abu", "dhabi", "malaysia", "singapore", "qatar", "uae",
    "emirates", "germany", "australia", "kazakhstan", "azerbaijan",
    "switzerland", "china", "india", "hong", "kong", "leipzig",
    "london", "newcastle", "suny", "boca", "raton", "campus", "online",
}


def _significant_tokens(s: str) -> set:
    """Distinctive tokens — drops generic connectors only (keeps locations)."""
    return {t for t in s.split() if t not in _GENERIC_TOKENS}


def resolve(db_clean: str, qs_names: list, qs_set: set):
    """Return (matched_cleaned_qs_name, method) or (None, None).

    exact → distinctive-token match → difflib @ 0.82 (guarded).

    Raw substring containment is intentionally NOT used: it lets a distinct
    institution grab a famous one's rank just by containing its name ("Kyoto
    University of Art and Design" → "Kyoto University", "York University" → "New
    York University"). Instead a non-exact match requires the distinctive token
    sets to be EQUAL, or to differ by location/campus qualifiers only — which
    still captures branch campuses ("...University Dubai") and short forms
    ("Royal Holloway" → "Royal Holloway University of London")."""
    if not db_clean:
        return None, None
    # 1. exact
    if db_clean in qs_set:
        return db_clean, "exact"
    # 2. distinctive-token match: sets equal, or one a subset of the other with
    #    the difference made up entirely of location/campus qualifiers.
    #    Closest-length candidate wins on ties.
    db_sig = _significant_tokens(db_clean)
    if db_sig:
        best = None
        best_gap = None
        for qs in qs_names:
            qs_sig = _significant_tokens(qs)
            if not qs_sig:
                continue
            if (db_sig <= qs_sig or qs_sig <= db_sig) and (db_sig ^ qs_sig) <= _LOCATION_TOKENS:
                gap = abs(len(qs) - len(db_clean))
                if best_gap is None or gap < best_gap:
                    best, best_gap = qs, gap
        if best is not None:
            return best, "core"
    # 3. fuzzy — propose via difflib @ 0.82, but only TRUST a candidate when its
    #    distinctive (non-generic) token SET equals the DB name's. This accepts
    #    connector/word-order/typo variants ("Brunel University London" ↔
    #    "Brunel University of London") while rejecting same-family-but-distinct
    #    institutions that merely share one token ("...lethbridge" ↔
    #    "...cambridge", "Texas State" ↔ "Utah State").
    db_sig = _significant_tokens(db_clean)
    if db_sig:
        for cand in difflib.get_close_matches(db_clean, qs_names, n=5, cutoff=0.82):
            if _significant_tokens(cand) == db_sig:
                return cand, "fuzzy"
    return None, None


# ── World rankings ─────────────────────────────────────────────────────────────
def load_world_lookup() -> dict:
    """{cleaned_name: {'display', 'numeric', 'orig'}} from the world file.
    Header at row index 2, data from index 3; cols: Index, Rank, Name, Country."""
    wb = openpyxl.load_workbook(WORLD_FILE, read_only=True, data_only=True)
    ws = wb.active
    lookup: dict[str, dict] = {}
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i < 3:
            continue
        rank_raw, name = row[1], row[2]
        if not name:
            continue
        # Strip QS-side trailing acronyms too ("...Berkeley (UCB)" → "...Berkeley").
        key = clean(strip_provider(name))
        if not key:
            continue
        numeric = parse_rank(rank_raw)
        entry = {"display": display_rank(rank_raw), "numeric": numeric, "orig": str(name).strip()}
        # On duplicate cleaned names keep the better (smaller) numeric rank.
        prev = lookup.get(key)
        if prev is None or (numeric is not None and (prev["numeric"] is None or numeric < prev["numeric"])):
            lookup[key] = entry
    wb.close()
    return lookup


# ── Subject rankings ───────────────────────────────────────────────────────────
def subject_name_for(sheet_name: str, full_embedded) -> str:
    if sheet_name in SUBJECT_NAME_FIXES:
        return SUBJECT_NAME_FIXES[sheet_name]
    if full_embedded and str(full_embedded).strip():
        return str(full_embedded).strip()
    return sheet_name.strip()


def load_subject_lookups() -> dict:
    """{subject_name: {cleaned_inst_name: {'display', 'numeric', 'score', 'orig'}}}."""
    wb = openpyxl.load_workbook(SUBJECT_FILE, read_only=True, data_only=True)
    subjects: dict[str, dict] = {}
    for sheet_name in wb.sheetnames:
        if sheet_name in SKIP_SHEETS:
            continue
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 5:
            continue
        full_embedded = rows[1][0] if len(rows) > 1 else None
        subject = subject_name_for(sheet_name, full_embedded)

        # Find the header row (col 0 == '2026'); data follows it.
        header_idx = None
        for i, r in enumerate(rows[:8]):
            if r and str(r[0]).strip() == "2026":
                header_idx = i
                break
        if header_idx is None:
            continue
        header = rows[header_idx]
        # Locate the SCORE column (last 'SCORE' header).
        score_col = None
        for j, h in enumerate(header):
            if h and str(h).strip().upper() == "SCORE":
                score_col = j
        if score_col is None:
            score_col = len(header) - 1

        lookup: dict[str, dict] = {}
        for r in rows[header_idx + 1:]:
            if not r or len(r) < 2:
                continue
            inst = r[1]
            if not inst or not str(inst).strip():
                continue
            rank_raw = r[0]
            score_raw = r[score_col] if score_col < len(r) else None
            try:
                score = float(score_raw) if score_raw not in (None, "", "-") else None
            except (TypeError, ValueError):
                score = None
            key = clean(strip_provider(inst))
            if not key:
                continue
            numeric = parse_rank(rank_raw)
            entry = {
                "display": display_rank(rank_raw),
                "numeric": numeric,
                "score": score,
                "orig": str(inst).strip(),
            }
            prev = lookup.get(key)
            if prev is None or (numeric is not None and (prev["numeric"] is None or numeric < prev["numeric"])):
                lookup[key] = entry
        subjects[subject] = lookup
    wb.close()
    return subjects


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("Loading spreadsheets…")
    world = load_world_lookup()
    subjects = load_subject_lookups()
    print(f"  World rankings: {len(world)} institutions")
    print(f"  Subject sheets: {len(subjects)} subjects")

    # Build the global candidate pool used to resolve each DB university ONCE.
    global_set: set[str] = set(world.keys())
    for subj_lookup in subjects.values():
        global_set.update(subj_lookup.keys())
    global_names = sorted(global_set)

    db = SessionLocal()
    try:
        unis = db.query(University).all()

        # ── World rankings ──────────────────────────────────────────────────────
        print("\n=== Matching WORLD rankings ===")
        world_names = sorted(world.keys())
        world_set = set(world.keys())
        # uni.id → resolved cleaned canonical name (from the global pool), reused
        # for the subject pass so fuzzy matching runs once per university.
        canonical: dict[int, str] = {}
        world_updated = 0
        world_cleared = 0
        not_matched = []
        for uni in unis:
            db_clean = clean(strip_provider(uni.name))
            # Resolve against the world set first (authoritative for world rank)…
            match, _ = resolve(db_clean, world_names, world_set)
            if match is None:
                # …then against the global pool so subjects can still match a
                # uni that has no world ranking.
                gmatch, _ = resolve(db_clean, global_names, global_set)
                canonical[uni.id] = gmatch or db_clean
                not_matched.append(uni.name)
                # Clear any stale ranking left by the legacy live-feed scraper so
                # the chatbot never shows an old number mislabeled as "2026".
                # Stamp updated_at fresh so the startup auto-scraper treats every
                # row as current and stays dormant, keeping QS-2026 authoritative.
                if uni.qs_ranking is not None or uni.qs_ranking_display is not None:
                    world_cleared += 1
                uni.qs_ranking = None
                uni.qs_ranking_display = None
                uni.qs_ranking_updated_at = utcnow()
                print(f'❌ No match: "{uni.name}"')
                continue

            canonical[uni.id] = match
            entry = world[match]
            uni.qs_ranking = entry["numeric"]
            uni.qs_ranking_display = entry["display"]
            uni.qs_ranking_updated_at = utcnow()
            world_updated += 1
            print(f'✅ "{uni.name}" → "{entry["orig"]}" (QS #{entry["display"]})')

        db.commit()

        # ── Subject rankings ────────────────────────────────────────────────────
        print("\n=== Matching SUBJECT rankings ===")
        total_subject_entries = 0
        unis_with_subject: set[int] = set()
        for subject, lookup in subjects.items():
            qs_names = sorted(lookup.keys())
            qs_set = set(lookup.keys())
            subject_matches = 0
            for uni in unis:
                db_clean = clean(strip_provider(uni.name))
                # Fast path: exact on canonical or db_clean; fall back to a fresh
                # resolve within this subject only if both miss.
                entry = lookup.get(canonical.get(uni.id, "")) or lookup.get(db_clean)
                if entry is None:
                    m, _ = resolve(db_clean, qs_names, qs_set)
                    entry = lookup.get(m) if m else None
                if entry is None:
                    continue

                # Replace any existing rows for this university+subject pair.
                db.query(UniversitySubjectRanking).filter(
                    UniversitySubjectRanking.university_id == uni.id,
                    UniversitySubjectRanking.subject == subject,
                ).delete(synchronize_session=False)

                db.add(UniversitySubjectRanking(
                    university_id=uni.id,
                    subject=subject,
                    rank_display=entry["display"],
                    rank_numeric=entry["numeric"],
                    score=entry["score"],
                    updated_at=utcnow(),
                ))
                total_subject_entries += 1
                unis_with_subject.add(uni.id)
                subject_matches += 1
            if subject_matches:
                print(f"  {subject}: {subject_matches} universities")

        db.commit()
    finally:
        db.close()

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n=== WORLD RANKINGS ===")
    print(f"Updated: {world_updated} universities")
    print(f"Not matched: {len(not_matched)} universities (listed above)")
    print(f"Cleared stale rankings on: {world_cleared} of the unmatched universities")

    print("\n=== SUBJECT RANKINGS ===")
    print(f"Total subject entries inserted: {total_subject_entries:,}")
    print(f"Universities with at least 1 subject ranking: {len(unis_with_subject)}")


if __name__ == "__main__":
    main()
