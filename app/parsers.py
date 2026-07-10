"""CSV parsers for all five BudgetTracker input sources.

Card statement parsers (BofA/Chase/Macy's) use flexible column-name
detection since exported header text varies slightly by bank and by
export format (some banks export "Posted Date" vs "Date", etc.).

The PayPal and Amazon parsers are written against the *actual* column
headers confirmed from real exports during design/testing:

  Amazon order-history export columns:
    Order ID, Order Date, Total Amount, Currency, Total Savings, Status,
    Item Title, Item ASIN, Item Quantity, Item Price, Item Discount,
    Promotions, Item URL, Details URL

  PayPal transaction export columns:
    Date, Time, TimeZone, Name, Type, Status, Currency, Amount, Fees,
    Total, Exchange Rate, Receipt ID, Balance, Transaction ID, Item Title
"""
from __future__ import annotations

import csv
from datetime import datetime, date
from pathlib import Path
from typing import Iterable

from .models import LedgerEntry, LedgerSource, EnrichmentRecord, EnrichmentSource

# ---------------------------------------------------------------------------
# Generic card-statement parsing (BofA / Chase / Macy's)
# ---------------------------------------------------------------------------

# If your exported file uses different header text than these, add it here
# rather than editing the parsing logic below.
COLUMN_ALIASES = {
    "date": ["date", "posted date", "transaction date"],
    "description": ["description", "payee", "merchant name or transaction description"],
    "amount": ["amount", "transaction amount"],
}

_DATE_FORMATS = ["%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"]


def _parse_date(value: str) -> date:
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {value!r}")


def _find_column(header: list[str], aliases: list[str]) -> str:
    lowered = {h.strip().lower(): h for h in header}
    for alias in aliases:
        if alias in lowered:
            return lowered[alias]
    raise KeyError(
        f"Could not find any of {aliases} in CSV header {header}. "
        f"Add your bank's exact column name to COLUMN_ALIASES in parsers.py."
    )


def parse_card_statement(path: str | Path, source: LedgerSource) -> list[LedgerEntry]:
    """Parses a generic bank/card CSV export into LedgerEntry rows.

    Works for BofA, Chase, and Macy's exports as long as the file has some
    recognizable Date/Description/Amount columns -- exact header text can
    vary, see COLUMN_ALIASES above.
    """
    entries: list[LedgerEntry] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        date_col = _find_column(header, COLUMN_ALIASES["date"])
        desc_col = _find_column(header, COLUMN_ALIASES["description"])
        amount_col = _find_column(header, COLUMN_ALIASES["amount"])

        for row in reader:
            raw_amount = row[amount_col].replace(",", "").replace("$", "").strip()
            if not raw_amount:
                continue
            entries.append(
                LedgerEntry(
                    source=source,
                    txn_date=_parse_date(row[date_col]),
                    description=row[desc_col].strip(),
                    amount=float(raw_amount),
                    raw_row=row,
                )
            )
    return entries


def parse_bofa(path: str | Path) -> list[LedgerEntry]:
    return parse_card_statement(path, LedgerSource.BOFA)


def parse_chase(path: str | Path) -> list[LedgerEntry]:
    return parse_card_statement(path, LedgerSource.CHASE)


def parse_macys(path: str | Path) -> list[LedgerEntry]:
    return parse_card_statement(path, LedgerSource.MACYS)


# ---------------------------------------------------------------------------
# Amazon order-history export
# ---------------------------------------------------------------------------

def parse_amazon_export(path: str | Path) -> list[EnrichmentRecord]:
    """Parses the "Order History Exporter for Amazon" CSV.

    One row per item, not per order. Multiple rows can share the same
    Order ID (multi-item orders) -- callers should group by record_id
    when they need order-level totals, since Item Price is unreliable
    (frequently 0) but Total Amount is repeated correctly on every row
    belonging to the same order.
    """
    records: list[EnrichmentRecord] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_amount_raw = (row.get("Total Amount") or "0").strip()
            try:
                amount = float(total_amount_raw)
            except ValueError:
                amount = 0.0

            order_date_raw = (row.get("Order Date") or "").strip()
            try:
                txn_date = _parse_date(order_date_raw)
            except ValueError:
                # Amazon export uses ISO format (YYYY-MM-DD) which _parse_date
                # already handles; if it still fails, skip rather than crash
                # the whole import over one bad row.
                continue

            records.append(
                EnrichmentRecord(
                    source=EnrichmentSource.AMAZON,
                    record_id=(row.get("Order ID") or "").strip(),
                    txn_date=txn_date,
                    merchant_or_item=(row.get("Item Title") or "").strip(),
                    amount=amount,
                    status=(row.get("Status") or "").strip(),
                    raw_row=row,
                )
            )
    return records


def group_amazon_by_order(records: Iterable[EnrichmentRecord]) -> dict[str, list[EnrichmentRecord]]:
    """Groups Amazon export rows by Order ID, since one order = many rows."""
    grouped: dict[str, list[EnrichmentRecord]] = {}
    for r in records:
        grouped.setdefault(r.record_id, []).append(r)
    return grouped


# ---------------------------------------------------------------------------
# PayPal transaction export
# ---------------------------------------------------------------------------

def parse_paypal_export(path: str | Path) -> list[EnrichmentRecord]:
    """Parses a PayPal "Activity Download Report" CSV.

    Every real payment appears as a *pair* of rows sharing the exact same
    Date+Time: one negative row with the real merchant Name, and one
    positive "funding" row (Type "General Card Deposit" if funded by the
    linked card, or "Bank Deposit to PP Account" if funded from a linked
    bank account instead -- the latter won't show up on the card statement
    and is filtered out by EnrichmentRecord.is_usable_for_matching()).

    Returns one EnrichmentRecord per *pair*, using the merchant row's Name
    and the funding row's Type/amount, so downstream matching logic only
    ever sees one record per real transaction rather than two raw rows.
    """
    raw_rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_rows.append(row)

    # Group by identical Date+Time -- that's how a payment row and its
    # funding row are linked in this export format.
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in raw_rows:
        key = (row.get("Date", "").strip(), row.get("Time", "").strip())
        groups.setdefault(key, []).append(row)

    records: list[EnrichmentRecord] = []
    for (date_str, _time_str), rows in groups.items():
        if not date_str:
            continue
        merchant_row = next((r for r in rows if float(r.get("Amount", 0) or 0) < 0), None)
        funding_row = next((r for r in rows if float(r.get("Amount", 0) or 0) > 0), None)

        if merchant_row is None:
            continue

        amount = abs(float(merchant_row.get("Amount", 0) or 0))
        funding_type = funding_row.get("Type") if funding_row else None

        records.append(
            EnrichmentRecord(
                source=EnrichmentSource.PAYPAL,
                record_id=(merchant_row.get("Transaction ID") or "").strip(),
                txn_date=_parse_date(date_str),
                merchant_or_item=(merchant_row.get("Name") or merchant_row.get("Type") or "").strip(),
                amount=amount,
                funding_type=(funding_type or "").strip(),
                raw_row=merchant_row,
            )
        )
    return records
