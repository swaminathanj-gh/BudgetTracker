import Foundation

/// Reconciliation engine: matches card-statement LedgerEntry rows against
/// PayPal/Amazon EnrichmentRecord rows, and applies the exclusion rules
/// (P2P transfers, fees, cancelled/zero-amount rows) discovered during
/// design.
///
/// The guiding rule throughout: the ledger amount (from the card
/// statement) is always the source of truth for the dollar amount.
/// Enrichment records only ever attach detail to an existing ledger line
/// -- they never create a new expense entry, which is how double-counting
/// between the card statement and PayPal/Amazon is avoided.
///
/// Note on value semantics: LedgerEntry is a Swift struct, so these
/// functions return modified copies rather than mutating in place (unlike
/// the original Python version, which mutated objects by reference).
/// Callers reconstruct the final entry list from the returned MatchResults.
enum Matcher {

    static let orderIdPattern = try! NSRegularExpression(pattern: #"\b(\d{3}-\d{7}-\d{7})\b"#)
    static let paypalTagPattern = try! NSRegularExpression(pattern: #"PAYPAL\s*\*\s*([A-Za-z0-9]+)"#, options: .caseInsensitive)
    static let amazonNonMarketplacePattern = try! NSRegularExpression(pattern: #"AMAZON\s*DIGIT|PRIME\s*VIDEO|AMZN\s*DIGITAL"#, options: .caseInsensitive)
    static let p2pPattern = try! NSRegularExpression(pattern: #"VENMO\s*\*|ZELLE|CASH\s*APP"#, options: .caseInsensitive)
    static let feePattern = try! NSRegularExpression(pattern: #"CASH EQUIVALENT.*TRANSACTION FEE"#, options: .caseInsensitive)

    static let dateMatchWindowDays = 3

    static func firstMatch(_ regex: NSRegularExpression, in text: String) -> String? {
        let range = NSRange(text.startIndex..., in: text)
        guard let match = regex.firstMatch(in: text, range: range), match.numberOfRanges > 1,
              let r = Range(match.range(at: 1), in: text) else { return nil }
        return String(text[r])
    }

    static func hasMatch(_ regex: NSRegularExpression, in text: String) -> Bool {
        let range = NSRange(text.startIndex..., in: text)
        return regex.firstMatch(in: text, range: range) != nil
    }

    static func withinWindow(_ d1: Date, _ d2: Date, days: Int = dateMatchWindowDays) -> Bool {
        abs(d1.timeIntervalSince(d2)) <= Double(days) * 86400
    }

    /// Matches BofA lines that look like PayPal charges against the PayPal
    /// export. Primary confirmation is amount + date window; the
    /// sub-merchant tag parsed from the BofA description is a bonus
    /// confidence signal when it lines up with the PayPal record's name,
    /// but is NOT required -- real-world testing showed PayPal's `Name`
    /// field doesn't always match the card's condensed sub-merchant tag.
    static func matchBofaPayPal(bofaEntries: [LedgerEntry], paypalRecords: [EnrichmentRecord]) -> [MatchResult] {
        let usablePaypal = paypalRecords.filter { $0.isUsableForMatching() }
        var results: [MatchResult] = []

        for var entry in bofaEntries {
            guard let tag = firstMatch(paypalTagPattern, in: entry.description)?.lowercased() else {
                continue // not a PayPal-routed line, leave for direct categorization
            }

            let candidates = usablePaypal.filter {
                abs($0.amount - abs(entry.amount)) < 0.01 && withinWindow($0.txnDate, entry.txnDate)
            }

            if candidates.isEmpty {
                entry.status = .needsReview
                results.append(MatchResult(
                    ledgerEntry: entry,
                    enrichmentRecord: nil,
                    confidence: 0.0,
                    reason: "BofA line tagged PAYPAL *\(tag) but no PayPal export record matched on amount+date. Needs manual review (check PayPal export covers this date range)."
                ))
                continue
            }

            let best = candidates.first { $0.merchantOrItem.lowercased().replacingOccurrences(of: " ", with: "").contains(tag) }
                ?? candidates.min(by: { abs($0.txnDate.timeIntervalSince(entry.txnDate)) < abs($1.txnDate.timeIntervalSince(entry.txnDate)) })!

            let confidence = best.merchantOrItem.lowercased().contains(tag) ? 0.95 : 0.7
            entry.status = .enriched
            entry.matchedEnrichmentId = best.recordId

            results.append(MatchResult(
                ledgerEntry: entry,
                enrichmentRecord: best,
                confidence: confidence,
                reason: "Matched on amount $\(String(format: "%.2f", best.amount)) + date within \(dateMatchWindowDays)d window."
            ))
        }
        return results
    }

    /// Matches Chase lines against Amazon order-history records using an
    /// exact Order ID match -- no fuzzy logic needed since Chase's own
    /// statement surfaces the literal Amazon order number.
    static func matchChaseAmazon(chaseEntries: [LedgerEntry], amazonGrouped: [String: [EnrichmentRecord]]) -> [MatchResult] {
        var results: [MatchResult] = []

        for var entry in chaseEntries {
            if hasMatch(amazonNonMarketplacePattern, in: entry.description) {
                // Amazon-billed but not a marketplace order (Prime Video,
                // digital subscriptions, third-party billers) -- no order
                // history lookup possible, categorize directly.
                entry.status = .direct
                results.append(MatchResult(
                    ledgerEntry: entry,
                    enrichmentRecord: nil,
                    confidence: 1.0,
                    reason: "Amazon-billed non-marketplace charge; categorize from description directly."
                ))
                continue
            }

            guard let orderId = firstMatch(orderIdPattern, in: entry.description) else {
                continue // not an Amazon line at all, leave for direct categorization
            }

            guard let items = amazonGrouped[orderId] else {
                entry.status = .needsReview
                results.append(MatchResult(
                    ledgerEntry: entry,
                    enrichmentRecord: nil,
                    confidence: 0.0,
                    reason: "Chase line references Amazon order \(orderId) but it wasn't found in the Amazon export. Needs manual review."
                ))
                continue
            }

            let usableItems = items.filter { $0.isUsableForMatching() }
            if usableItems.isEmpty {
                entry.status = .needsReview
                results.append(MatchResult(
                    ledgerEntry: entry,
                    enrichmentRecord: nil,
                    confidence: 0.0,
                    reason: "Amazon order \(orderId) found but all rows were cancelled/zero-amount."
                ))
                continue
            }

            entry.status = .enriched
            entry.matchedEnrichmentId = orderId
            let itemSummary = usableItems.map { $0.merchantOrItem }.joined(separator: "; ")
            results.append(MatchResult(
                ledgerEntry: entry,
                enrichmentRecord: usableItems[0],
                confidence: 1.0,
                reason: "Exact Order ID match (\(orderId)): \(itemSummary)"
            ))
        }
        return results
    }

    /// Flags P2P transfers and their paired fees so they're excluded from
    /// category-spend totals rather than miscategorized as a purchase.
    /// Returns a new array with the relevant entries updated.
    static func applyExclusionRules(_ entries: [LedgerEntry]) -> [LedgerEntry] {
        var sorted = entries.sorted { $0.txnDate < $1.txnDate }

        for i in sorted.indices {
            if hasMatch(p2pPattern, in: sorted[i].description) {
                sorted[i].status = .transfer
                sorted[i].category = "Transfers (excluded)"
            }
        }
        for i in sorted.indices {
            if hasMatch(feePattern, in: sorted[i].description) {
                sorted[i].status = .fee
                sorted[i].category = "Transfer fees (excluded)"
                if let transfer = sorted.first(where: { $0.status == .transfer && $0.txnDate == sorted[i].txnDate }) {
                    sorted[i].notes = "Fee for transfer: \(transfer.description)"
                }
            }
        }
        return sorted
    }

    /// Runs the full reconciliation pipeline across all three card
    /// ledgers. Returns the merged entry list (with P2P/fee exclusions and
    /// PayPal/Amazon matches applied) plus the MatchResults for entries
    /// that went through enrichment matching, for UI/debugging purposes.
    static func reconcile(
        bofaEntries: [LedgerEntry],
        chaseEntries: [LedgerEntry],
        macysEntries: [LedgerEntry],
        paypalRecords: [EnrichmentRecord],
        amazonRecords: [EnrichmentRecord]
    ) -> (entries: [LedgerEntry], matchResults: [MatchResult]) {
        let allEntries = applyExclusionRules(bofaEntries + chaseEntries + macysEntries)

        let remainingBofa = allEntries.filter { $0.source == .bofa && $0.status == .unmatched }
        let remainingChase = allEntries.filter { $0.source == .chase && $0.status == .unmatched }
        let untouched = allEntries.filter {
            !(($0.source == .bofa || $0.source == .chase) && $0.status == .unmatched)
        }

        let amazonGrouped = Parsers.groupAmazonByOrder(amazonRecords)

        let bofaResults = matchBofaPayPal(bofaEntries: remainingBofa, paypalRecords: paypalRecords)
        let chaseResults = matchChaseAmazon(chaseEntries: remainingChase, amazonGrouped: amazonGrouped)
        let matchResults = bofaResults + chaseResults

        let matchedEntries = matchResults.map { $0.ledgerEntry }
        return (entries: untouched + matchedEntries, matchResults: matchResults)
    }
}
