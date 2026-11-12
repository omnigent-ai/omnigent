import CryptoKit
import Foundation
import Security

/// PKCE (RFC 7636) and form-encoding helpers shared by the native OAuth flows.
enum OAuthSupport {
  /// 32 random bytes as base64url: a 43-character verifier or state; nil if the RNG fails.
  static func randomValue() -> String? {
    var bytes = [UInt8](repeating: 0, count: 32)
    guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
      return nil
    }
    return base64URL(Data(bytes))
  }

  /// The S256 code challenge for `verifier`.
  static func challenge(for verifier: String) -> String {
    base64URL(Data(SHA256.hash(data: Data(verifier.utf8))))
  }

  /// An `application/x-www-form-urlencoded` body that escapes everything but unreserved characters.
  static func formBody(_ fields: [(String, String)]) -> Data {
    let unreserved = CharacterSet(
      charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    return Data(
      fields.map {
        "\($0.0.addingPercentEncoding(withAllowedCharacters: unreserved)!)=\($0.1.addingPercentEncoding(withAllowedCharacters: unreserved)!)"
      }.joined(separator: "&").utf8)
  }

  private static func base64URL(_ data: Data) -> String {
    data.base64EncodedString().replacingOccurrences(of: "+", with: "-")
      .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
  }
}
