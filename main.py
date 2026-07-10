"""BudgetTracker desktop app entry point (PySide6).

Two tabs:
  - Transactions: import statements, review/edit categories, see what's
    still flagged NEEDS_REVIEW.
  - Summary: spend totals by category.

Run with:
    python app/main.py
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QFileDialog,
    QMessageBox,
    QComboBox,
    QLabel,
)
from PySide6.QtCore import Qt

from . import db, parsers, matcher, categorizer
from .models import LedgerSource, ReviewStatus

STATUS_COLORS = {
    ReviewStatus.NEEDS_REVIEW: "#5a3a3a",
    ReviewStatus.ENRICHED: "#2f4f3f",
    ReviewStatus.DIRECT: "#2f2f2f",
    ReviewStatus.TRANSFER: "#3f3f5f",
    ReviewStatus.FEE: "#3f3f5f",
}


class TransactionsTab(QWidget):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.rules = categorizer.load_rules()

        layout = QVBoxLayout(self)

        import_row = QHBoxLayout()
        for label, source in [
            ("Import BofA CSV", LedgerSource.BOFA),
            ("Import Chase CSV", LedgerSource.CHASE),
            ("Import Macy's CSV", LedgerSource.MACYS),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _, s=source: self.import_card_statement(s))
            import_row.addWidget(btn)

        paypal_btn = QPushButton("Import PayPal export")
        paypal_btn.clicked.connect(self.import_paypal)
        import_row.addWidget(paypal_btn)

        amazon_btn = QPushButton("Import Amazon export")
        amazon_btn.clicked.connect(self.import_amazon)
        import_row.addWidget(amazon_btn)

        reconcile_btn = QPushButton("Reconcile + Categorize")
        reconcile_btn.clicked.connect(self.run_reconciliation)
        import_row.addWidget(reconcile_btn)

        layout.addLayout(import_row)

        self.status_label = QLabel("No data imported yet.")
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Date", "Source", "Description", "Amount", "Category", "Status"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        # In-memory staging for imported-but-not-yet-reconciled files
        self._pending_bofa = []
        self._pending_chase = []
        self._pending_macys = []
        self._pending_paypal = []
        self._pending_amazon = []

        self.refresh_table()

    def _pick_file(self) -> str | None:
        path, _ = QFileDialog.getOpenFileName(self, "Select CSV", "", "CSV files (*.csv *.CSV)")
        return path or None

    def import_card_statement(self, source: LedgerSource):
        path = self._pick_file()
        if not path:
            return
        parser = {
            LedgerSource.BOFA: parsers.parse_bofa,
            LedgerSource.CHASE: parsers.parse_chase,
            LedgerSource.MACYS: parsers.parse_macys,
        }[source]
        try:
            entries = parser(path)
        except Exception as exc:  # noqa: BLE001 - surface any parse error to the user
            QMessageBox.critical(self, "Import failed", str(exc))
            return

        target = {
            LedgerSource.BOFA: self._pending_bofa,
            LedgerSource.CHASE: self._pending_chase,
            LedgerSource.MACYS: self._pending_macys,
        }[source]
        target.extend(entries)
        self.status_label.setText(
            f"Loaded {len(entries)} rows from {path}. Click 'Reconcile + Categorize' when all files are loaded."
        )

    def import_paypal(self):
        path = self._pick_file()
        if not path:
            return
        try:
            records = parsers.parse_paypal_export(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        self._pending_paypal.extend(records)
        self.status_label.setText(f"Loaded {len(records)} PayPal records from {path}.")

    def import_amazon(self):
        path = self._pick_file()
        if not path:
            return
        try:
            records = parsers.parse_amazon_export(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        self._pending_amazon.extend(records)
        self.status_label.setText(f"Loaded {len(records)} Amazon order-item rows from {path}.")

    def run_reconciliation(self):
        if not any(
            [
                self._pending_bofa,
                self._pending_chase,
                self._pending_macys,
                self._pending_paypal,
                self._pending_amazon,
            ]
        ):
            QMessageBox.information(self, "Nothing to reconcile", "Import at least one file first.")
            return

        results = matcher.reconcile(
            self._pending_bofa,
            self._pending_chase,
            self._pending_macys,
            self._pending_paypal,
            self._pending_amazon,
        )
        all_entries = self._pending_bofa + self._pending_chase + self._pending_macys
        categorizer.apply_categorization(results, all_entries, self.rules)

        new_count = db.save_entries(self.conn, all_entries)
        self.status_label.setText(
            f"Reconciled {len(all_entries)} ledger lines ({new_count} new). "
            f"{sum(1 for e in all_entries if e.status == ReviewStatus.NEEDS_REVIEW)} need manual review."
        )

        # Clear staging now that it's persisted
        self._pending_bofa.clear()
        self._pending_chase.clear()
        self._pending_macys.clear()
        self._pending_paypal.clear()
        self._pending_amazon.clear()

        self.refresh_table()

    def refresh_table(self):
        entries = db.load_all_entries(self.conn)
        self.table.setRowCount(len(entries))
        self._entries_by_row = entries

        categories = sorted(set(self.rules.values()) | {"Uncategorized", "Needs Review"})

        for row, e in enumerate(entries):
            self.table.setItem(row, 0, QTableWidgetItem(e.txn_date.isoformat()))
            self.table.setItem(row, 1, QTableWidgetItem(e.source.value))
            self.table.setItem(row, 2, QTableWidgetItem(e.description))
            self.table.setItem(row, 3, QTableWidgetItem(f"{e.amount:.2f}"))

            combo = QComboBox()
            combo.addItems(categories)
            current = e.category or "Uncategorized"
            if current not in categories:
                combo.addItem(current)
            combo.setCurrentText(current)
            combo.currentTextChanged.connect(
                lambda text, entry=e: self._on_category_changed(entry, text)
            )
            self.table.setCellWidget(row, 4, combo)

            status_item = QTableWidgetItem(e.status.value)
            self.table.setItem(row, 5, status_item)

        self.table.resizeColumnsToContents()

    def _on_category_changed(self, entry, new_category: str):
        db.update_category(self.conn, entry.dedup_key(), new_category)


class SummaryTab(QWidget):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        layout = QVBoxLayout(self)

        refresh_btn = QPushButton("Refresh totals")
        refresh_btn.clicked.connect(self.refresh)
        layout.addWidget(refresh_btn)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Category", "Total"])
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        self.refresh()

    def refresh(self):
        totals = db.category_totals(self.conn)
        self.table.setRowCount(len(totals))
        for row, (category, total) in enumerate(totals):
            self.table.setItem(row, 0, QTableWidgetItem(category))
            self.table.setItem(row, 1, QTableWidgetItem(f"${total:,.2f}"))
        self.table.resizeColumnsToContents()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BudgetTracker")
        self.resize(1000, 650)

        self.conn = db.get_connection()

        tabs = QTabWidget()
        self.transactions_tab = TransactionsTab(self.conn)
        self.summary_tab = SummaryTab(self.conn)
        tabs.addTab(self.transactions_tab, "Transactions")
        tabs.addTab(self.summary_tab, "Summary")
        tabs.currentChanged.connect(lambda _: self.summary_tab.refresh())

        self.setCentralWidget(tabs)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
