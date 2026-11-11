"""Let native policy hooks reuse the server's "no policies apply" answer.

Native harnesses start a hook process for every tool call and prompt, and each
one asks the server's policy evaluate endpoint even when the session has no
policies. When the operator enables it, the server's no-policy ALLOW carries
:data:`ALLOW_CACHE_FIELD`: how many seconds that answer stays valid. The hook
that receives it records the expiry in the session's bridge directory, and the
hook command's shell guard (:func:`guarded_hook_command`) answers later events
itself until then, without starting Python or calling the server.

The cache file holds ``"<expiry> <generation> <mutations>"``, where the
generation is a random token. A hook reads it before the session identity and
its request (:func:`generation`), and :func:`record` stores the answer only if
the token is unchanged and no policy mutation is running. A session switch, a
policy mutation and any answer without a window each pick a new token, so an
answer the server gave before one of them cannot open the window. A missing or
unreadable file never matches a token a hook holds. When an invalidation cannot
take the lock, it leaves a revocation marker that the guard and :func:`record`
honor until a reset under the lock replaces the generation, so the lock holder
cannot restore its window.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import secrets
import shlex
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: _locked uses filelock instead.
    fcntl = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

#: Field on a no-policy ``POLICY_ACTION_ALLOW`` evaluate response: how many
#: seconds the caller may keep answering the session's hooks ALLOW without
#: asking again. Absent unless the operator enabled the allow cache.
ALLOW_CACHE_FIELD = "allow_cache_seconds"

#: Upper bound on an allow window, whatever the server or operator says, so a
#: bad setting cannot leave a session's policy changes unenforced for long.
MAX_ALLOW_CACHE_SECONDS = 300.0

_CACHE_FILENAME = "policy_allow_until"
_LOCK_FILENAME = "policy_allow_until.lock"
_REVOKED_FILENAME = "policy_allow_until.revoked"
_LOCK_TIMEOUT_S = 5.0

# POSIX shell run before the hook command; see guarded_hook_command. Only the
# first 2 KB is matched: patterns over a large payload are quadratic in dash.
# Without an open window the hook runs on its own stdin, so the shell reads
# nothing and starts no extra process; an expiry of 0 skips even `date`.
# outer succeeds when its argument, the text before a key, is just "{" once
# its strings are dropped and the objects and arrays it closes are removed,
# innermost first. Dropping strings first keeps brackets inside them from
# closing anything; text with a backslash escape defers, since an escaped
# quote would end a string early.
_GUARD_SCRIPT = r"""if [ ! -e @REVOKED@ ] && [ -r @CACHE@ ] && read -r allow_until _ < @CACHE@ &&
  [ "$allow_until" -gt 0 ] 2>/dev/null && [ "$(date +%s)" -lt "$allow_until" ]; then
  payload=$(cat)
  outer() {
    rest=$1
    case $rest in *\\*) return 1 ;; esac
    plain=
    while case $rest in *'"'*'"'*) true ;; *) false ;; esac; do
      plain=$plain${rest%%'"'*}
      rest=${rest#*'"'}
      rest=${rest#*'"'}
    done
    rest=$plain$rest
    while pre=${rest%%[]\}]*}; [ "$pre" != "$rest" ]; do
      case ${rest#"$pre"} in '}'*) open='{' ;; *) open='[' ;; esac
      case $pre in *"$open"*) ;; *) return 1 ;; esac
      rest=${pre%"$open"*}${rest#"$pre"?}
    done
    case ${rest#'{'} in "$rest"|*[{[]*) return 1 ;; esac
  }
  head=$(printf '%.2048s' "$payload")
  event=${head#*'"hook_event_name":'}
  tool=${head#*'"tool_name":'}
  outer "${head%%'"hook_event_name":'*}" || event=
  outer "${head%%'"tool_name":'*}" || tool=
  case ${event# } in
    '"UserPromptSubmit"'*) exit 0 ;;
    '"PreToolUse"'*|'"PostToolUse"'*) case ${tool# } in '"'[!'"']*) exit 0 ;; esac ;;
  esac
  printf '%s' "$payload" | { @COMMAND@; }
  exit
fi
{ @COMMAND@; }"""


def allow_cache_seconds(response: object) -> float | None:
    """
    Return the allow window an evaluate response grants, if any.

    :param response: Parsed ``EvaluationResponse`` body, e.g.
        ``{"result": "POLICY_ACTION_ALLOW", "allow_cache_seconds": 42.5}``.
    :returns: The window in seconds, capped at
        :data:`MAX_ALLOW_CACHE_SECONDS`, or ``None`` unless the response is
        an ALLOW with a finite positive window.
    """
    if not isinstance(response, dict) or response.get("result") != "POLICY_ACTION_ALLOW":
        return None
    seconds = response.get(ALLOW_CACHE_FIELD)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return min(float(seconds), MAX_ALLOW_CACHE_SECONDS)


def generation(bridge_dir: Path) -> str | None:
    """
    Return the cache generation, to pass to :func:`record` after the request.

    Read it before the session identity: a session switch picks a new
    generation, so an answer for the previous session cannot open the new
    session's window. A missing or unreadable cache file gets a new one.

    :param bridge_dir: The session's native bridge directory.
    :returns: An opaque token, e.g. ``"9f86d081884c7d65"``, or ``None`` when the
        cache file cannot be read or created; :func:`record` opens no window
        for ``None``.
    """
    current = _read_state(bridge_dir)[1]
    if current is not None and not _revoked(bridge_dir):
        return current
    try:
        with _locked(bridge_dir):
            _, current, mutations = _read_state(bridge_dir)
            if current is None or _revoked(bridge_dir):
                current = _new_generation()
                _write_state(bridge_dir, 0, current, mutations)
                _clear_revocation(bridge_dir)
    except OSError:
        _logger.warning("Could not create the policy allow cache", exc_info=True)
        return None
    return current


def record(
    bridge_dir: Path,
    generation_before: str | None,
    response: object,
    requested_at: float,
) -> None:
    """
    Open or close the allow window from one evaluate response.

    A response that grants a window opens it until *requested_at* plus the
    window, unless the generation moved since *generation_before* or a policy
    mutation is running. Any other response closes it under a new generation:
    the server either evaluated policies or did not vouch for the answer, and
    an older no-policy answer still in flight must not reopen it.

    :param bridge_dir: The session's native bridge directory.
    :param generation_before: :func:`generation` read before the request.
    :param response: Parsed ``EvaluationResponse`` body.
    :param requested_at: Wall-clock time (``time.time()``) the request was
        sent, so the window also covers the request's own latency.
    :returns: None. Failures are logged and leave hooks asking the server.
    """
    seconds = allow_cache_seconds(response)
    try:
        with _locked(bridge_dir):
            _, current, mutations = _read_state(bridge_dir)
            if seconds is None:
                _write_state(bridge_dir, 0, _new_generation(), mutations)
                _clear_revocation(bridge_dir)
            elif (
                current is not None
                and current == generation_before
                and not mutations
                and not _revoked(bridge_dir)
            ):
                _write_state(bridge_dir, int(requested_at + seconds), current, mutations)
    except OSError:
        _logger.warning("Could not update the policy allow cache", exc_info=True)


def invalidate(bridge_dir: Path) -> None:
    """
    Close the allow window and discard answers from requests in flight.

    :param bridge_dir: The session's native bridge directory.
    :returns: None.
    """
    _reset_generation(bridge_dir, mutation_delta=0)


@contextmanager
def policy_mutation(bridge_dir: Path) -> Iterator[None]:
    """
    Keep the allow window closed while a tool call may add a session policy.

    Answers the server gave before or during the call are discarded, so the
    first hook after it asks the server again.

    :param bridge_dir: The session's native bridge directory.
    :returns: A context manager wrapping the mutating call.
    """
    _reset_generation(bridge_dir, mutation_delta=1)
    try:
        yield
    finally:
        _reset_generation(bridge_dir, mutation_delta=-1)


def guarded_hook_command(bridge_dir: Path, command: str) -> str:
    """
    Wrap a policy hook command so an open allow window answers in the shell.

    While the window is open, prompts and tool events that name their tool
    exit 0 with no output, which is how the hooks express ALLOW. Only keys of
    the outer JSON object count: the text before a key, less its strings and
    the objects and arrays it closes, must be ``{`` alone. Closed metadata
    objects before the keys, such as Claude Code's ``"effort": {"level":
    "medium"}``, are allowed; a name nested in tool input is ignored, whatever
    the field order or brackets inside strings. A backslash escape before a
    key only costs the shortcut. A tool event without a top-level name is
    malformed and still reaches the server, which rejects it. Everything else
    runs *command* with the same stdin.

    :param bridge_dir: The session's native bridge directory.
    :param command: POSIX shell command that evaluates the hook, e.g.
        ``"/usr/bin/python3 -I -m omnigent.harnesses.codex_native.hook
        evaluate-policy --bridge-dir /tmp/b"``.
    :returns: The wrapped command, a multi-line shell script.
    """
    cache_path = shlex.quote(str(bridge_dir / _CACHE_FILENAME))
    revoked_path = shlex.quote(str(bridge_dir / _REVOKED_FILENAME))
    return (
        _GUARD_SCRIPT.replace("@CACHE@", cache_path)
        .replace("@REVOKED@", revoked_path)
        .replace("@COMMAND@", command)
    )


def _reset_generation(bridge_dir: Path, *, mutation_delta: int) -> None:
    """Close the window under a new generation, adjusting the mutation count."""
    try:
        with _locked(bridge_dir):
            mutations = max(_read_state(bridge_dir)[2] + mutation_delta, 0)
            _write_state(bridge_dir, 0, _new_generation(), mutations)
            _clear_revocation(bridge_dir)
    except OSError:
        _logger.warning("Could not invalidate the policy allow cache", exc_info=True)
        # The lock holder may still write a window; the marker outlives that.
        try:
            (bridge_dir / _REVOKED_FILENAME).touch()
        except OSError:
            with contextlib.suppress(OSError):
                (bridge_dir / _CACHE_FILENAME).unlink()


def _revoked(bridge_dir: Path) -> bool:
    """Return whether a failed invalidation has revoked the current window."""
    return (bridge_dir / _REVOKED_FILENAME).exists()


def _clear_revocation(bridge_dir: Path) -> None:
    """Drop the revocation once a new generation is written under the lock."""
    with contextlib.suppress(FileNotFoundError):
        (bridge_dir / _REVOKED_FILENAME).unlink()


def _new_generation() -> str:
    """Return a generation no earlier reader can hold."""
    return secrets.token_hex(8)


@contextmanager
def _locked(bridge_dir: Path) -> Iterator[None]:
    """
    Serialize cache updates across hook processes and relay threads.

    Every hook takes this lock, so on POSIX it is a plain ``flock`` rather than
    ``filelock``, whose import costs a few milliseconds per hook.

    :raises TimeoutError: If the lock stays held for the timeout; it is an
        ``OSError``, as is ``filelock.Timeout``.
    """
    if not bridge_dir.is_dir():
        # Creating the lock file would recreate a bridge directory that cleanup removed.
        raise FileNotFoundError(bridge_dir)
    path = bridge_dir / _LOCK_FILENAME
    if fcntl is None:
        from filelock import FileLock

        # A new lock object per use: some filelock releases share one object's
        # lock across threads, and the relay updates the cache from several.
        with FileLock(str(path), timeout=_LOCK_TIMEOUT_S):
            yield
        return
    # flock holds per open file description, so a descriptor of its own per
    # use also keeps the relay's threads apart.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"policy allow cache lock held for {_LOCK_TIMEOUT_S}s"
                    ) from None
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)


def _read_state(bridge_dir: Path) -> tuple[int, str | None, int]:
    """Return ``(expiry, generation, mutations)``; ``(0, None, 0)`` if unreadable."""
    try:
        text = (bridge_dir / _CACHE_FILENAME).read_text(encoding="utf-8")
        expiry, current, mutations = text.split()
        return int(expiry), current, int(mutations)
    except (OSError, ValueError):
        return 0, None, 0


def _write_state(bridge_dir: Path, expiry: int, current: str, mutations: int) -> None:
    """Replace the cache file atomically so the shell guard never reads a partial line."""
    path = bridge_dir / _CACHE_FILENAME
    tmp_path = path.with_name(f".{_CACHE_FILENAME}.{os.getpid()}.tmp")
    tmp_path.write_text(f"{expiry} {current} {mutations}\n", encoding="utf-8")
    os.replace(tmp_path, path)
