import Foundation

@MainActor
protocol DatabricksSessionCreating {
  func create(context: DatabricksWebContext, tokens: DatabricksOAuthTokens) async throws
    -> DatabricksWebSession
}

struct DatabricksWebSession {
  let pageURL: URL
  let cookies: [HTTPCookie]
  let allowedOrigins: Set<String>
  let configuration: DatabricksOAuthConfiguration
  let workspaceID: String?

  private func pageScope(for url: URL) -> DatabricksCredentialScope? {
    guard
      let scope = try? DatabricksCredentialScope(workspaceURL: url, configuration: configuration),
      allowedOrigins.contains(scope.workspaceOrigin.absoluteString),
      scope.workspaceID == nil || workspaceID == nil || scope.workspaceID == workspaceID
    else { return nil }
    return scope
  }

  func isAuthenticationURL(_ url: URL) -> Bool {
    pageScope(for: url) != nil && Self.isLoginPath(url.path)
  }

  func isSignOutURL(_ url: URL) -> Bool {
    guard pageScope(for: url) != nil else { return false }
    if url.path == "/auth/logout" || url.path == "/logout" { return true }
    return url.path == "/login.html"
      && URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.contains {
        $0.name == "logout" && $0.value == "1"
      } == true
  }

  func navigationURL(for url: URL) -> URL? {
    guard let scope = pageScope(for: url), !Self.isLoginPath(url.path) else { return nil }
    var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
    components.scheme = "https"
    components.host = scope.workspaceOrigin.host
    components.port = nil
    if scope.workspaceOrigin.absoluteString == pageURL.omnigentOrigin, let workspaceID,
      scope.workspaceID == nil
    {
      components.queryItems =
        (components.queryItems ?? []) + [URLQueryItem(name: "o", value: workspaceID)]
    }
    return components.url
  }

  static func isLoginPath(_ path: String) -> Bool {
    path == "/login" || path == "/login.html" || path.hasPrefix("/login/")
      || path.hasPrefix("/oidc/")
      || path == "/auth/login" || path == "/auth/callback" || path == "/auth/session/create"
  }
}

@MainActor
struct DatabricksSessionClient: DatabricksSessionCreating {
  private static let transport = DatabricksOAuthClient.makeSession()
  private let session: URLSession

  init(session: URLSession? = nil) { self.session = session ?? Self.transport }

  func create(context: DatabricksWebContext, tokens: DatabricksOAuthTokens) async throws
    -> DatabricksWebSession
  {
    guard tokens.isValid, !DatabricksWebSession.isLoginPath(context.pageURL.path) else {
      throw DatabricksSessionError.invalidResponse
    }
    if let issuer = tokens.issuer, issuer.url.path.hasPrefix("/oidc/accounts/"),
      context.scope.workspaceID == nil
    {
      throw DatabricksSessionError.workspaceRequired
    }
    let startOrigin = Self.exchangeOrigin(context: context, tokens: tokens)
    let original = URLComponents(url: context.pageURL, resolvingAgainstBaseURL: false)!
    let nextPath =
      original.percentEncodedPath + (original.percentEncodedQuery.map { "?" + $0 } ?? "")
    var endpoint = URLComponents(
      url: startOrigin.appendingPathComponent("auth/session/create"), resolvingAgainstBaseURL: false
    )!
    endpoint.queryItems = [URLQueryItem(name: "next_url", value: nextPath)]
    var currentURL = endpoint.url!
    var jar: [HTTPCookie] = []
    var updates: [HTTPCookie] = []
    let pageScope = try DatabricksCredentialScope(
      workspaceURL: context.pageURL, configuration: context.configuration)
    var workspaceID = context.scope.workspaceID ?? pageScope.workspaceID
    for hop in 0...8 {
      try Task.checkCancellation()
      var request = URLRequest(url: currentURL)
      request.timeoutInterval = 20
      if hop == 0 {
        request.setValue("Bearer " + tokens.accessToken, forHTTPHeaderField: "Authorization")
      }
      let applicable = jar.filter { Self.cookie($0, appliesTo: currentURL) }.sorted {
        $0.path.count > $1.path.count
      }
      if !applicable.isEmpty {
        request.setValue(
          HTTPCookie.requestHeaderFields(with: applicable)["Cookie"], forHTTPHeaderField: "Cookie")
      }
      let response: HTTPURLResponse
      do {
        let (_, rawResponse) = try await session.data(for: request)
        guard let http = rawResponse as? HTTPURLResponse, http.url == currentURL else {
          throw DatabricksSessionError.invalidResponse
        }
        response = http
      } catch {
        if Task.isCancelled || (error as? URLError)?.code == .cancelled {
          throw CancellationError()
        }
        if let error = error as? DatabricksSessionError { throw error }
        throw DatabricksSessionError.networkUnavailable
      }
      try Task.checkCancellation()
      let received = try Self.cookies(from: response)
      for cookie in received {
        updates.removeAll {
          $0.name == cookie.name && $0.domain == cookie.domain && $0.path == cookie.path
        }
        updates.append(cookie)
        jar.removeAll {
          $0.name == cookie.name && $0.domain == cookie.domain && $0.path == cookie.path
        }
        if cookie.expiresDate.map({ $0 > Date() }) ?? true,
          cookie.name != "DBAUTH" || !cookie.value.isEmpty
        {
          jar.append(cookie)
        }
      }
      if [301, 302, 303, 307, 308].contains(response.statusCode) {
        guard hop < 8, let location = response.value(forHTTPHeaderField: "Location"),
          let next = URL(string: location, relativeTo: currentURL)?.absoluteURL,
          let nextScope = try? DatabricksCredentialScope(
            workspaceURL: next, configuration: context.configuration)
        else { throw DatabricksSessionError.unsafeRedirect }
        if let id = nextScope.workspaceID {
          guard workspaceID == nil || workspaceID == id else {
            throw DatabricksSessionError.workspaceChanged
          }
          workspaceID = id
        }
        var components = URLComponents(url: next, resolvingAgainstBaseURL: false)!
        components.fragment = nil
        components.scheme = "https"
        components.host = nextScope.workspaceOrigin.host
        components.port = nil
        currentURL = components.url!
        continue
      }
      guard (200...299).contains(response.statusCode) else {
        throw DatabricksSessionError.rejected(response.statusCode)
      }
      let knownOrigin =
        currentURL.omnigentOrigin == context.scope.workspaceOrigin.absoluteString
        || currentURL.omnigentOrigin == startOrigin.omnigentOrigin
      let atAppPath =
        currentURL.path == context.pageURL.path
        || currentURL.path == WorkspaceURLExpander.workspaceUIPath
        || currentURL.path == "/auth/session/create"
      guard atAppPath || (knownOrigin && (currentURL.path.isEmpty || currentURL.path == "/")) else {
        throw DatabricksSessionError.unexpectedLanding
      }
      var page = URLComponents(url: currentURL, resolvingAgainstBaseURL: false)!
      page.percentEncodedPath = original.percentEncodedPath
      page.percentEncodedQuery = original.percentEncodedQuery
      page.percentEncodedFragment = original.percentEncodedFragment
      if context.scope.workspaceID == nil, let workspaceID {
        page.queryItems = (page.queryItems ?? []) + [URLQueryItem(name: "o", value: workspaceID)]
      }
      let pageURL = page.url!
      guard
        jar.contains(where: {
          $0.name == "DBAUTH" && !$0.value.isEmpty && Self.cookie($0, appliesTo: pageURL)
        })
      else {
        throw DatabricksSessionError.missingCookie
      }
      return DatabricksWebSession(
        pageURL: pageURL, cookies: updates,
        allowedOrigins: Set([
          context.scope.workspaceOrigin.absoluteString, startOrigin.omnigentOrigin!,
          context.pageURL.omnigentOrigin!, pageURL.omnigentOrigin!,
        ]),
        configuration: context.configuration, workspaceID: workspaceID)
    }
    throw DatabricksSessionError.unsafeRedirect
  }

  private static func exchangeOrigin(context: DatabricksWebContext, tokens: DatabricksOAuthTokens)
    -> URL
  {
    guard let issuer = tokens.issuer, issuer.url.path == "/oidc",
      let issuerScope = try? DatabricksCredentialScope(
        workspaceURL: issuer.url, configuration: context.configuration)
    else { return context.scope.workspaceOrigin }
    return issuerScope.workspaceOrigin
  }

  static func cookies(from response: HTTPURLResponse) throws -> [HTTPCookie] {
    guard let url = response.url, url.scheme?.lowercased() == "https",
      let host = url.host?.lowercased()
    else {
      throw DatabricksSessionError.invalidResponse
    }
    let headers = response.allHeaderFields.reduce(into: [String: String]()) { result, item in
      if let key = item.key as? String, let value = item.value as? String { result[key] = value }
    }
    let cookies = HTTPCookie.cookies(withResponseHeaderFields: headers, for: url)
    for cookie in cookies {
      let domain = Self.domain(of: cookie)
      guard ServerAuthentication(host: domain) == .databricksWorkspace,
        host == domain || (cookie.domain.hasPrefix(".") && host.hasSuffix("." + domain)),
        cookie.path.hasPrefix("/"),
        !cookie.name.isEmpty,
        cookie.name.rangeOfCharacter(from: .controlCharacters) == nil,
        cookie.value.rangeOfCharacter(from: .controlCharacters) == nil
      else { throw DatabricksSessionError.unsafeCookie }
      let isDeletion = cookie.value.isEmpty || (cookie.expiresDate.map { $0 <= Date() } ?? false)
      if cookie.name == "DBAUTH", !isDeletion, !(cookie.isSecure && cookie.isHTTPOnly) {
        throw DatabricksSessionError.unsafeCookie
      }
    }
    return cookies
  }

  private static func domain(of cookie: HTTPCookie) -> String {
    let domain = cookie.domain.lowercased()
    return domain.hasPrefix(".") ? String(domain.dropFirst()) : domain
  }

  static func cookie(_ cookie: HTTPCookie, appliesTo url: URL) -> Bool {
    guard let host = url.host?.lowercased() else { return false }
    let domain = Self.domain(of: cookie)
    let matchesHost =
      host == domain || (cookie.domain.hasPrefix(".") && host.hasSuffix("." + domain))
    let path = url.path.isEmpty ? "/" : url.path
    let matchesPath =
      path == cookie.path
      || path.hasPrefix(cookie.path.hasSuffix("/") ? cookie.path : cookie.path + "/")
    return matchesHost && matchesPath && (!cookie.isSecure || url.scheme == "https")
      && (cookie.expiresDate.map { $0 > Date() } ?? true)
  }
}

enum DatabricksSessionError: Error, Equatable, LocalizedError {
  case invalidResponse, unsafeRedirect, workspaceChanged, unsafeCookie, missingCookie,
    unexpectedLanding
  case networkUnavailable
  case rejected(Int)
  case cancelled, presentationUnavailable, credentialsChanged, workspaceRequired
  case reauthenticationRequired, recoveryExhausted, signOutIncomplete

  var errorDescription: String? {
    switch self {
    case .invalidResponse: "Databricks returned an invalid session response."
    case .unsafeRedirect: "Databricks returned an unsupported session redirect."
    case .workspaceChanged: "The workspace changed. Select the intended workspace and reconnect."
    case .unsafeCookie:
      "Databricks returned a session cookie with an unsupported scope or security attributes."
    case .missingCookie: "Databricks did not create a session cookie for this workspace."
    case .unexpectedLanding: "Databricks did not finish loading the workspace session."
    case .networkUnavailable: "Could not reach Databricks. Please try again."
    case .rejected(let status): "Databricks rejected session creation (HTTP \(status))."
    case .reauthenticationRequired: "Your workspace session expired. Sign in again to continue."
    case .recoveryExhausted: "The workspace session could not be restored. Reconnect to try again."
    case .signOutIncomplete:
      "Sign-out could not finish. Reconnect to retry cleanup before signing in."
    case .workspaceRequired:
      "Enter a workspace-specific URL, including its o parameter when required."
    case .credentialsChanged: "Workspace credentials changed. Reconnect to continue."
    case .cancelled: "Sign-in was cancelled. You can try again."
    case .presentationUnavailable: "Could not present the sign-in browser. Please try again."
    }
  }
}
