import Foundation

enum LedgerSource: String, Codable {
    case bofa, chase, macys
}

enum EnrichmentSource: String, Codable {
    case paypal, amazon
}

enum ReviewStatus: String, Codable {
    case unmatched          // not yet processed
    case enriched           // matched to PayPal/Amazon detail, categorized
    case direct             // categorized straight from card description, no enrichment needed
    case transfer           // P2P transfer, excluded from spend totals
    case fee                // fee attached to a transfer
    case needsReview        // failed match / data-quality gap / unrecognized merchant
}

/// One line from a BofA / Chase / Macy's card statement.
///
/// This is the source of truth for the dollar amount and date of every
/// real expense. EnrichmentRecords only ever attach detail to an existing
/// LedgerEntry -- they never create a new one. That's how double-counting
/// between the card statement and PayPal/Amazon is avoided.
struct LedgerEntry: Codable, Identifiable {
    var source: LedgerSource
    var txnDate: Date
    var description: String
    var amount: Double          // positive = spend, negative = payment/credit

    var status: ReviewStatus = .unmatched
    var category: String? = nil
    var matchedEnrichmentId: String? = nil
    var notes: String? = nil

    /// Stable identity for dedup / storage (source + date + amount + description).
    var id: String {
        let df = ISO8601DateFormatter()
        let dateStr = df.string(from: txnDate).prefix(10)
        return "\(source.rawValue)|\(dateStr)|\(String(format: "%.2f", amount))|\(description.trimmingCharacters(in: .whitespaces).lowercased())"
    }
}

/// One record from a PayPal or Amazon export, used to add detail to a
/// matching LedgerEntry.
struct EnrichmentRecord: Codable {
    var source: EnrichmentSource
    var recordId: String                 // PayPal Transaction ID or Amazon Order ID
    var txnDate: Date
    var merchantOrItem: String
    var amount: Double
    var fundingType: String? = nil       // PayPal only: "General Card Deposit" vs "Bank Deposit to PP Account"
    var status: String? = nil            // Amazon only: "Delivered", "Cancelled", etc.

    /// Filters out known data-quality gaps before they're ever handed to
    /// the matcher (cancelled orders, zero-amount rows, bank-funded PayPal
    /// transactions that won't appear on a card statement).
    func isUsableForMatching() -> Bool {
        if source == .amazon {
            if let status = status, status.trimmingCharacters(in: .whitespaces).lowercased() == "cancelled" {
                return false
            }
            if amount == 0 {
                return false
            }
        }
        if source == .paypal {
            if let fundingType = fundingType, fundingType.lowercased().contains("bank deposit") {
                return false
            }
        }
        return true
    }
}

struct BudgetCategory: Codable {
    var name: String
    var budgetAnnual: Double = 0.0
    var parent: String? = nil
}

struct MatchResult {
    var ledgerEntry: LedgerEntry
    var enrichmentRecord: EnrichmentRecord?
    var confidence: Double
    var reason: String
}
