# BudgetTracker

A personal desktop app (macOS) for quarterly expense tracking and categorization
against an annual budget.

## Why this design

Every dollar you actually spend shows up on exactly one of three **primary
ledger** sources: your BofA rewards card, Chase Prime card, or Macy's Amex
statement. Two of those cards route certain purchases through PayPal or
Amazon, which show up on the card statement as an opaque line (e.g.
`PAYPAL *ALIPAYUSINC` or `AMAZON MKTPL*...`) with no detail on what was
actually bought. PayPal's own transaction export and Amazon's own order
history export are used purely as **enrichment** sources: they are looked up
and matched against an existing ledger line to fill in real item-level detail
for categorization. They never create a new expense entry on their own --
that's how double-counting is avoided.

```
Primary ledger (source of $ truth)      Enrichment (source of "what was it")
-----------------------------------     ------------------------------------
BofA rewards card statement       <----  PayPal transaction export
Chase Prime card statement        <----  Amazon order history export
Macy's Amex card statement        (no enrichment source; self-contained)
```

## Project layout

```
BudgetTracker/
  app/
    models.py       - core data classes (LedgerEntry, EnrichmentRecord, Category)
    parsers.py       - CSV parsers for all five input sources
    matcher.py       - reconciliation engine (ledger <-> enrichment matching)
    categorizer.py   - rule-based categorization + manual override
    db.py            - SQLite storage layer
    main.py           - PySide6 desktop UI entry point
  requirements.txt
  README.md
```

## Data sources and their real quirks (found via manual testing)

- **BofA card CSV**: PayPal-funded purchases include a sub-merchant tag after
  `PAYPAL *`, e.g. `PAYPAL *ALIPAYUSINC`. Use that tag as the primary match
  hint against PayPal's `Name` field, confirmed by amount + date within a
  0-3 day window (PayPal's own transaction date can lag/lead the card
  posting date).
- **Chase card CSV**: Amazon purchases include the literal Amazon Order
  Number in the description/memo, which is an *exact* match key against the
  `Order ID` column in the Amazon order-history export. No fuzzy matching
  needed for Amazon.
- **Amazon order-history export**: one row per item, not per order --  a
  multi-item order appears as multiple rows sharing the same `Order ID` and
  the same (repeated) `Total Amount`. `Item Price` is unreliable (often 0),
  so match/categorize at the order level, not the item level. Rows with
  `Status == "Cancelled"` or `Total Amount == 0` are data-quality gaps, not
  real charges -- route them to manual review, don't try to match them.
- **PayPal transaction export**: every real payment appears as a *pair* of
  rows sharing the exact same Date+Time: one negative row with the real
  merchant name, and one positive "funding" row. Group by identical
  timestamp to join them. The funding row's `Type` matters:
  `General Card Deposit` means it was funded by the linked card (matches
  the BofA statement); `Bank Deposit to PP Account` means it was funded from
  a linked bank account instead, and will **not** appear on the BofA card
  statement at all -- these are rare exceptions, routed to manual review
  rather than a full separate bank-statement reconciliation pipeline.
- **Non-marketplace Amazon charges**: things like `Amazon Digit*`,
  `Prime Video Channels`, or third-party subscriptions billed through Amazon
  (e.g. `EMERGENT`, `AMZ*EMERGENT LABS`) never have an Order Number and skip
  the Amazon lookup entirely -- categorized directly from the card
  description.
- **P2P transfers** (`VENMO *`, `ZELLE`, `CASH APP`) are excluded from
  category-spend totals by default and routed to a separate "transfers"
  bucket, since they're not purchases. A same-day
  `CASH EQUIVALENT - TRANSACTION FEE` line is paired with the P2P line above
  it as its fee.
- **Anything left uncategorized** at the end of the pipeline -- failed
  match, unrecognized merchant, data-quality gap -- goes to a manual-review
  queue rather than being silently dropped or miscategorized.

## Getting started

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app/main.py
```

## Column mapping note

The BofA/Chase/Macy's CSV parsers in `parsers.py` are written against the
generic column names most banks use (`Date`, `Description`, `Amount`), with
a flexible column-detection step, since your own exported files may use
slightly different header names than what's shown in the design examples
above. Check `parsers.py`'s `COLUMN_ALIASES` dict and add your bank's exact
header text if a file doesn't parse first try.
