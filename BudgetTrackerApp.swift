import SwiftUI
import AppKit
import UniformTypeIdentifiers

@main
struct BudgetTrackerApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .windowResizability(.contentSize)
    }
}

struct ContentView: View {
    var body: some View {
        TabView {
            TransactionsView()
                .tabItem { Label("Transactions", systemImage: "list.bullet.rectangle") }
            SummaryView()
                .tabItem { Label("Summary", systemImage: "chart.pie") }
        }
        .frame(minWidth: 900, minHeight: 600)
    }
}

// MARK: - Transactions tab

struct TransactionsView: View {
    @State private var entries: [LedgerEntry] = []
    @State private var statusMessage: String = "No data imported yet."
    @State private var rules = Categorizer.loadRules()

    // Staged (not yet reconciled) imports
    @State private var pendingBofa: [LedgerEntry] = []
    @State private var pendingChase: [LedgerEntry] = []
    @State private var pendingMacys: [LedgerEntry] = []
    @State private var pendingPaypal: [EnrichmentRecord] = []
    @State private var pendingAmazon: [EnrichmentRecord] = []

    private let categories = ["Uncategorized", "Groceries", "Utilities", "Auto/Gas", "Subscriptions",
                               "Entertainment", "Shopping/Misc", "Clothing", "Travel",
                               "Health/Supplements", "Health/Medical", "Transfers (excluded)",
                               "Transfer fees (excluded)"]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                importButton("Import BofA CSV") { importCard(source: .bofa) }
                importButton("Import Chase CSV") { importCard(source: .chase) }
                importButton("Import Macy's CSV") { importCard(source: .macys) }
                importButton("Import PayPal export") { importPaypal() }
                importButton("Import Amazon export") { importAmazon() }
                Button("Reconcile + Categorize") { reconcile() }
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal)
            .padding(.top)

            Text(statusMessage)
                .font(.callout)
                .foregroundStyle(.secondary)
                .padding(.horizontal)

            Table(entries) {
                TableColumn("Date") { entry in
                    Text(entry.txnDate.formatted(date: .numeric, time: .omitted))
                }
                TableColumn("Source") { entry in Text(entry.source.rawValue) }
                TableColumn("Description") { entry in
                    Text(entry.description).lineLimit(1)
                }
                TableColumn("Amount") { entry in
                    Text(entry.amount, format: .currency(code: "USD"))
                }
                TableColumn("Category") { entry in
                    Picker("", selection: categoryBinding(for: entry)) {
                        ForEach(categories, id: \.self) { Text($0).tag($0) }
                    }
                    .labelsHidden()
                }
                TableColumn("Status") { entry in
                    Text(entry.status.rawValue)
                        .foregroundStyle(entry.status == .needsReview ? .red : .primary)
                }
            }
            .padding(.horizontal)
        }
        .onAppear { refresh() }
    }

    private func importButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
    }

    private func categoryBinding(for entry: LedgerEntry) -> Binding<String> {
        Binding(
            get: { entry.category ?? "Uncategorized" },
            set: { newValue in
                Store.shared.updateCategory(id: entry.id, category: newValue)
                refresh()
            }
        )
    }

    private func pickFile() -> URL? {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.commaSeparatedText, UTType(filenameExtension: "csv") ?? .data]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        return panel.runModal() == .OK ? panel.url : nil
    }

    private func importCard(source: LedgerSource) {
        guard let url = pickFile() else { return }
        do {
            let parsed: [LedgerEntry]
            switch source {
            case .bofa: parsed = try Parsers.parseBofA(path: url.path)
            case .chase: parsed = try Parsers.parseChase(path: url.path)
            case .macys: parsed = try Parsers.parseMacys(path: url.path)
            }
            switch source {
            case .bofa: pendingBofa.append(contentsOf: parsed)
            case .chase: pendingChase.append(contentsOf: parsed)
            case .macys: pendingMacys.append(contentsOf: parsed)
            }
            statusMessage = "Loaded \(parsed.count) rows from \(url.lastPathComponent). Click 'Reconcile + Categorize' when all files are loaded."
        } catch {
            statusMessage = "Import failed: \(error)"
        }
    }

    private func importPaypal() {
        guard let url = pickFile() else { return }
        do {
            let records = try Parsers.parsePayPalExport(path: url.path)
            pendingPaypal.append(contentsOf: records)
            statusMessage = "Loaded \(records.count) PayPal records from \(url.lastPathComponent)."
        } catch {
            statusMessage = "Import failed: \(error)"
        }
    }

    private func importAmazon() {
        guard let url = pickFile() else { return }
        do {
            let records = try Parsers.parseAmazonExport(path: url.path)
            pendingAmazon.append(contentsOf: records)
            statusMessage = "Loaded \(records.count) Amazon order-item rows from \(url.lastPathComponent)."
        } catch {
            statusMessage = "Import failed: \(error)"
        }
    }

    private func reconcile() {
        guard !(pendingBofa.isEmpty && pendingChase.isEmpty && pendingMacys.isEmpty
                && pendingPaypal.isEmpty && pendingAmazon.isEmpty) else {
            statusMessage = "Nothing to reconcile -- import at least one file first."
            return
        }

        let (mergedEntries, matchResults) = Matcher.reconcile(
            bofaEntries: pendingBofa,
            chaseEntries: pendingChase,
            macysEntries: pendingMacys,
            paypalRecords: pendingPaypal,
            amazonRecords: pendingAmazon
        )

        let finalEntries = Categorizer.applyCategorization(
            matchResults: matchResults,
            allEntries: mergedEntries,
            rules: rules
        )

        let newCount = Store.shared.save(finalEntries)
        let needsReview = finalEntries.filter { $0.status == .needsReview }.count
        statusMessage = "Reconciled \(finalEntries.count) ledger lines (\(newCount) new). \(needsReview) need manual review."

        pendingBofa.removeAll()
        pendingChase.removeAll()
        pendingMacys.removeAll()
        pendingPaypal.removeAll()
        pendingAmazon.removeAll()

        refresh()
    }

    private func refresh() {
        entries = Store.shared.loadAll()
    }
}

// MARK: - Summary tab

struct SummaryView: View {
    @State private var totals: [(category: String, total: Double)] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button("Refresh totals") { refresh() }
                .padding([.horizontal, .top])

            Table(totals, id: \.category) {
                TableColumn("Category") { row in Text(row.category) }
                TableColumn("Total") { row in Text(row.total, format: .currency(code: "USD")) }
            }
            .padding(.horizontal)
        }
        .onAppear { refresh() }
    }

    private func refresh() {
        totals = Store.shared.categoryTotals()
    }
}
