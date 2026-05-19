"""Configuration constants for the AI-Sana backend."""

import os

# ── Filter configuration ────────────────────────────────────────
MIN_RESULTS = 5                    # minimum universities to return from filter
MAX_UNIVERSITIES_TO_CLAUDE = 30    # max universities to score and send to Claude
TOP_N_RECOMMENDATIONS = 15         # top N universities sent in context to Claude

# Drop order when results < MIN_RESULTS (softest → hardest). The order in this
# list mirrors the order clauses are appended in filter_universities().
FILTER_DROP_ORDER = [
    "programs",   # student might accept other programs
    "budget",     # student might increase budget
    "language",   # student might study abroad / improve IELTS
    "gpa",        # hardest to change, drop last
]

# ── Currency exchange rates (to USD) ────────────────────────────
EXCHANGE_RATES = {
    "USD": 1.00,
    "GBP": 1.27,
    "EUR": 1.08,
    "CAD": 0.74,
    "AUD": 0.65,
    "NZD": 0.60,
    "SGD": 0.74,
    "JPY": 0.0067,
    "CHF": 1.13,
    "SEK": 0.095,
    "NOK": 0.093,
    "DKK": 0.145,
}

# ── Scoring configuration ───────────────────────────────────────
SCORING_WEIGHTS = {
    "country_match":         3,
    "program_match":         2,
    "scholarship_yes":       2,
    "scholarship_partial":   1,
    "budget_headroom":       1,
    "gpa_headroom":          1,
}

BUDGET_HEADROOM_THRESHOLD = 0.2   # tuition must be 20%+ below budget
GPA_HEADROOM_THRESHOLD    = 0.5   # student must be 0.5+ above min GPA

# ── Claude configuration ────────────────────────────────────────
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
CLAUDE_MAX_TOKENS = 2000
