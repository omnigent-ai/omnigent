import XCTest

@testable import Omnigent

final class DeepLinkTests: XCTestCase {
  /// A real conversation id — a bare 32-char hex uuid, the form the API emits
  /// today (`uuid4().hex`). Used as the canonical valid id across these tests.
  private let hex = "e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9"
  private let dashed = "e4f5a6b7-c8d9-e0f1-a2b3-c4d5e6f7a8b9"

  func testParsesLoopbackHostWithPortAsHTTP() {
    let dl = DeepLink.parse(URL(string: "omnigent://localhost:8000/c/\(hex)")!)
    XCTAssertEqual(dl?.origin, "http://localhost:8000")
    XCTAssertEqual(dl?.path, "/c/\(hex)")

    let dl2 = DeepLink.parse(URL(string: "omnigent://127.0.0.1:8000/c/\(hex)")!)
    XCTAssertEqual(dl2?.origin, "http://127.0.0.1:8000")
    XCTAssertEqual(dl2?.path, "/c/\(hex)")
  }

  func testParsesRemoteHostAsHTTPS() {
    let dl = DeepLink.parse(URL(string: "omnigent://my-workspace.cloud.databricks.com/c/\(hex)")!)
    XCTAssertEqual(dl?.origin, "https://my-workspace.cloud.databricks.com")
    XCTAssertEqual(dl?.path, "/c/\(hex)")
  }

  func testPreservesNonDefaultPortOnRemoteHost() {
    let dl = DeepLink.parse(URL(string: "omnigent://example.com:8443/c/\(hex)")!)
    XCTAssertEqual(dl?.origin, "https://example.com:8443")
    XCTAssertEqual(dl?.path, "/c/\(hex)")
  }

  func testParsesIPv6LoopbackAsHTTP() {
    let dl = DeepLink.parse(URL(string: "omnigent://[::1]:8000/c/\(hex)")!)
    XCTAssertEqual(dl?.origin, "http://[::1]:8000")
    XCTAssertEqual(dl?.path, "/c/\(hex)")
  }

  func testStripsTrailingSlashFromPath() {
    // Foundation already strips it; the parser normalizes regardless so the
    // forwarded path is always `/c/<id>` (react-router matches that exactly).
    let dl = DeepLink.parse(URL(string: "omnigent://localhost:8000/c/\(hex)/")!)
    XCTAssertEqual(dl?.path, "/c/\(hex)")
  }

  func testAcceptsDashedUuidId() {
    // The SPA's `bareConversationId` normalizes a canonical dashed uuid to the
    // bare hex form, so a deep link minted with a dashed id must still parse.
    let dl = DeepLink.parse(URL(string: "omnigent://localhost:8000/c/\(dashed)")!)
    XCTAssertEqual(dl?.origin, "http://localhost:8000")
    XCTAssertEqual(dl?.path, "/c/\(dashed)")
  }

  func testAcceptsLegacyConvPrefixedId() {
    // Pre-migration links carry a `conv_` prefix; the SPA strips it. The
    // parser forwards the id as-is and lets the SPA normalize.
    let dl = DeepLink.parse(URL(string: "omnigent://localhost:8000/c/conv_\(hex)")!)
    XCTAssertEqual(dl?.path, "/c/conv_\(hex)")
  }

  func testAcceptsLegacyAgPrefixedId() {
    let dl = DeepLink.parse(URL(string: "omnigent://localhost:8000/c/ag_\(hex)")!)
    XCTAssertEqual(dl?.path, "/c/ag_\(hex)")
  }

  func testAcceptsUppercaseHexId() {
    // Case-insensitive: the SPA lowercases the id downstream.
    let upper = hex.uppercased()
    let dl = DeepLink.parse(URL(string: "omnigent://localhost:8000/c/\(upper)")!)
    XCTAssertEqual(dl?.path, "/c/\(upper)")
  }

  func testRejectsNonOmnigentScheme() {
    XCTAssertNil(DeepLink.parse(URL(string: "https://localhost:8000/c/\(hex)")!))
    XCTAssertNil(DeepLink.parse(URL(string: "vscode://localhost/c/\(hex)")!))
  }

  func testRejectsLinkWithNoHost() {
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent:///c/\(hex)")!))
  }

  func testRejectsNonConversationPaths() {
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/inbox")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/settings/appearance")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/\(hex)/extra")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/")!))
  }

  // MARK: - Smuggled separators (F-CR-6)

  /// `URL.path` is percent-DECODED, so `%3F` (`?`) reappears as a literal in
  /// the path. Without grammar validation this smuggles a query past the
  /// "/c/<id> only" shape: the rebuilt `/c/<id>?view=terminal` would reach the
  /// SPA with a real query string. The id-grammar guard rejects the `?`.
  func testRejectsSmuggledQueryViaEncodedQuestionMark() {
    let url = URL(string: "omnigent://localhost:8000/c/\(hex)%3Fview=terminal")!
    XCTAssertEqual(url.path, "/c/\(hex)?view=terminal")  // sanity: Foundation decodes %3F
    XCTAssertNil(DeepLink.parse(url))
  }

  /// `%23` (`#`) decodes to a literal `#` and would smuggle a fragment.
  func testRejectsSmuggledFragmentViaEncodedHash() {
    let url = URL(string: "omnigent://localhost:8000/c/\(hex)%23frag")!
    XCTAssertEqual(url.path, "/c/\(hex)#frag")  // sanity: Foundation decodes %23
    XCTAssertNil(DeepLink.parse(url))
  }

  /// `%2F` (`/`) decodes to a path separator — already caught by the nested-path
  /// check, but also rejected by the grammar (no `/` in an id).
  func testRejectsEncodedPathSeparator() {
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/\(hex)%2Fextra")!))
  }

  /// `%2E` (`.`) decodes to a literal `.`; a bare `.`/`..` could be mistaken
  /// for a path traversal segment. The grammar has no `.`.
  func testRejectsEncodedDotAndDotDot() {
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/\(hex)%2E.")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/%2e%2e")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/..")!))
  }

  /// Control characters (`%00` null, `%0A` newline, `%7F` DEL) decode into the
  /// path and are not part of any conversation id.
  func testRejectsControlCharacters() {
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/\(hex)%00x")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/\(hex)%0Ax")!))
    XCTAssertNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/\(hex)%7Fx")!))
  }

  /// A malformed percent-escape leaves a literal `%` in `URL.path`; `%` is not
  /// in the id grammar.
  func testRejectsMalformedPercentEscape() {
    let url = URL(string: "omnigent://localhost:8000/c/\(hex)%zz")!
    XCTAssertNil(DeepLink.parse(url))
  }

  /// The validator is a DENYLIST, not a grammar: it rejects only smuggled
  /// structure (`?`, `#`, `.`, control chars, `%`) — it deliberately does NOT
  /// assume the id's exact format, so a non-canonical-but-benign id (a short
  /// stub, a wrong prefix, a too-long string) is ACCEPTED and left to the SPA's
  /// own router to judge. This keeps the parser from breaking if the server's
  /// id scheme changes (ULID, nanoid, base64, …).
  func testAcceptsBenignNonCanonicalIds() {
    // None of these smuggle structure; the SPA is the authority on id validity.
    XCTAssertNotNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/conv_abc")!))
    XCTAssertNotNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/x")!))
    XCTAssertNotNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/not-a-uuid")!))
    XCTAssertNotNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/\(hex.dropLast())")!))
    // 31 hex
    XCTAssertNotNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/\(hex)g")!))
    // 33rd char not hex
    XCTAssertNotNil(DeepLink.parse(URL(string: "omnigent://localhost:8000/c/host_\(hex)")!))
    // wrong legacy prefix
  }
}
