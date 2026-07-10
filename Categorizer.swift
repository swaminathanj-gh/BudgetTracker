import Foundation

/// Rule-based categorization with manual override.
///
/// Kept simple on purpose: a small, user-editable list of
/// (keyword -> category) rules, checked against either the enrichment
/// record's merchant/item text (when a match was found) or the ledger
/// entry's own description (for direct/uncategorized lines). Anything
/// that doesn't match a rule is left for manual categorization in the UI
/// rather than guessed at.
enum Categorizer {

    static var rulesURL: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("BudgetTracker")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("category_rules.json")
    }

    // Seed rules -- edit category_rules.json after first run to tune these
    // to your own budget categories, this is just a starting point.
    static let defaultRules: [String: String] = [
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
    ]

    static func loadRules() -> [String: String] {
        if let data = try? Data(contentsOf: rulesURL),
           let rules = try? JSONDecoder().decode([String: String].self, from: data) {
            return rules
        }
        return defaultRules
    }

    static func saveRules(_ rules: [String: String]) {
        if let data = try? JSONEncoder().encode(rules) {
            try? data.write(to: rulesURL)
        }
    }

    static func categorize(text: String, rules: [String: String]) -> String? {
        let lowered = text.lowercased()
        for (keyword, category) in rules {
            if lowered.contains(keyword.lowercased()) {
                return category
            }
        }
        return nil
    }

    /// Produces the final categorized entry list.
    ///
    /// - Entries with a matched enrichment record are categorized from the
    ///   enrichment record's merchant/item text (the real detail looked up
    ///   from PayPal/Amazon).
    /// - Entries already marked direct/transfer/fee are categorized (or
    ///   keep their pre-set category) from the ledger description itself.
    /// - Anything left with no rule match stays uncategorized (nil) and
    ///   status .needsReview, surfaced in the UI for manual assignment.
    static func applyCategorization(
        matchResults: [MatchResult],
        allEntries: [LedgerEntry],
        rules: [String: String]? = nil
    ) -> [LedgerEntry] {
        let rules = rules ?? loadRules()
        var byId: [String: LedgerEntry] = Dictionary(uniqueKeysWithValues: allEntries.map { ($0.id, $0) })

        for result in matchResults {
            var entry = result.ledgerEntry
            let category: String?
            if let enrichment = result.enrichmentRecord {
                category = categorize(text: enrichment.merchantOrItem, rules: rules)
            } else {
                category = categorize(text: entry.description, rules: rules)
            }

            if let category = category {
                entry.category = category
            } else if entry.status == .enriched || entry.status == .direct {
                entry.status = .needsReview
            }
            byId[entry.id] = entry
        }

        // Direct categorization for any entry that never went through the
        // matcher at all (plain card charges with no PayPal/Amazon/P2P
        // pattern -- the majority of lines on any statement).
        for (id, entry) in Array(byId) where entry.status == .unmatched {
            var updated = entry
            if let category = categorize(text: entry.description, rules: rules) {
                updated.category = category
                updated.status = .direct
            } else {
                updated.status = .needsReview
            }
            byId[id] = updated
        }

        return Array(byId.values)
    }
}
