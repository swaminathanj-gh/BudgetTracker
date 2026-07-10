import Foundation

/// Minimal RFC4180-ish CSV parser (Foundation has no built-in CSV support).
/// Handles quoted fields containing commas, e.g. Amazon's Item Title column.
enum CSV {
    static func parse(contentsOf path: String) throws -> [[String: String]] {
        let raw = try String(contentsOfFile: path, encoding: .utf8)
        let rows = parseRows(raw)
        guard let header = rows.first else { return [] }
        var result: [[String: String]] = []
        for row in rows.dropFirst() {
            if row.count == 1 && row[0].isEmpty { continue } // skip trailing blank line
            var dict: [String: String] = [:]
            for (i, col) in header.enumerated() {
                dict[col] = i < row.count ? row[i] : ""
            }
            result.append(dict)
        }
        return result
    }

    static func parseRows(_ text: String) -> [[String]] {
        var rows: [[String]] = []
        var field = ""
        var row: [String] = []
        var inQuotes = false
        var chars = Array(text)
        var i = 0

        // Strip UTF-8 BOM if present
        if chars.first == "\u{FEFF}" {
            chars.removeFirst()
        }

        while i < chars.count {
            let c = chars[i]
            if inQuotes {
                if c == "\"" {
                    if i + 1 < chars.count && chars[i + 1] == "\"" {
                        field.append("\"")
                        i += 1
                    } else {
                        inQuotes = false
                    }
                } else {
                    field.append(c)
                }
            } else {
                if c == "\"" {
                    inQuotes = true
                } else if c == "," {
                    row.append(field)
                    field = ""
                } else if c == "\r" {
                    // ignore, \n handles line end
                } else if c == "\n" {
                    row.append(field)
                    rows.append(row)
                    field = ""
                    row = []
                } else {
                    field.append(c)
                }
            }
            i += 1
        }
        if !field.isEmpty || !row.isEmpty {
            row.append(field)
            rows.append(row)
        }
        return rows
    }
}

enum DateParsing {
    static func parse(_ value: String) -> Date? {
        let trimmed = value.trimmingCharacters(in: .whitespaces)
        let formats = ["MM/dd/yyyy", "yyyy-MM-dd", "MM/dd/yy"]
        for fmt in formats {
            let df = DateFormatter()
            df.dateFormat = fmt
            df.timeZone = TimeZone(identifier: "UTC")
            if let date = df.date(from: trimmed) {
                return date
            }
        }
        return nil
    }
}
