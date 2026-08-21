import Foundation

enum WatchMarkdown {
  private static let maximumParsedUTF8Length = 50_000

  static func canRender(_ source: String) -> Bool {
    source.utf8.count <= maximumParsedUTF8Length
  }

  static func attributedString(from source: String) -> AttributedString {
    guard canRender(source) else { return AttributedString(source) }

    let lines = source.split(separator: "\n", omittingEmptySubsequences: false)
    var rendered = AttributedString()
    var inCodeBlock = false
    var tableHeaders: [String]?

    for index in lines.indices {
      var line = String(lines[index])
      if line.trimmingCharacters(in: .whitespaces).hasPrefix("```") {
        inCodeBlock.toggle()
        continue
      }
      var intent: InlinePresentationIntent?
      if inCodeBlock {
        intent = .code
      } else {
        let nextIsTableSeparator =
          lines.indices.contains(index + 1) && isTableSeparator(String(lines[index + 1]))
        if nextIsTableSeparator, let cells = tableCells(line) {
          tableHeaders = cells
          continue
        }
        if isTableSeparator(line) { continue }
        if let headers = tableHeaders, let cells = tableCells(line) {
          line = zip(headers, cells).map { "**\($0):** \($1)" }.joined(separator: "\n")
        } else {
          tableHeaders = nil
        }
        let transformed = blockPrefix(line)
        line = transformed.text
        if transformed.isHeader { intent = .stronglyEmphasized }
      }
      if !rendered.characters.isEmpty { rendered.append(AttributedString("\n")) }

      var fragment =
        inCodeBlock
        ? AttributedString(line)
        : (try? AttributedString(
          markdown: line,
          options: .init(
            interpretedSyntax: .inlineOnlyPreservingWhitespace,
            failurePolicy: .returnPartiallyParsedIfPossible
          )
        )) ?? AttributedString(line)
      if let intent {
        for range in fragment.runs.map(\.range) {
          fragment[range].inlinePresentationIntent =
            (fragment[range].inlinePresentationIntent ?? []).union(intent)
        }
      }
      rendered.append(fragment)
    }

    sanitizeLinks(in: &rendered)
    return rendered.characters.isEmpty && !source.isEmpty ? AttributedString(source) : rendered
  }

  static func streamingRemainder(
    after renderedSource: String,
    renderedText: String,
    in latestSource: String
  ) -> String? {
    guard latestSource.hasPrefix(renderedSource) else { return nil }
    let rawSuffix = String(latestSource.dropFirst(renderedSource.count))
    guard !rawSuffix.isEmpty else { return "" }

    let trailingWhitespace = String(
      renderedSource.reversed().prefix(while: \.isWhitespace).reversed())
    let maximumOverlap = min(trailingWhitespace.count, renderedText.count)
    for overlap in stride(from: maximumOverlap, through: 1, by: -1) {
      if renderedText.hasSuffix(trailingWhitespace.prefix(overlap)) {
        return String(trailingWhitespace.dropFirst(overlap)) + rawSuffix
      }
    }
    return trailingWhitespace + rawSuffix
  }

  private static func blockPrefix(_ line: String) -> (text: String, isHeader: Bool) {
    let indent = line.prefix(while: { $0 == " " || $0 == "\t" })
    var content = line.dropFirst(indent.count)
    var prefix = String(indent)

    while content.first == ">" {
      content.removeFirst()
      if content.first == " " { content.removeFirst() }
      prefix += "│ "
    }

    let heading = content.prefix(while: { $0 == "#" }).count
    let isHeader = (1...6).contains(heading) && content.dropFirst(heading).first == " "
    if isHeader { content = content.dropFirst(heading + 1) }

    if content.hasPrefix("- ") || content.hasPrefix("* ") || content.hasPrefix("+ ") {
      content = content.dropFirst(2)
      prefix += "• "
    }
    return (prefix + content, isHeader)
  }

  private static func tableCells(_ line: String) -> [String]? {
    var cells = line.split(separator: "|", omittingEmptySubsequences: false)
      .map { $0.trimmingCharacters(in: .whitespaces) }
    if cells.first?.isEmpty == true { cells.removeFirst() }
    if cells.last?.isEmpty == true { cells.removeLast() }
    return cells.count > 1 ? cells : nil
  }

  private static func isTableSeparator(_ line: String) -> Bool {
    guard let cells = tableCells(line) else { return false }
    return cells.allSatisfy { cell in
      return cell.contains("-")
        && cell.trimmingCharacters(in: CharacterSet(charactersIn: ":-")).isEmpty
    }
  }

  private static func sanitizeLinks(in text: inout AttributedString) {
    for range in text.runs.map(\.range) {
      if let link = text[range].link, !isSafeLink(link) { text[range].link = nil }
      if let imageURL = text[range].imageURL {
        text[range].imageURL = nil
        if isSafeLink(imageURL) { text[range].link = imageURL }
      }
    }
  }

  private static func isSafeLink(_ url: URL) -> Bool {
    guard let scheme = url.scheme?.lowercased() else { return false }
    return scheme == "https" || scheme == "http"
  }
}

actor WatchMarkdownRenderer {
  static let shared = WatchMarkdownRenderer()

  func render(_ source: String) async -> AttributedString? {
    guard !Task.isCancelled else { return nil }
    let rendered = WatchMarkdown.attributedString(from: source)
    return Task.isCancelled ? nil : rendered
  }
}
