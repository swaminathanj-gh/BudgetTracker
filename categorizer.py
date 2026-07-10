"""Rule-based categorization with manual override.

Keeps things simple on purpose: a small, user-editable list of
(keyword -> category) rules, checked against either the enrichment
record's merchant/item text (when a match was found) or the ledger
entry's own description (for direct/uncategorized lines). Anything that
doesn't match a rule is left for manual categorization in the UI rather
than guessed at.
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import LedgerEntry, MatchResult, ReviewStatus

DEFAULT_RULES_PATH = Path(__file__).parent / "category_rules.json"

# Seed rules -- edit category_rules.json after first run to tune these to
# your own budget categories, this is just a starting point.
DEFAULT_RULES: dict[str, str] = {
    "costco": "Groceries",
    "grocery": "Groceries",
    "organics": "Groceries",
    "whole foods": "Groceries",
    "electric": "Utilities",
    "water": "Utilities",
    "internet": "Utilities",
    "gas #": "Auto/Gas",
    "apple.com": "Subscriptions",
    "prime video": "Entertainment",
    "amazon digit": "Subscriptions",
    "etsy": "Shopping/Misc",
    "macy": "Clothing",
    "airport": "Travel",
    "emergent": "Health/Supplements",
    "dexcom": "Health/Medical",
}


def load_rules(path: Path = DEFAULT_RULES_PATH) -> dict[str, str]:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return dict(DEFAULT_RULES)


def save_rules(rules: dict[str, str], path: Path = DEFAULT_RULES_PATH) -> None:
    with open(path, "w") as f:
        json.dump(rules, f, indent=2)


def categorize_text(text: str, rules: dict[str, str]) -> str | None:
    lowered = text.lower()
    for keyword, category in rules.items():
        if keyword.lower() in lowered:
            return category
    return None


def apply_categorization(
    match_results: list[MatchResult],
    all_entries: list[LedgerEntry],
    rules: dict[str, str] | None = None,
) -> None:
    """Mutates LedgerEntry.category in place.

    - For entries with a matched enrichment record, categorize from the
      enrichment record's merchant/item text (the real detail we went to
      the trouble of looking up).
    - For entries already marked DIRECT/TRANSFER/FEE, categorize (or
      leave the pre-set category) from the ledger description itself.
    - Anything left with no rule match stays uncategorized (None) and
      status NEEDS_REVIEW, surfaced in the UI for manual assignment.
    """
    rules = rules or load_rules()

    for result in match_results:
        entry = result.ledger_entry
        if result.enrichment_record is not None:
            category = categorize_text(result.enrichment_record.merchant_or_item, rules)
        else:
            category = categorize_text(entry.description, rules)

        if category:
            entry.category = category
        elif entry.status == ReviewStatus.ENRICHED or entry.status == ReviewStatus.DIRECT:
            entry.status = ReviewStatus.NEEDS_REVIEW

    # Also run direct categorization for any entry that never went through
    # the matcher at all (plain card charges with no PayPal/Amazon/P2P
    # pattern -- the majority of lines on any statement).
    for entry in all_entries:
        if entry.status == ReviewStatus.UNMATCHED:
            category = categorize_text(entry.description, rules)
            if category:
                entry.category = category
                entry.status = ReviewStatus.DIRECT
            else:
                entry.status = ReviewStatus.NEEDS_REVIEW
