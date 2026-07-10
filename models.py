"""Core data classes for BudgetTracker.

These are deliberately plain dataclasses (no ORM) so parsers/matcher/db
stay simple and testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class LedgerSource(str, Enum):
    BOFA = "bofa"
    CHASE = "chase"
    MACYS = "macys"


class EnrichmentSource(str, Enum):
    PAYPAL = "paypal"
    AMAZON = "amazon"


class ReviewStatus(str, Enum):
    """Where a ledger entry stands in the categorization pipeline."""
    UNMATCHED = "unmatched"          # not yet processed
    ENRICHED = "enriched"            # matched to PayPal/Amazon detail, categorized
    DIRECT = "direct"                # categorized straight from card description, no enrichment needed
    TRANSFER = "transfer"            # P2P transfer, excluded from spend totals
    FEE = "fee"                      # fee attached to a transfer
    NEEDS_REVIEW = "needs_review"    # failed match / data-quality gap / unrecognized merchant


@dataclass
class LedgerEntry:
    """One line from a BofA / Chase / Macy's card statement.

    This is the source of truth for the dollar amount and date of every
    real expense. Enrichment records only ever attach detail to an existing
    LedgerEntry -- they never create a new one.
    """
    source: LedgerSource
    txn_date: date
    description: str
    amount: float                       # positive = spend, negative = payment/credit
    raw_row: dict = field(default_factory=dict)

    # Filled in during matching/categorization:
    status: ReviewStatus = ReviewStatus.UNMATCHED
    category: Optional[str] = None
    matched_enrichment_id: Optional[str] = None  # transaction_id / order_id of matched enrichment record
    notes: Optional[str] = None

    # Stable identity for dedup / storage (source + date + amount + description hash)
    def dedup_key(self) -> str:
        return f"{self.source.value}|{self.txn_date.isoformat()}|{self.amount:.2f}|{self.description.strip().lower()}"


@dataclass
class EnrichmentRecord:
    """One record from a PayPal or Amazon export, used to add detail to a
    matching LedgerEntry.
    """
    source: EnrichmentSource
    record_id: str                      # PayPal Transaction ID or Amazon Order ID
    txn_date: date
    merchant_or_item: str
    amount: float
    funding_type: Optional[str] = None  # PayPal only: "General Card Deposit" vs "Bank Deposit to PP Account"
    status: Optional[str] = None        # Amazon only: "Delivered", "Cancelled", etc.
    raw_row: dict = field(default_factory=dict)

    def is_usable_for_matching(self) -> bool:
        """Filters out known data-quality gaps before they're ever handed
        to the matcher (cancelled orders, zero-amount rows, bank-funded
        PayPal transactions that won't appear on a card statement).
        """
        if self.source == EnrichmentSource.AMAZON:
            if self.status and self.status.strip().lower() == "cancelled":
                return False
            if self.amount == 0:
                return False
        if self.source == EnrichmentSource.PAYPAL:
            if self.funding_type and "bank deposit" in self.funding_type.strip().lower():
                return False
        return True


@dataclass
class Category:
    name: str
    budget_annual: float = 0.0
    parent: Optional[str] = None


@dataclass
class MatchResult:
    ledger_entry: LedgerEntry
    enrichment_record: Optional[EnrichmentRecord]
    confidence: float                   # 0.0-1.0, informational only
    reason: str                         # human-readable explanation, shown in UI for transparency
