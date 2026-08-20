import Darwin
import Foundation

/// Minimal HTTP/1.1 server on an OS-assigned port (POSIX sockets), reachable
/// from the app via `http://localhost` on the iOS Simulator (shared loopback).
///
/// Shared between the integration tests (`OmnigentTests`) and the UI tests
/// (`OmnigentUITests`) — compiled into both test targets so neither needs its
/// own copy. Each target gets its own module, so the class is duplicated across
/// modules (unavoidable for cross-target sharing without a framework), but the
/// source is maintained in one place.
final class MockHTTPServer {
  private(set) var port: Int = 0
  private let handler: (String, String) -> (Int, [String: String], Data)
  private var listenFD: Int32 = -1
  private var source: DispatchSourceRead?
  private let queue = DispatchQueue(label: "omnigent.mock-http-server")

  /// - Parameter handler: Receives `(HTTPMethod, path)` and returns
  ///   `(statusCode, headerField -> value, body)`. Header names are lowercased
  ///   on the way out.
  init(handler: @escaping (String, String) -> (Int, [String: String], Data)) throws {
    self.handler = handler
    try start()
  }

  deinit {
    source?.cancel()
    if listenFD >= 0 { close(listenFD) }
  }

  private func start() throws {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { throw URLError(.cannotConnectToHost) }
    listenFD = fd

    var opt: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &opt, socklen_t(MemoryLayout<Int32>.size))

    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = 0
    addr.sin_addr.s_addr = 0
    let bindResult = withUnsafePointer(to: &addr) {
      $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
      }
    }
    guard bindResult == 0 else { throw URLError(.cannotFindHost) }

    var actual = sockaddr_in()
    var len = socklen_t(MemoryLayout<sockaddr_in>.size)
    let nameResult = withUnsafeMutablePointer(to: &actual) {
      $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        getsockname(fd, $0, &len)
      }
    }
    guard nameResult == 0 else { throw URLError(.cannotFindHost) }
    port = Int(UInt16(bigEndian: actual.sin_port))

    guard listen(fd, 16) == 0 else { throw URLError(.cannotConnectToHost) }

    let flags = fcntl(fd, F_GETFL, 0)
    _ = fcntl(fd, F_SETFL, flags | O_NONBLOCK)

    source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: queue)
    source?.setEventHandler { [weak self] in self?.acceptConnections() }
    source?.resume()
  }

  private func acceptConnections() {
    while true {
      let conn = accept(listenFD, nil, nil)
      if conn < 0 { break }
      handleConnection(conn)
    }
  }

  private func handleConnection(_ fd: Int32) {
    defer { close(fd) }

    var buffer = Data()
    var tmp = [UInt8](repeating: 0, count: 4096)
    let terminator = Data([0x0D, 0x0A, 0x0D, 0x0A])
    let deadline = Date().addingTimeInterval(5)
    while buffer.range(of: terminator) == nil, Date() < deadline {
      let n = read(fd, &tmp, tmp.count)
      if n <= 0 {
        usleep(1_000)
        continue
      }
      buffer.append(tmp, count: n)
      if buffer.count > 64 * 1024 { break }
    }

    let request = String(data: buffer, encoding: .utf8) ?? ""
    let firstLine = request.split(separator: "\r\n", maxSplits: 1).first.map(String.init) ?? ""
    let parts = firstLine.split(separator: " ")
    let method = parts.first.map(String.init)?.uppercased() ?? "GET"
    let path = parts.count > 1 ? String(parts[1]) : "/"

    let (status, headers, body) = handler(method, path)
    writeResponse(
      fd: fd, status: status, headers: headers, body: body, includeBody: method != "HEAD")
  }

  private func writeResponse(
    fd: Int32, status: Int, headers: [String: String], body: Data, includeBody: Bool
  ) {
    let reason: String
    switch status {
    case 200: reason = "OK"
    case 302: reason = "Found"
    default: reason = "OK"
    }
    var head = "HTTP/1.1 \(status) \(reason)\r\n"
    var allHeaders = headers
    if allHeaders["content-length"] == nil {
      allHeaders["content-length"] = "\(body.count)"
    }
    allHeaders["connection"] = "close"
    for (key, value) in allHeaders {
      head += "\(key): \(value)\r\n"
    }
    head += "\r\n"

    var out = Data(head.utf8)
    if includeBody { out.append(body) }
    out.withUnsafeBytes { raw in
      var sent = 0
      guard let ptr = raw.baseAddress else { return }
      while sent < out.count {
        let n = write(fd, ptr.advanced(by: sent), out.count - sent)
        if n <= 0 { break }
        sent += n
      }
    }
  }
}
