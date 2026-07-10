"""SQLite storage for BudgetTracker.

A personal, single-user desktop app doesn't need a server database --
SQLite gives durable local storage with zero setup. Dedup is handled via
LedgerEntry.dedup_key() so re-importing the same statement twice (e.g. you
export overlapping date ranges each quarter) doesn't create duplicate rows.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import LedgerEntry, LedgerSource, ReviewStatus

DEFAULT_DB_PATH = Path.home() / "BudgetTracker" / "budgettracker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_entries (
    dedup_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    txn_date TEXT NOT NULL,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL,
    category TEXT,
    matched_enrichment_id TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_ledger_date ON ledger_entries(txn_date);
CREATE INDEX IF NOT EXISTS idx_ledger_category ON ledger_entries(category);
"""


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def save_entries(conn: sqlite3.Connection, entries: list[LedgerEntry]) -> int:
    """Upserts entries by dedup_key. Returns count of new rows inserted
    (existing rows are updated in place, e.g. if you re-categorize and
    re-import)."""
    cur = conn.cursor()
    new_count = 0
    for e in entries:
        cur.execute("SELECT 1 FROM ledger_entries WHERE dedup_key = ?", (e.dedup_key(),))
        is_new = cur.fetchone() is None
        cur.execute(
            """
            INSERT INTO ledger_entries
                (dedup_key, source, txn_date, description, amount, status, category, matched_enrichment_id, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedup_key) DO UPDATE SET
                status=excluded.status,
                category=excluded.category,
                matched_enrichment_id=excluded.matched_enrichment_id,
                notes=excluded.notes
            """,
            (
                e.dedup_key(),
                e.source.value,
                e.txn_date.isoformat(),
                e.description,
                e.amount,
                e.status.value,
                e.category,
                e.matched_enrichment_id,
                e.notes,
            ),
        )
        if is_new:
            new_count += 1
    conn.commit()
    return new_count


def load_all_entries(conn: sqlite3.Connection) -> list[LedgerEntry]:
    from datetime import date as _date

    cur = conn.cursor()
    cur.execute(
        "SELECT source, txn_date, description, amount, status, category, matched_enrichment_id, notes "
        "FROM ledger_entries ORDER BY txn_date"
    )
    entries = []
    for row in cur.fetchall():
        source, txn_date_str, description, amount, status, category, matched_id, notes = row
        entries.append(
            LedgerEntry(
                source=LedgerSource(source),
                txn_date=_date.fromisoformat(txn_date_str),
                description=description,
                amount=amount,
                status=ReviewStatus(status),
                category=category,
                matched_enrichment_id=matched_id,
                notes=notes,
            )
        )
    return entries


def update_category(conn: sqlite3.Connection, dedup_key: str, category: str) -> None:
    """Used by the UI when the user manually assigns/corrects a category."""
    conn.execute(
        "UPDATE ledger_entries SET category = ?, status = ? WHERE dedup_key = ?",
        (category, ReviewStatus.DIRECT.value, dedup_key),
    )
    conn.commit()


def category_totals(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    """Spend totals by category, excluding transfers/fees which are
    tagged with their own categories but should be shown separately in
    the UI rather than mixed into the budget-vs-actual comparison."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COALESCE(category, 'Uncategorized'), SUM(amount)
        FROM ledger_entries
        WHERE status != 'transfer'
        GROUP BY COALESCE(category, 'Uncategorized')
        ORDER BY SUM(amount) DESC
        """
    )
    return cur.fetchall()
