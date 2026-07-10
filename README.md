# BudgetTracker (Swift / Xcode version)

A native macOS app for quarterly expense tracking and categorization
against an annual budget. This is a Swift/SwiftUI rewrite of the original
Python prototype, built as a Swift Package so it opens and runs directly
in Xcode with no other tools needed.

## Opening in Xcode

1. Open Xcode.
2. File -> Open... and select this folder (the one containing `Package.swift`),
   or just double-click `Package.swift` in Finder -- Xcode opens Swift
   Packages directly, no `.xcodeproj` file needed.
3. Wait for Xcode to resolve/index the package (should be fast, there are
   no external dependencies).
4. Press the Run button (or Cmd+R). A window should open.

No terminal, no pip, no virtual environment -- Xcode handles the build.

## IMPORTANT: this has not been compiled or run yet

Unlike the original Python version (which was tested end-to-end against
your real Amazon/PayPal export files), this Swift rewrite was written in a
Linux sandbox with no Swift toolchain and no macOS available, so none of
it has been compiled or executed. The logic is a direct port of the
already-tested Python version, but you will be the first to actually build
this. If Xcode shows build errors, copy the exact error text back and it
can be fixed from there -- treat the first build as a real test, not a
formality.

## Design (same as the Python version)

Every dollar you actually spend shows up on exactly one of three
**primary ledger** sources: BofA rewards card, Chase Prime card, or Macy's
Amex statement. PayPal and Amazon exports are **enrichment only** --
matched against an existing ledger line to fill in real detail, never
creating a new expense entry. See the original design notes (from the
Python README) for the full rationale and the specific CSV quirks found
during testing:

- BofA lines routed through PayPal carry a sub-merchant tag
  (`PAYPAL *ALIPAYUSINC`) used as a matching hint; amount + date (0-3 day
  window) is the real confirmation.
- Chase lines for Amazon purchases carry the literal Amazon Order Number,
  which is an exact-match key against the Amazon export's `Order ID`.
- Amazon export is one row per item; group by Order ID for order-level
  totals. `Status == "Cancelled"` or `Total Amount == 0` rows are
  data-quality gaps, excluded from matching.
- PayPal export pairs a merchant row with a funding row sharing the same
  Date+Time; `Bank Deposit to PP Account` funding means it won't appear on
  the BofA card statement at all (rare exception, routed to manual review).
- Non-marketplace Amazon charges (Prime Video, digital subscriptions)
  skip the order-history lookup and categorize directly.
- P2P transfers (Venmo/Zelle/Cash App) are excluded from spend totals;
  same-day "Cash Equivalent - Transaction Fee" lines pair with them.

## Project layout

```
BudgetTrackerSwift/
  Package.swift
  Sources/BudgetTracker/
    Models.swift              - LedgerEntry, EnrichmentRecord, MatchResult, etc.
    CSV.swift                  - hand-rolled CSV parser + date parsing (Foundation has neither)
    Parsers.swift              - BofA/Chase/Macy's/PayPal/Amazon CSV parsers
    Matcher.swift               - reconciliation engine
    Categorizer.swift          - rule-based categorization + manual override
    Store.swift                 - JSON-file-based local persistence (no SQLite dependency)
    BudgetTrackerApp.swift    - SwiftUI app entry point + views
```

## Storage note

This version stores data as a single JSON file under
`~/Library/Application Support/BudgetTracker/entries.json` rather than
SQLite, to avoid pulling in an external Swift package dependency just for
storage. Fine for a personal quarterly tool's data volume. Category rules
live alongside it in `category_rules.json` -- edit that file (or use the
in-app category picker, which updates it automatically) to tune
categorization to your own budget categories.
