import Foundation

/// Local persistence for BudgetTracker.
///
/// A personal, single-user desktop app doesn't need a real database --
/// a single JSON file under Application Support gives durable local
/// storage with zero setup and no external dependency (avoids pulling in
/// a SQLite Swift package just for this). Dedup is handled via
/// LedgerEntry.id so re-importing the same statement twice (e.g. you
/// export overlapping date ranges each quarter) doesn't create duplicates.
final class Store {
    static let shared = Store()

    private var fileURL: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("BudgetTracker")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("entries.json")
    }

    private var cache: [String: LedgerEntry] = [:]

    private init() {
        load()
    }

    private func load() {
        guard let data = try? Data(contentsOf: fileURL) else { return }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        if let entries = try? decoder.decode([LedgerEntry].self, from: data) {
            cache = Dictionary(uniqueKeysWithValues: entries.map { ($0.id, $0) })
        }
    }

    private func persist() {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = .prettyPrinted
        if let data = try? encoder.encode(Array(cache.values)) {
            try? data.write(to: fileURL)
        }
    }

    /// Upserts entries by id. Returns count of new rows inserted (existing
    /// rows are updated in place, e.g. if you re-categorize and re-import).
    @discardableResult
    func save(_ entries: [LedgerEntry]) -> Int {
        var newCount = 0
        for entry in entries {
            if cache[entry.id] == nil { newCount += 1 }
            cache[entry.id] = entry
        }
        persist()
        return newCount
    }

    func loadAll() -> [LedgerEntry] {
        cache.values.sorted { $0.txnDate < $1.txnDate }
    }

    /// Used by the UI when the user manually assigns/corrects a category.
    func updateCategory(id: String, category: String) {
        guard var entry = cache[id] else { return }
        entry.category = category
        entry.status = .direct
        cache[id] = entry
        persist()
    }

    /// Spend totals by category, excluding transfers which are tagged
    /// with their own category but shown separately in the UI rather than
    /// mixed into the budget-vs-actual comparison.
    func categoryTotals() -> [(category: String, total: Double)] {
        var totals: [String: Double] = [:]
        for entry in cache.values where entry.status != .transfer {
            let key = entry.category ?? "Uncategorized"
            totals[key, default: 0] += entry.amount
        }
        return totals.map { (category: $0.key, total: $0.value) }
            .sorted { $0.total > $1.total }
    }
}
