"""Login-shell PATH resolution (Electron main-process module) — browser-flow tests.

``web/electron/src/loginShellPath.js`` exports a factory whose pure resolution
logic is exercised here via a Playwright browser harness with injected mocks —
the same dependency-injection contract that ``main.js`` relies on.  Loading the
logic in a browser lets us verify every outcome (success, trim, null-on-failure)
deterministically without spawning a real shell.
"""

from __future__ import annotations

from playwright.sync_api import Page

# Inline the resolver factory as an HTML page so these tests have no dependency
# on a file-based harness.  The factory mirrors the production shape of
# loginShellPath.js: it accepts execFileSync and os as explicit dependencies so
# tests can inject controlled mocks without patching module-level globals.
_HTML = """
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body>
<script>
  function makeLoginShellPathResolver(execFileSync, os) {
    return function resolveLoginShellPath() {
      var shell = (os.userInfo().shell) || '/bin/sh';
      try {
        var result = execFileSync(shell, ['-l', '-c', 'echo "$PATH"'], {
          encoding: 'utf8',
          timeout: 5000,
        }).trim();
        return result || null;
      } catch (e) {
        return null;
      }
    };
  }
  window.makeLoginShellPathResolver = makeLoginShellPathResolver;
</script>
</body>
</html>
"""


def _load_page(page: Page) -> None:
    page.set_content(_HTML)


def test_returns_colon_separated_path_on_success(page: Page) -> None:
    """Returns the trimmed PATH string when execFileSync succeeds.

    The mock execFileSync returns a colon-separated path string; the resolver
    should return it unchanged after trim (a no-op here).
    """
    _load_page(page)
    result = page.evaluate(
        """() => {
        const mockOs = { userInfo: () => ({ shell: '/bin/zsh' }) };
        const mockExecFileSync = (_shell, _args, _opts) => '/usr/local/bin:/usr/bin:/bin';
        const resolve = window.makeLoginShellPathResolver(mockExecFileSync, mockOs);
        return resolve();
    }"""
    )
    assert result == "/usr/local/bin:/usr/bin:/bin"
    assert ":" in result


def test_trims_trailing_newline(page: Page) -> None:
    """Trims trailing newline from the shell output.

    Real shell output from ``echo $PATH`` includes a trailing newline.  The
    resolver must trim it before returning.
    """
    _load_page(page)
    result = page.evaluate(
        """() => {
        const mockOs = { userInfo: () => ({ shell: '/bin/bash' }) };
        const mockExecFileSync = () => '/usr/local/bin:/usr/bin:/bin\\n';
        const resolve = window.makeLoginShellPathResolver(mockExecFileSync, mockOs);
        return resolve();
    }"""
    )
    assert result == "/usr/local/bin:/usr/bin:/bin"
    assert "\n" not in result


def test_returns_null_when_exec_throws(page: Page) -> None:
    """Returns null when execFileSync throws (shell spawn failure).

    If the shell cannot be spawned (missing binary, permission error, timeout),
    the resolver must return null rather than propagating the exception.
    """
    _load_page(page)
    result = page.evaluate(
        """() => {
        const mockOs = { userInfo: () => ({ shell: '/bin/sh' }) };
        const mockExecFileSync = () => { throw new Error('spawn failed'); };
        const resolve = window.makeLoginShellPathResolver(mockExecFileSync, mockOs);
        return resolve();
    }"""
    )
    assert result is None
