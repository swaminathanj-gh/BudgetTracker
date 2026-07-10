import Foundation

/// CSV parsers for all five BudgetTracker input sources.
///
/// Card statement parsing (BofA/Chase/Macy's) uses flexible column-name
/// detection since exported header text varies slightly by bank.
///
/// The PayPal and Amazon parsers are written against the *actual* column
/// headers confirmed from real exports during design/testing:
///
///   Amazon order-history export columns:
///     Order ID, Order Date, Total Amount, Currency, Total Savings, Status,
///     Item Title, Item ASIN, Item Quantity, Item Price, Item Discount,
///     Promotions, Item URL, Details URL
///
///   PayPal transaction export columns:
///     Date, Time, TimeZone, Name, Type, Status, Currency, Amount, Fees,
///     Total, Exchange Rate, Receipt ID, Balance, Transaction ID, Item Title
enum ParserError: Error {
    case missingColumn(String)
    case badRow(String)
}

enum ColumnAliases {
    // If your exported file uses different header text than these, add it
    // here rather than editing the parsing logic below.
    static let date = ["date", "posted date", "transaction date"]
    static let description = ["description", "payee", "merchant name or transaction description"]
    static let amount = ["amount", "transaction amount"]
}

func findColumn(_ header: [String], aliases: [String]) throws -> String {
    let lowered = Dictionary(uniqueKeysWithValues: header.map { ($0.trimmingCharacters(in: .whitespaces).lowercased(), $0) })
    for alias in aliases {
        if let match = lowered[alias] {
            return match
        }
    }
    throw ParserError.missingColumn(
        "Could not find any of \(aliases) in CSV header \(header). Add your bank's exact column name to ColumnAliases in Parsers.swift."
    )
}

enum Parsers {

    static func parseCardStatement(path: String, source: LedgerSource) throws -> [LedgerEntry] {
        let rows = try CSV.parse(contentsOf: path)
        guard !rows.isEmpty else { return [] }
        let header = Array(rows[0].keys)

        let dateCol = try findColumn(header, aliases: ColumnAliases.date)
        let descCol = try findColumn(header, aliases: ColumnAliases.description)
        let amountCol = try findColumn(header, aliases: ColumnAliases.amount)

        var entries: [LedgerEntry] = []
        for row in rows {
            let rawAmount = (row[amountCol] ?? "")
                .replacingOccurrences(of: ",", with: "")
                .replacingOccurrences(of: "$", with: "")
                .trimmingCharacters(in: .whitespaces)
            guard !rawAmount.isEmpty, let amount = Double(rawAmount) else { continue }
            guard let date = DateParsing.parse(row[dateCol] ?? "") else { continue }

            entries.append(
                LedgerEntry(
                    source: source,
                    txnDate: date,
                    description: (row[descCol] ?? "").trimmingCharacters(in: .whitespaces),
                    amount: amount
                )
            )
        }
        return entries
    }

    static func parseBofA(path: String) throws -> [LedgerEntry] {
        try parseCardStatement(path: path, source: .bofa)
    }

    static func parseChase(path: String) throws -> [LedgerEntry] {
        try parseCardStatement(path: path, source: .chase)
    }

    static func parseMacys(path: String) throws -> [LedgerEntry] {
        try parseCardStatement(path: path, source: .macys)
    }

    /// Parses the "Order History Exporter for Amazon" CSV.
    ///
    /// One row per item, not per order -- a multi-item order appears as
    /// multiple rows sharing the same Order ID and the same (repeated)
    /// Total Amount. Item Price is unreliable (frequently 0), so match at
    /// the order level, not the item level.
    static func parseAmazonExport(path: String) throws -> [EnrichmentRecord] {
        let rows = try CSV.parse(contentsOf: path)
        var records: [EnrichmentRecord] = []
        for row in rows {
            let amount = Double(row["Total Amount"] ?? "0") ?? 0.0
            guard let date = DateParsing.parse(row["Order Date"] ?? "") else { continue }

            records.append(
                EnrichmentRecord(
                    source: .amazon,
                    recordId: (row["Order ID"] ?? "").trimmingCharacters(in: .whitespaces),
                    txnDate: date,
                    merchantOrItem: (row["Item Title"] ?? "").trimmingCharacters(in: .whitespaces),
                    amount: amount,
                    status: (row["Status"] ?? "").trimmingCharacters(in: .whitespaces)
                )
            )
        }
        return records
    }

    static func groupAmazonByOrder(_ records: [EnrichmentRecord]) -> [String: [EnrichmentRecord]] {
        var grouped: [String: [EnrichmentRecord]] = [:]
        for r in records {
            grouped[r.recordId, default: []].append(r)
        }
        return grouped
    }

    /// Parses a PayPal "Activity Download Report" CSV.
    ///
    /// Every real payment appears as a *pair* of rows sharing the exact
    /// same Date+Time: one negative row with the real merchant Name, and
    /// one positive "funding" row (Type "General Card Deposit" if funded by
    /// the linked card, or "Bank Deposit to PP Account" if funded from a
    /// linked bank account instead -- the latter is filtered out later by
    /// EnrichmentRecord.isUsableForMatching()).
    static func parsePayPalExport(path: String) throws -> [EnrichmentRecord] {
        let rows = try CSV.parse(contentsOf: path)

        var groups: [String: [[String: String]]] = [:]
        for row in rows {
            let key = "\(row["Date"] ?? "")|\(row["Time"] ?? "")"
            groups[key, default: []].append(row)
        }

        var records: [EnrichmentRecord] = []
        for (_, groupRows) in groups {
            guard let dateStr = groupRows.first?["Date"], !dateStr.isEmpty else { continue }
            guard let date = DateParsing.parse(dateStr) else { continue }

            let merchantRow = groupRows.first { (Double($0["Amount"] ?? "0") ?? 0) < 0 }
            let fundingRow = groupRows.first { (Double($0["Amount"] ?? "0") ?? 0) > 0 }

            guard let merchantRow = merchantRow else { continue }

            let amount = abs(Double(merchantRow["Amount"] ?? "0") ?? 0)
            let fundingType = fundingRow?["Type"]

            records.append(
                EnrichmentRecord(
                    source: .paypal,
                    recordId: (merchantRow["Transaction ID"] ?? "").trimmingCharacters(in: .whitespaces),
                    txnDate: date,
                    merchantOrItem: (merchantRow["Name"] ?? merchantRow["Type"] ?? "").trimmingCharacters(in: .whitespaces),
                    amount: amount,
                    fundingType: fundingType?.trimmingCharacters(in: .whitespaces)
                )
            )
        }
        return records
    }
}
