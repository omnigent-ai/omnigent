#!/usr/bin/env python3
"""Send a Slack alert when a configured VIP acts in the Omnigent repository."""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import re
import urllib.error
import urllib.request


class SlackPostError(RuntimeError):
    """A Slack post failed without exposing the secret webhook URL."""


def vip_logins(mapping: str) -> set[str]:
    """Extract GitHub logins from the top-level ``vip`` YAML mapping."""
    in_vip = False
    logins: set[str] = set()
    for line in mapping.splitlines():
        if not in_vip:
            if re.fullmatch(r"vip:\s*(?:#.*)?", line):
                in_vip = True
            continue
        if line and not line[0].isspace():
            break
        match = re.match(r"^  ([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})):\s*", line)
        if match:
            logins.add(match.group(1).lower())
    return logins


def _target(payload: dict) -> tuple[str, str, str]:
    issue = payload.get("issue") or {}
    pull = payload.get("pull_request") or {}
    discussion = payload.get("discussion") or {}
    if "pull_request" in payload:
        return "PR", str(pull.get("number", "")), str(pull.get("title", ""))
    if issue:
        kind = "PR" if "pull_request" in issue else "issue"
        return kind, str(issue.get("number", "")), str(issue.get("title", ""))
    if discussion:
        return "discussion", str(discussion.get("number", "")), str(discussion.get("title", ""))
    return "repository", "", ""


def _url(payload: dict) -> str:
    for key in ("comment", "review", "pull_request", "issue", "discussion"):
        value = payload.get(key) or {}
        if value.get("html_url"):
            return str(value["html_url"])
    return str((payload.get("repository") or {}).get("html_url", ""))


def _activity(event_name: str, payload: dict) -> str:
    action = str(payload.get("action", "updated")).replace("_", " ")
    if event_name == "issue_comment":
        return {
            "created": "commented on",
            "edited": "edited a comment on",
            "deleted": "deleted a comment on",
        }.get(action, f"{action} a comment on")
    if event_name == "pull_request_review_comment":
        return {
            "created": "left a review comment on",
            "edited": "edited a review comment on",
            "deleted": "deleted a review comment on",
        }.get(action, f"{action} a review comment on")
    if event_name == "pull_request_review":
        state = str((payload.get("review") or {}).get("state", "review")).lower()
        if action == "submitted":
            article = "an" if state[:1] in "aeiou" else "a"
            return f"submitted {article} {state} review on"
        return f"{action} a review on"
    if event_name == "discussion_comment":
        return {
            "created": "commented on",
            "edited": "edited a comment on",
            "deleted": "deleted a comment on",
        }.get(action, f"{action} a comment on")
    if event_name == "workflow_dispatch":
        return "triggered a test alert for"
    return f"{action}"


def build_alert(
    mapping: str,
    event_name: str,
    payload: dict,
    *,
    actor_override: str | None = None,
) -> str | None:
    """Build Slack text for a VIP-authored event, or return ``None``."""
    actor = actor_override or str((payload.get("sender") or {}).get("login", ""))
    if not actor or actor.lower() not in vip_logins(mapping):
        return None

    kind, number, title = _target(payload)
    if event_name == "workflow_dispatch":
        kind, number, title = "notification pipeline", "", ""
    repo = str((payload.get("repository") or {}).get("full_name", "omnigent-ai/omnigent"))
    label = f"{kind} #{number}" if number else kind
    if title:
        label += f": {title[:180]}"
    label = html.escape(label.replace("|", "¦"))
    target_url = html.escape(_url(payload), quote=True)
    linked_label = f"<{target_url}|{label}>" if target_url else label
    return (
        f":rotating_light: *VIP GitHub activity* — *{html.escape(actor)}* "
        f"{_activity(event_name, payload)} {linked_label} in `{html.escape(repo)}`"
    )


def post_to_slack(webhook_url: str, text: str) -> None:
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise SlackPostError(f"Slack returned HTTP {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise SlackPostError(f"could not reach Slack: {exc.reason}") from None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapping", type=pathlib.Path, required=True)
    parser.add_argument("--event", type=pathlib.Path, required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--actor")
    parser.add_argument("--slack-webhook")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    mapping = args.mapping.read_text(encoding="utf-8")
    payload = json.loads(args.event.read_text(encoding="utf-8"))
    text = build_alert(mapping, args.event_name, payload, actor_override=args.actor)
    if text is None:
        actor = args.actor or str((payload.get("sender") or {}).get("login", "unknown"))
        print(f"{actor} is not in the VIP mapping; no alert sent.")
        return 0
    if args.dry_run:
        print(text)
        return 0
    if not args.slack_webhook:
        raise SystemExit("VIP_ACTIVITY_SLACK_WEBHOOK_URL is not configured")
    post_to_slack(args.slack_webhook, text)
    print("VIP activity alert sent to Slack.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
