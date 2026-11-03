"""Network-free attribution of successful glab and GitLab MCP merge-request writes."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePath
from urllib.parse import unquote

from omnigent.git_providers import EnvInstances
from omnigent.git_providers.gitlab import GitLabProvider, instance_authority
from omnigent.runner.git_providers import ShellPrOp, ShellSegment
from omnigent.runner.git_providers.tool_output import result_objects
from omnigent.runner.session_prs import PullRequestRef

_WRITES = {"create", "update", "merge", "close", "reopen", "approve", "revoke", "rebase", "delete"}
_MCP_WRITES = {
    "create_merge_request",
    "update_merge_request",
    "merge_merge_request",
    "approve_merge_request",
    "unapprove_merge_request",
    "rebase_merge_request",
    "delete_merge_request",
}
_VALUE_FLAGS = {
    "--repo",
    "-R",
    "--hostname",
    "--title",
    "-t",
    "--description",
    "-d",
    "--description-file",
    "--attach",
    "--head",
    "--related-issue",
    "--template",
    "--source-branch",
    "-s",
    "--target-branch",
    "-b",
    "--assignee",
    "-a",
    "--reviewer",
    "--label",
    "-l",
    "--unlabel",
    "-u",
    "--milestone",
    "-m",
    "--output",
    "-F",
    "--sha",
    "--message",
    "--squash-message",
    "--method",
    "-X",
    "--field",
    "--raw-field",
    "--input",
    "--header",
    "-H",
    "--jq",
    "--page",
    "--per-page",
    "-p",
    "-P",
}
_SWITCHES = {
    "--yes",
    "-y",
    "--draft",
    "--ready",
    "--remove-source-branch",
    "-r",
    "--squash",
    "--auto-merge",
    "--when-pipeline-succeeds",
    "--no-edit",
    "--fill",
    "--fill-commit-body",
    "--push",
    "--web",
    "--include",
    "-i",
    "--paginate",
    "--lock-discussion",
    "--unlock-discussion",
    "--unassign",
    "--wip",
    "--signoff",
    "--squash-before-merge",
    "--allow-collaboration",
    "--copy-issue-labels",
    "--create-source-branch",
    "--no-editor",
    "--recover",
    "--help",
    "-h",
    "-w",
}


def _arguments(tokens: list[str]) -> tuple[list[str], dict[str, str]] | None:
    positional, flags = [], {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        flag, equals, value = token.partition("=")
        api = positional[:1] == ["api"]
        merge = positional[:2] in (["mr", "merge"], ["mr", "accept"])
        takes_value = flag in _VALUE_FLAGS or (api and flag == "-f")
        if (flag == "-f" and not api) or (merge and flag in {"-d", "-s"}):
            takes_value = False
        if takes_value:
            if not equals:
                index += 1
                if index == len(tokens):
                    return None
                value = tokens[index]
            flags[flag] = value
        elif flag in _SWITCHES or flag == "-f" or (merge and flag in {"-d", "-s"}):
            flags[flag] = value if equals else "true"
        elif len(token) > 2 and (token[:2] in _VALUE_FLAGS or (api and token[:2] == "-f")):
            flags[token[:2]] = token[2:]
        elif token.startswith("-"):
            return None
        else:
            positional.append(token)
        index += 1
    return positional, flags


def reference(value: object) -> PullRequestRef | None:
    """Read only a trusted GitLab MR URL, excluding other providers and body text."""
    if not isinstance(value, str):
        return None
    parsed = GitLabProvider().parse_pr_url(value, EnvInstances())
    return PullRequestRef(**vars(parsed)) if parsed else None


def _target(project: object, iid: object, host: str | None) -> PullRequestRef | None:
    if not isinstance(project, str) or not isinstance(iid, (str, int)) or isinstance(iid, bool):
        return None
    project = unquote(project).removesuffix(".git").strip("/")
    remote = GitLabProvider().parse_remote_url(project, EnvInstances())
    if remote:
        host, project = remote.host, remote.repository
    else:
        first, separator, rest = project.partition("/")
        if separator and GitLabProvider().matches_host(first, EnvInstances()):
            host, project = first, rest
    if host is None:
        return None
    return reference(f"https://{host}/{project}/-/merge_requests/{str(iid).lstrip('!')}")


def _shell_host(segment: ShellSegment, hostname: str | None) -> str | None:
    if hostname is not None:
        return instance_authority(hostname)
    host = ""
    for token in segment.raw_tokens[: -len(segment.invocation_tokens)]:
        if token.startswith("GITLAB_HOST="):
            host = token.split("=", 1)[1]
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token) or PurePath(token).name == "env":
            continue
        else:
            # Wrapper options may clear or replace the inherited environment.
            return None
    return instance_authority(host) if host else None


def _push_output(text: str) -> list[PullRequestRef]:
    """Read GitLab's existing-MR banner, never push hints or arbitrary URLs."""
    if re.search(r"(?m)^(?:error: failed to push some refs|fatal:)", text):
        return []
    refs = []
    for match in re.finditer(
        r"(?m)^remote:\s*View merge request for [^\n]+:\s*\nremote:[ \t]+(https?://\S+)[ \t\r]*$",
        text,
    ):
        if re.search(r"/-/merge_requests/[1-9][0-9]*/?$", match[1]) and (
            ref := reference(match[1])
        ):
            refs.append(ref)
    return refs


def _git_push(tokens: Sequence[str]) -> ShellPrOp | None:
    """Recognize explicit MR push options without resolving aliases or running git."""
    args = list(tokens[1:])
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag in {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}:
            if not args:
                return None
            args.pop(0)
        elif flag in {"--no-pager", "--no-optional-locks"} or flag.startswith(
            ("--git-dir=", "--work-tree=", "--namespace=", "--config-env=", "-C", "-c")
        ):
            continue
        else:
            return None
    if not args or args.pop(0) != "push":
        return None
    options = []
    while args:
        arg = args.pop(0)
        if arg == "--":
            break
        if arg in {"-o", "--push-option"}:
            if not args:
                return ShellPrOp(False, False, None, True)
            options.append(args.pop(0))
        elif arg.startswith("--push-option="):
            options.append(arg.split("=", 1)[1])
        elif arg.startswith("-o"):
            options.append(arg[2:])
        elif arg in {
            "-u",
            "--set-upstream",
            "-f",
            "--force",
            "--force-with-lease",
            "--force-if-includes",
            "--no-verify",
            "--follow-tags",
            "--atomic",
            "--porcelain",
            "-v",
            "--verbose",
            "-q",
            "--quiet",
        } or arg.startswith("--force-with-lease="):
            continue
        elif arg.startswith("-"):
            return ShellPrOp(False, False, None, True)
    mutations = {
        "merge_request.create",
        "merge_request.title",
        "merge_request.description",
        "merge_request.target",
        "merge_request.target_project",
        "merge_request.draft",
        "merge_request.label",
        "merge_request.unlabel",
        "merge_request.assign",
        "merge_request.unassign",
        "merge_request.merge_when_pipeline_succeeds",
        "merge_request.remove_source_branch",
        "merge_request.squash",
    }
    tracks = any(option.split("=", 1)[0] in mutations for option in options)
    return ShellPrOp(tracks, "merge_request.create" in options, None, False, _push_output)


def shell_pr_operations(segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
    """Describe MR reads too, so mixed command output cannot masquerade as a write."""
    operations = []
    pushes = []
    for segment in segments:
        if PurePath(segment.invocation_tokens[0]).name == "git":
            if op := _git_push(segment.invocation_tokens):
                pushes.append(op)
            continue
        if PurePath(segment.invocation_tokens[0]).name != "glab":
            continue
        tokens = list(segment.invocation_tokens[1:])
        parsed = _arguments(tokens)
        if parsed is None:
            operations.append(ShellPrOp(False, False, None, True))
            continue
        positional, flags = parsed
        if not positional or positional[0] not in {"mr", "api"}:
            continue
        host = _shell_host(segment, flags.get("--hostname"))
        project = flags.get("--repo", flags.get("-R"))
        target, tracks, creates = None, False, False
        if positional[0] == "mr" and len(positional) > 1:
            action = {"new": "create", "accept": "merge"}.get(positional[1], positional[1])
            tracks, creates = action in _WRITES, action == "create"
            if len(positional) > 2 and not creates:
                target = reference(positional[2]) or _target(project, positional[2], host)
        elif positional[0] == "api" and len(positional) > 1:
            endpoint = positional[1].split("?", 1)[0].strip("/")
            match = re.fullmatch(
                r"projects/([^/]+)/merge_requests(?:/([1-9][0-9]*)(?:/(merge|approve|unapprove|rebase))?)?",
                endpoint,
            )
            method = flags.get("--method", flags.get("-X")) or (
                "POST"
                if any(k in flags for k in ("--field", "-f", "--raw-field", "-F", "--input"))
                else "GET"
            )
            if match:
                creates = match[2] is None and method.upper() == "POST"
                tracks = creates or (
                    match[2] is not None and method.upper() in {"PUT", "POST", "DELETE"}
                )
                target = _target(match[1], match[2], host) if match[2] else None
        if any(flag in flags for flag in ("--help", "-h")) or (
            creates and any(flag in flags for flag in ("--web", "-w"))
        ):
            tracks, creates = False, False
        operations.append(ShellPrOp(tracks, creates, target, "--jq" in flags))
    # Ordinary pushes are not PR operations. With an MR push, a second ordinary
    # push makes the shared remote output ambiguous.
    if any(op.tracks for op in pushes):
        operations.extend(pushes)
    return operations


def pr_from_object(obj: Mapping[str, object]) -> PullRequestRef | None:
    """A GitLab result identifies an MR by web_url and its project-local IID."""
    ref = reference(obj.get("web_url"))
    return ref if ref is not None and str(obj.get("iid")) == str(ref.number) else None


def mcp_prs(
    tool_name: str, arguments: dict[str, object], result: object
) -> tuple[list[PullRequestRef], bool] | None:
    """Track explicit MR mutation tools, never notes, reads or URLs quoted in content."""
    name = tool_name.rsplit("__", 1)[-1].removeprefix("gitlab_")
    if name not in _MCP_WRITES:
        return None
    created = name == "create_merge_request"
    configured_host = arguments.get("hostname", arguments.get("host"))
    host = instance_authority(configured_host) if isinstance(configured_host, str) else None
    if configured_host and host is None:
        return [], created
    project = arguments.get("project_id", arguments.get("project"))
    if created and arguments.get("target_project_id") is not None:
        project = arguments["target_project_id"]
    iid = arguments.get("merge_request_iid", arguments.get("iid"))
    target = _target(project, iid, host) if not created else None
    references = []
    for obj in result_objects(result):
        for candidate in (obj, obj.get("merge_request")):
            if not isinstance(candidate, dict):
                continue
            ref = pr_from_object(candidate)
            if ref is None or (target is not None and ref.url != target.url):
                continue
            if configured_host and ref.host != host:
                continue
            if isinstance(project, str) and not project.isdigit():
                expected = _target(project, ref.number, host if configured_host else ref.host)
                if expected is None or expected.url != ref.url:
                    continue
            if (
                isinstance(project, (str, int))
                and str(project).isdigit()
                and str(candidate.get("project_id")) != str(project)
            ):
                continue
            if iid is not None and not created and str(ref.number) != str(iid):
                continue
            references.append(ref)
    return references or ([target] if target else []), created
