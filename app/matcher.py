"""Reconciliation engine: matches card-statement LedgerEntry rows against
PayPal/Amazon EnrichmentRecord rows, and applies the exclusion rules
(P2P transfers, fees, cancelled/zero-amount rows) discovered during design.

The guiding rule throughout: the ledger amount (from the card statement)
is always the source of truth for the dollar amount. Enrichment records
only ever attach detail to an existing ledger line -- they never create a
new expense entry, which is how double-counting between the card
statement and PayPal/Amazon is avoided.
"""
from __future__ import annotations

import re
from datetime import timedelta

from .models import (
    LedgerEntry,
    EnrichmentRecord,
    EnrichmentSource,
    MatchResult,
    ReviewStatus,
)

# Amazon order numbers look like 113-1234567-1234567 / 114-1234567-1234567 etc.
ORDER_ID_PATTERN = re.compile(r"\b(\d{3}-\d{7}-\d{7})\b")

# "PAYPAL *ALIPAYUSINC 402-935-7733 CA" -> sub-merchant tag "ALIPAYUSINC"
PAYPAL_TAG_PATTERN = re.compile(r"PAYPAL\s*\*\s*([A-Z0-9]+)", re.IGNORECASE)

AMAZON_MARKETPLACE_PATTERN = re.compile(r"AMAZON\s*MKTPL|AMZN\s*MKTP", re.IGNORECASE)
AMAZON_NON_MARKETPLACE_PATTERN = re.compile(
    r"AMAZON\s*DIGIT|PRIME\s*VIDEO|AMZN\s*DIGITAL", re.IGNORECASE
)

P2P_PATTERNS = re.compile(r"VENMO\s*\*|ZELLE|CASH\s*APP", re.IGNORECASE)
FEE_PATTERN = re.compile(r"CASH EQUIVALENT.*TRANSACTION FEE", re.IGNORECASE)

DATE_MATCH_WINDOW_DAYS = 3


def _within_window(d1, d2, days: int = DATE_MATCH_WINDOW_DAYS) -> bool:
    return abs((d1 - d2).days) <= days


def match_bofa_paypal(
    bofa_entries: list[LedgerEntry], paypal_records: list[EnrichmentRecord]
) -> list[MatchResult]:
    """Matches BofA lines that look like PayPal charges against the PayPal
    export. Primary confirmation is amount + date window; the sub-merchant
    tag parsed from the BofA description is used as a bonus confidence
    signal when it happens to line up with the PayPal record's name, but
    is NOT required, since real-world testing showed PayPal's `Name` field
    doesn't always match the card's condensed sub-merchant tag.
    """
    usable_paypal = [r for r in paypal_records if r.is_usable_for_matching()]
    results: list[MatchResult] = []

    for entry in bofa_entries:
        tag_match = PAYPAL_TAG_PATTERN.search(entry.description)
        if not tag_match:
            continue  # not a PayPal-routed line, leave for direct categorization

        tag = tag_match.group(1).lower()
        candidates = [
            r
            for r in usable_paypal
            if abs(r.amount - abs(entry.amount)) < 0.01
            and _within_window(r.txn_date, entry.txn_date)
        ]

        if not candidates:
            results.append(
                MatchResult(
                    ledger_entry=entry,
                    enrichment_record=None,
                    confidence=0.0,
                    reason=(
                        f"BofA line tagged PAYPAL *{tag} but no PayPal export "
                        f"record matched on amount+date. Needs manual review "
                        f"(check PayPal export covers this date range)."
                    ),
                )
            )
            entry.status = ReviewStatus.NEEDS_REVIEW
            continue

        # Prefer a candidate whose name loosely contains the tag; otherwise
        # take the closest date match.
        best = next(
            (c for c in candidates if tag in c.merchant_or_item.lower().replace(" ", "")),
            None,
        )
        if best is None:
            best = min(candidates, key=lambda c: abs((c.txn_date - entry.txn_date).days))

        confidence = 0.95 if best.merchant_or_item.lower().find(tag) != -1 else 0.7
        entry.status = ReviewStatus.ENRICHED
        entry.matched_enrichment_id = best.record_id
        entry.category = None  # left for categorizer.py to fill in from best.merchant_or_item
        results.append(
            MatchResult(
                ledger_entry=entry,
                enrichment_record=best,
                confidence=confidence,
                reason=f"Matched on amount ${best.amount:.2f} + date within {DATE_MATCH_WINDOW_DAYS}d window.",
            )
        )

    return results


def match_chase_amazon(
    chase_entries: list[LedgerEntry], amazon_grouped: dict[str, list[EnrichmentRecord]]
) -> list[MatchResult]:
    """Matches Chase lines against Amazon order-history records using an
    exact Order ID match -- no fuzzy logic needed here since Chase's own
    statement surfaces the literal Amazon order number.
    """
    results: list[MatchResult] = []

    for entry in chase_entries:
        haystack = entry.description + " " + " ".join(str(v) for v in entry.raw_row.values())
        order_match = ORDER_ID_PATTERN.search(haystack)

        if AMAZON_NON_MARKETPLACE_PATTERN.search(entry.description):
            # Amazon-billed but not a marketplace order (Prime Video, digital
            # subscriptions, third-party billers like Emergent) -- no order
            # history lookup possible, categorize directly from description.
            entry.status = ReviewStatus.DIRECT
            results.append(
                MatchResult(
                    ledger_entry=entry,
                    enrichment_record=None,
                    confidence=1.0,
                    reason="Amazon-billed non-marketplace charge; categorize from description directly.",
                )
            )
            continue

        if not order_match:
            continue  # not an Amazon line at all, leave for direct categorization

        order_id = order_match.group(1)
        items = amazon_grouped.get(order_id)

        if not items:
            entry.status = ReviewStatus.NEEDS_REVIEW
            results.append(
                MatchResult(
                    ledger_entry=entry,
                    enrichment_record=None,
                    confidence=0.0,
                    reason=(
                        f"Chase line references Amazon order {order_id} but it "
                        f"wasn't found in the Amazon export. Needs manual review."
                    ),
                )
            )
            continue

        usable_items = [i for i in items if i.is_usable_for_matching()]
        if not usable_items:
            entry.status = ReviewStatus.NEEDS_REVIEW
            results.append(
                MatchResult(
                    ledger_entry=entry,
                    enrichment_record=None,
                    confidence=0.0,
                    reason=f"Amazon order {order_id} found but all rows were cancelled/zero-amount.",
                )
            )
            continue

        entry.status = ReviewStatus.ENRICHED
        entry.matched_enrichment_id = order_id
        # Combine item titles for a human-readable summary; the ledger
        # amount (entry.amount) remains the source of truth for the $ total,
        # never the sum of the (unreliable) per-item prices.
        item_summary = "; ".join(i.merchant_or_item for i in usable_items)
        results.append(
            MatchResult(
                ledger_entry=entry,
                enrichment_record=usable_items[0],
                confidence=1.0,
                reason=f"Exact Order ID match ({order_id}): {item_summary}",
            )
        )

    return results


def apply_exclusion_rules(entries: list[LedgerEntry]) -> None:
    """Mutates entries in place: flags P2P transfers and their paired fees
    so they're excluded from category-spend totals rather than
    miscategorized as a purchase.
    """
    sorted_entries = sorted(entries, key=lambda e: e.txn_date)

    for i, entry in enumerate(sorted_entries):
        if P2P_PATTERNS.search(entry.description):
            entry.status = ReviewStatus.TRANSFER
            entry.category = "Transfers (excluded)"
            continue

        if FEE_PATTERN.search(entry.description):
            entry.status = ReviewStatus.FEE
            entry.category = "Transfer fees (excluded)"
            # Try to find the P2P line it belongs to (same day, adjacent row)
            same_day_transfer = next(
                (
                    e
                    for e in sorted_entries
                    if e.status == ReviewStatus.TRANSFER and e.txn_date == entry.txn_date
                ),
                None,
            )
            if same_day_transfer is not None:
                entry.notes = f"Fee for transfer: {same_day_transfer.description}"


def reconcile(
    bofa_entries: list[LedgerEntry],
    chase_entries: list[LedgerEntry],
    macys_entries: list[LedgerEntry],
    paypal_records: list[EnrichmentRecord],
    amazon_records: list[EnrichmentRecord],
) -> list[MatchResult]:
    """Runs the full reconciliation pipeline across all three card ledgers.

    Returns MatchResults only for entries that went through PayPal/Amazon
    matching; BofA/Chase/Macy's lines that don't match a PayPal/Amazon/P2P
    pattern are left with status UNMATCHED for the categorizer to handle
    via direct rule-based categorization.
    """
    from .parsers import group_amazon_by_order  # local import to avoid cycle at module load

    all_entries = bofa_entries + chase_entries + macys_entries
    apply_exclusion_rules(all_entries)

    remaining_bofa = [e for e in bofa_entries if e.status == ReviewStatus.UNMATCHED]
    remaining_chase = [e for e in chase_entries if e.status == ReviewStatus.UNMATCHED]

    amazon_grouped = group_amazon_by_order(amazon_records)

    results = []
    results += match_bofa_paypal(remaining_bofa, paypal_records)
    results += match_chase_amazon(remaining_chase, amazon_grouped)
    return results
