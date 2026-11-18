# Omnigent Automations

## 1. What are automations?

Automations let an agent run on a schedule without anyone starting each run.
You describe the work once, and Omnigent starts it at the times you choose and
keeps a history of every run.

**What automations can do**

- **Run any agent with your prompt:** pick an agent and tell it what to do.
  It uses that agent's skills and tools, and works as you, with your
  permissions.
- **Run on a schedule:** every hour, day, weekday or week, or a custom
  schedule, in your time zone. Runs can be as close as 10 minutes apart if the
  automation runs on a connected host or continues the same chat, otherwise an
  hour. Edits to the schedule take effect from the next run.
- **Run where you choose:** on one of your connected hosts (your laptop or a
  remote dev machine, optionally in a specific folder), or on a fresh sandbox for each
  run.
- **Run on demand:** **Run now** starts a run straight away, even when the
  automation is paused.
- **Start fresh or keep context:** start a new chat every run, or continue the
  same chat so the agent remembers what it did last time.
- **Keep a run history:** each run shows whether it is starting, running,
  succeeded, failed or skipped, with the reason for a failure and a link to its
  chat.

## 2. Examples

### 2.1 On-call: start a session for every new Jira ticket

Every 10 minutes, Omnigent checks a Jira project for new tickets. It starts a
separate session for each one, which investigates the ticket and proposes a
fix, then comments the session link on the ticket.

**Set it up from the Jira template**

1. Open **Automations**, go to the **Templates** tab, and choose
   **Jira tickets**.
2. **Trigger:** keep **Ticket created**, and set **in project equals** to
   your on-call project, for example `ES`. Click **Add condition** to narrow
   it to your tickets: **Assignee equals** `me`. You can also add conditions
   such as **Component equals** `Backend`.
3. Leave **Comment the session link on the ticket** checked, so whoever
   filed the ticket can follow along.
4. **Instructions for each session:** what to do with **one** ticket. Each
   ticket gets its own session with these instructions plus that ticket's
   details. For example:

   > You're the on-call engineer for this ticket.
   >
   > 1. Read the ticket and its comments, and work out what's broken and since
   >    when.
   > 2. Investigate with the `<oncall-skill>` skill: check the logs and
   >    dashboards it points to, reproduce the issue if you can, and find the
   >    root cause.
   > 3. If the fix is clear and small, make it on a new branch, add a test, run
   >    the tests, and open a PR that links the ticket.
   > 4. Comment on the ticket with the root cause, the evidence, and the PR link,
   >    or explain why it needs a human.
   >
   > Don't merge anything, and don't change the ticket's status or assignee.

5. **Host:** choose **Sandbox**, so it runs with your laptop off.
6. **Repository:** pick the repo the fixes go into.
7. **Schedule:** choose **Custom schedule**, then **Repeat every** `10`
   **Minutes**. Set ends to **On date** with the last day of your on-call
   shift, or **Never** if you want to keep it running. Ten minutes is the shortest gap
   allowed, and it's available here because the template keeps one session.
8. Click **Create automation**.

### 2.2 PR review: review new PRs that request my review

Every weekday morning, Omnigent finds the new pull requests that request your
review. It starts a separate session for each one, which reviews the PR and
drafts comments for you to submit.

**Set it up from the GitHub template**

1. Open **Automations**, go to the **Templates** tab, and choose **GitHub**.
2. **Trigger:** choose **Pull request opened**, and set **in repo** to the
   repository, for example `my-org/my-repo`. Click **Add condition**,
   choose **Search qualifiers (advanced)**, and enter `review-requested:@me`.
3. Uncheck **Comment the session link on it** if you don't want a comment from
   your account on each PR.
4. **Instructions for each session:** what to do with **one** PR. For
   example:

   > Review this pull request for me.
   >
   > 1. Read the description and the diff. Check out the branch and run the
   >    tests for the changed area.
   > 2. Look for bugs, missing tests, and anything that doesn't match the
   >    description.
   > 3. If it changes the UI, start the app, go through the changed flow, and
   >    save screenshots.
   > 4. Write your review comments as a **pending** review. Don't submit, approve
   >    or request changes.
   >
   > End with a short summary: the PR link, the risk (low, medium or high), the
   > main issues, and links to any screenshots.

5. **Host:** choose **Sandbox**, so it runs with your laptop off.
6. **Repository:** pick the same repo, so the session can check out the PR.
7. **Schedule:** choose **Every weekday**, pick a time such as 9:00, and set
   ends to **Never**.
8. Click **Create automation**.

### 2.3 Inbox digest: sort my email and list what needs a reply

Every morning, the agent reads the email you got in the last day, sorts it into
groups, and lists the messages you should reply to, with a suggested reply for
each.

**Set it up**

1. Open **Automations** and click **New automation**.
2. **Name:** `inbox digest`.
3. **Prompt:** what the agent should do each run. For example:

   > Summarize the email I received in the last 24 hours. Don't send, archive,
   > delete or label anything.
   >
   > 1. Put every message in one group: **Needs my reply**, **FYI**,
   >    **Meetings and calendar**, **Notifications** (bots, alerts, CI), or
   >    **Newsletters and promotions**.
   > 2. A message needs my reply if someone asks me a direct question, asks me
   >    to review or approve something, or is waiting on me to decide. Mail sent
   >    to a large list doesn't count unless it names me.
   > 3. List **Needs my reply** first, most urgent first. For each one give the
   >    sender, the subject, one line on what they need, a deadline if there is
   >    one, and a two- or three-sentence draft reply.
   > 4. Then give a one-line summary of each other group, and how many messages
   >    are in it. Call out anything in FYI I shouldn't miss.
   >
   > Link each message so I can open it in Gmail.

4. **Session:** **New session each run**, so each day's digest is its own
   session.
5. **Host:** **Sandbox**, or a connected host. Wherever it runs, the
   agent needs Gmail access, such as a Google MCP server.
6. **Schedule:** **Every day** at 8:00 in your time zone, ending **Never**.
   If you choose **Every weekday** instead, change "the last 24 hours" in the
   prompt to "since the last weekday at 8:00", so Monday covers the weekend.
7. Click **Create automation**, then **Run now** once to check the result.
