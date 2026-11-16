// Golden outputs of this renderer for its Python port, omnigent/tools/builtins/trigger_prompts.py,
// which agents use to create trigger automations. tests/tools/builtins/test_trigger_prompts.py
// checks the port against the same file. After changing how a trigger renders, saves or
// validates, regenerate the fixture and update the port until both suites pass:
//   pnpm --filter web exec vitest run src/components/scheduled/triggerTemplates.parity.test.ts -u
import { describe, expect, it } from "vitest";
import {
  decodeTriggerPrompt,
  emptyTriggerFields,
  encodeTriggerPrompt,
  MAX_INSTRUCTIONS_CHARS,
  parseGithubReviewer,
  SESSION_LINK_ID_PLACEHOLDER,
  splitJqlOrderBy,
  stripTriggerHeader,
  triggerFieldsComplete,
  TRIGGER_SOURCES,
  withTriggerEvent,
  type TriggerConfig,
  type TriggerConfigContext,
  type TriggerFields,
  type TriggerSource,
  type TriggerWorkerConfig,
} from "./triggerTemplates";

const FIXTURE = "../../../../tests/tools/fixtures/trigger_prompts.json";

const SESSION_LINK = `https://omnigent.example.com/c/${SESSION_LINK_ID_PLACEHOLDER}`;

const WORKER: TriggerWorkerConfig = {
  placement: "same_host",
  agentId: null,
  model: "",
  effort: "",
  permissionMode: "",
};

const SCOPES: Record<TriggerSource, string> = {
  jira: "ES",
  slack: "eng-support",
  github: "octo-org/octo-repo",
  linear: "ENG",
  pagerduty: "Checkout API",
};

/** A trigger on the source's default event and scope, with instructions and default workers. */
function trigger(source: TriggerSource, overrides: Partial<TriggerConfig> = {}): TriggerConfig {
  return {
    source,
    ...emptyTriggerFields(source),
    scope: SCOPES[source],
    instructions: "Investigate it and propose a fix.",
    worker: WORKER,
    ...overrides,
  };
}

/** A config saved before scope operators existed. */
function withoutScopeOperator(config: TriggerConfig): TriggerConfig {
  const { scopeOperator: _scopeOperator, ...rest } = config;
  return rest;
}

interface RenderCase {
  name: string;
  config: TriggerConfig;
  context?: Omit<TriggerConfigContext, "sessionLink">;
}

const RENDER_CASES: RenderCase[] = [
  { name: "jira-created", config: trigger("jira", { scope: "ES" }) },
  {
    name: "jira-created-every-condition",
    config: trigger("jira", {
      scope: " ES ,OPS, ",
      scopeOperator: "in",
      conditions: [
        { field: "label", operator: "is", value: "omnigent" },
        { field: "label", operator: "is_not", value: "wont-fix" },
        { field: "issuetype", operator: "is", value: "Bug" },
        { field: "component", operator: "is_not", value: 'Back\\end "core"' },
        { field: "priority", operator: "is_not", value: "Low" },
        { field: "assignee", operator: "is", value: " ME " },
        { field: "assignee", operator: "is_not", value: "alice@example.com" },
        { field: "assignee", operator: "empty", value: "" },
        { field: "assignee", operator: "not_empty", value: "" },
        { field: "custom_field", operator: "is", value: "Platform", name: "Team Area" },
        { field: "custom_field", operator: "is_not", value: "Ops", name: '"Quoted Field"' },
        { field: "custom_field", operator: "empty", value: "", name: "cf[10010]" },
        { field: "custom_field", operator: "not_empty", value: "", name: " CF[42] " },
        { field: "jql", operator: "matches", value: 'summary ~ "crash" ORDER BY created DESC' },
        {
          field: "jql",
          operator: "matches",
          value: "text ~ \"say 'ORDER BY' \\\" here\" AND reporter = 'order by x'",
        },
        { field: "jql", operator: "matches", value: "ORDER BY priority" },
      ],
    }),
  },
  {
    name: "jira-created-poll-10",
    config: trigger("jira", { scope: "ES" }),
    context: { pollingIntervalMinutes: 10 },
  },
  {
    name: "jira-created-poll-90-no-link",
    config: trigger("jira", { scope: "ES", linkBack: false, linkBackTouched: true }),
    context: { pollingIntervalMinutes: 90 },
  },
  {
    name: "jira-scope-operator-absent",
    config: withoutScopeOperator(trigger("jira", { scope: "ES" })),
  },
  {
    name: "jira-scope-operator-absent-list",
    config: withoutScopeOperator(trigger("jira", { scope: "ES, OPS" })),
  },
  {
    name: "jira-assigned-me",
    config: trigger("jira", {
      event: "assigned",
      target: "me",
      linkBack: false,
      linkBackTouched: true,
    }),
  },
  {
    name: "jira-assigned-anyone-task-session",
    config: trigger("jira", { event: "assigned", target: "anyone", taskSessionMode: "continue" }),
    context: { taskSessionTitle: "Ticket triage" },
  },
  {
    name: "jira-assigned-empty-target",
    config: trigger("jira", { event: "assigned", target: "" }),
  },
  {
    name: "jira-assigned-unassigned",
    config: trigger("jira", { event: "assigned", target: "Unassigned" }),
  },
  {
    name: "jira-assigned-email",
    config: trigger("jira", {
      event: "assigned",
      target: ' bob"s@example.com ',
      conditions: [{ field: "label", operator: "is", value: "omnigent" }],
    }),
  },
  { name: "jira-assigned-without-target", config: trigger("jira", { event: "assigned" }) },
  {
    name: "jira-task-session-long-title",
    config: trigger("jira", { taskSessionMode: "continue" }),
    context: { taskSessionTitle: `${"a".repeat(198)}   ${"b".repeat(10)}` },
  },
  {
    name: "jira-task-session-emoji-title",
    config: trigger("jira", { taskSessionMode: "continue" }),
    context: { taskSessionTitle: "\u{1F389}".repeat(205) },
  },
  {
    name: "jira-task-session-blank-title",
    config: trigger("jira", { taskSessionMode: "continue", linkBack: false }),
    context: { taskSessionTitle: "   " },
  },
  { name: "jira-task-session-no-title", config: trigger("jira", { taskSessionMode: "continue" }) },
  {
    name: "jira-task-session-new-mode",
    config: trigger("jira", { taskSessionMode: "new" }),
  },
  {
    name: "jira-special-characters",
    config: trigger("jira", {
      scope: "ÉQUIPE",
      conditions: [
        { field: "label", operator: "is", value: 'a"b\\c' },
        { field: "label", operator: "is_not", value: "--> <tag>" },
      ],
      instructions:
        '  \n Investigate <this> --> & "quote" \\ backslash é \u{1F600}   line\u0001\u001f\t end ﻿\n ',
    }),
  },
  {
    name: "worker-json-escapes",
    config: trigger("jira", {
      scope: "ES",
      worker: {
        placement: "same_host",
        agentId: 'ag_"quoted"\\id',
        model: "example-model   line",
        effort: "high",
        permissionMode: "acceptEdits",
      },
    }),
  },
  {
    name: "slack-posted",
    config: trigger("slack", { scope: "#eng-support, ##bugs, C0123ABCD", scopeOperator: "in" }),
  },
  {
    name: "slack-posted-filters-no-link",
    config: trigger("slack", {
      scope: "eng-support",
      linkBack: false,
      conditions: [
        { field: "text", operator: "contains", value: " error " },
        { field: "text", operator: "not_contains", value: "ignore" },
        { field: "topic", operator: "about", value: "a bug report" },
        { field: "author", operator: "is", value: "@alerts-bot" },
        { field: "author", operator: "is_not", value: "alice" },
      ],
    }),
  },
  {
    name: "slack-posted-one-filter-task-session",
    config: trigger("slack", {
      scope: "help-desk",
      taskSessionMode: "continue",
      conditions: [{ field: "text", operator: "contains", value: "help" }],
    }),
    context: { taskSessionTitle: "Slack help desk" },
  },
  {
    name: "slack-dm-many",
    config: trigger("slack", {
      event: "dm",
      scope: "alice, @bob, U0123ABCD, carol@example.com, Alice",
      scopeOperator: "in",
      conditions: [{ field: "text", operator: "contains", value: "deploy" }],
    }),
  },
  {
    name: "slack-dm-one-poll-60",
    config: trigger("slack", { event: "dm", scope: "@alice" }),
    context: { pollingIntervalMinutes: 60 },
  },
  {
    name: "slack-group-dm",
    config: trigger("slack", {
      event: "group_dm",
      scope: "@alice, bob",
      scopeOperator: "in",
      conditions: [{ field: "topic", operator: "about", value: "outages" }],
    }),
  },
  {
    name: "slack-group-dm-task-session-no-link",
    config: trigger("slack", {
      event: "group_dm",
      scope: "alice, bob, carol",
      scopeOperator: "in",
      taskSessionMode: "continue",
      linkBack: false,
    }),
  },
  {
    name: "slack-lone-surrogates",
    config: trigger("slack", {
      scope: "eng",
      instructions: "Reply \ud800 then \udfff alone.",
    }),
  },
  {
    name: "github-issue-conditions",
    config: trigger("github", {
      scope: "octo-org/octo-repo",
      conditions: [
        { field: "label", operator: "is", value: "bug" },
        { field: "label", operator: "is_not", value: "needs triage" },
        { field: "author", operator: "is", value: "@octocat" },
        { field: "author", operator: "is_not", value: '"hubot"' },
        { field: "assignee", operator: "is", value: "me" },
        { field: "assignee", operator: "is_not", value: "@octocat" },
        { field: "assignee", operator: "empty", value: "" },
        { field: "qualifiers", operator: "matches", value: " no:milestone  " },
      ],
    }),
  },
  {
    name: "github-pr-reviewers",
    config: trigger("github", {
      event: "pr",
      scope: "octo-org/octo-repo, octo-org/other-repo",
      scopeOperator: "in",
      conditions: [
        { field: "reviewer", operator: "is", value: "acme/release-team" },
        { field: "reviewer", operator: "is_not", value: "@octocat" },
        { field: "reviewer", operator: "is", value: "me" },
      ],
    }),
  },
  {
    name: "github-any-no-link",
    config: trigger("github", { event: "any", scope: "octo-org/octo-repo", linkBack: false }),
  },
  {
    name: "github-review-me",
    config: trigger("github", {
      event: "review",
      scope: "octo-org/octo-repo",
      target: "me",
      linkBack: false,
      conditions: [{ field: "qualifiers", operator: "matches", value: "label:won't-fix" }],
    }),
    context: { pollingIntervalMinutes: 240 },
  },
  {
    name: "github-review-login",
    config: trigger("github", {
      event: "review",
      scope: "octo-org/octo-repo",
      target: "@octocat",
      linkBack: true,
      linkBackTouched: true,
    }),
  },
  {
    name: "github-review-team-task-session",
    config: trigger("github", {
      event: "review",
      scope: "octo-org/octo-repo",
      target: "acme/release-team",
      linkBack: false,
      taskSessionMode: "continue",
    }),
    context: { taskSessionTitle: "Reviews" },
  },
  {
    name: "github-review-invalid-target",
    config: trigger("github", {
      event: "review",
      scope: "octo-org/octo-repo",
      target: "octo cat",
      linkBack: false,
    }),
  },
  {
    name: "github-issue-managed-worker",
    config: trigger("github", {
      scope: "octo-org/octo-repo",
      worker: {
        placement: "managed",
        agentId: "ag_example",
        model: "example-model",
        effort: "high",
        permissionMode: "auto-review",
      },
    }),
  },
  {
    name: "github-pr-managed-task-session",
    config: trigger("github", {
      event: "pr",
      scope: "octo-org/octo-repo",
      taskSessionMode: "continue",
      worker: { ...WORKER, placement: "managed", model: "example-model" },
    }),
    context: { taskSessionTitle: "Pull request queue" },
  },
  {
    name: "linear-every-condition",
    config: trigger("linear", {
      scope: "ENG",
      conditions: [
        { field: "label", operator: "is", value: "Bug" },
        { field: "status", operator: "is_not", value: "Done" },
        { field: "priority", operator: "is", value: "Urgent" },
        { field: "project", operator: "is_not", value: "Mobile app" },
        { field: "assignee", operator: "is", value: "me" },
        { field: "assignee", operator: "is_not", value: "alice@example.com" },
        { field: "assignee", operator: "empty", value: "" },
        { field: "assignee", operator: "not_empty", value: "" },
        { field: "creator", operator: "is", value: "ME" },
        { field: "creator", operator: "is_not", value: "bob" },
        { field: "text", operator: "contains", value: "crash" },
        { field: "text", operator: "not_contains", value: "flaky" },
      ],
    }),
  },
  {
    name: "linear-teams-task-session",
    config: trigger("linear", {
      scope: "ENG, OPS",
      scopeOperator: "in",
      taskSessionMode: "continue",
    }),
  },
  {
    name: "linear-one-condition-no-link",
    config: trigger("linear", {
      scope: "ENG",
      linkBack: false,
      conditions: [{ field: "label", operator: "is", value: "Bug" }],
    }),
  },
  {
    name: "pagerduty-one-service-conditions",
    config: trigger("pagerduty", {
      scope: "Checkout API",
      conditions: [
        { field: "urgency", operator: "is", value: "high" },
        { field: "priority", operator: "is_not", value: "P5" },
        { field: "status", operator: "is", value: "triggered" },
        { field: "assignee", operator: "is", value: "me" },
        { field: "assignee", operator: "is_not", value: "alice@example.com" },
        { field: "title", operator: "contains", value: "database" },
        { field: "title", operator: "not_contains", value: "test" },
      ],
    }),
  },
  {
    name: "pagerduty-name-contains",
    config: trigger("pagerduty", { scope: "  Payments ", scopeOperator: "contains" }),
  },
  {
    name: "pagerduty-services-task-session-no-link",
    config: trigger("pagerduty", {
      scope: "Checkout API, Payments",
      scopeOperator: "in",
      taskSessionMode: "continue",
      linkBack: false,
    }),
  },
];

// Expected to fail validation; rendered anyway so the port renders them identically.
const INCOMPLETE_RENDER_CASES = ["github-review-invalid-target"];

const JIRA = trigger("jira", { scope: "ES", instructions: "x" });
const SLACK = trigger("slack", { scope: "eng-support", instructions: "x" });
const GITHUB = trigger("github", { scope: "octo-org/octo-repo", instructions: "x" });
const REVIEWERS = [
  "me",
  " @octocat ",
  "acme/release-team",
  "octo cat",
  "acme/a/b",
  '"octocat"',
  "",
];

const COMPLETENESS_CASES: { name: string; config: TriggerConfig }[] = [
  { name: "complete", config: JIRA },
  { name: "blank scope", config: { ...JIRA, scope: "  " } },
  { name: "equals with a list", config: { ...JIRA, scope: "ES, OPS" } },
  { name: "in with one scope", config: { ...JIRA, scopeOperator: "in" } },
  { name: "in with a list", config: { ...JIRA, scopeOperator: "in", scope: "ES, OPS" } },
  { name: "comma-only scope", config: { ...JIRA, scopeOperator: "in", scope: " , ," } },
  {
    name: "condition without a value",
    config: { ...JIRA, conditions: [{ field: "label", operator: "is", value: " " }] },
  },
  {
    name: "valueless condition",
    config: { ...JIRA, conditions: [{ field: "assignee", operator: "empty", value: "" }] },
  },
  {
    name: "custom field without a name",
    config: {
      ...JIRA,
      conditions: [{ field: "custom_field", operator: "is", value: "Backend", name: " " }],
    },
  },
  {
    name: "empty custom field",
    config: {
      ...JIRA,
      conditions: [{ field: "custom_field", operator: "empty", value: "", name: "Team" }],
    },
  },
  {
    name: "unnamed empty custom field",
    config: {
      ...JIRA,
      conditions: [{ field: "custom_field", operator: "empty", value: "", name: " " }],
    },
  },
  {
    name: "custom field with no name key",
    config: { ...JIRA, conditions: [{ field: "custom_field", operator: "not_empty", value: "" }] },
  },
  { name: "empty instructions", config: { ...JIRA, instructions: " \n " } },
  {
    name: "instructions at the cap",
    config: { ...JIRA, instructions: "x".repeat(MAX_INSTRUCTIONS_CHARS) },
  },
  {
    name: "instructions over the cap",
    config: { ...JIRA, instructions: "x".repeat(MAX_INSTRUCTIONS_CHARS + 1) },
  },
  {
    name: "instructions trimmed to the cap",
    config: { ...JIRA, instructions: ` ${"x".repeat(MAX_INSTRUCTIONS_CHARS)} \n` },
  },
  {
    name: "emoji instructions at the cap",
    config: { ...JIRA, instructions: "\u{1F600}".repeat(MAX_INSTRUCTIONS_CHARS / 2) },
  },
  {
    name: "emoji instructions over the cap",
    config: { ...JIRA, instructions: `${"\u{1F600}".repeat(MAX_INSTRUCTIONS_CHARS / 2)}x` },
  },
  { name: "group DM with one person", config: { ...SLACK, event: "group_dm", scope: "alice" } },
  {
    name: "group DM with two people",
    config: { ...SLACK, event: "group_dm", scope: "alice, bob" },
  },
  {
    name: "group DM with a repeated person",
    config: { ...SLACK, event: "group_dm", scope: "alice, @Alice" },
  },
  { name: "group DM of bare prefixes", config: { ...SLACK, event: "group_dm", scope: "@, @" } },
  { name: "DM of a bare prefix", config: { ...SLACK, event: "dm", scope: "@" } },
  { name: "channel of a bare prefix", config: { ...SLACK, scope: "#" } },
  { name: "DM of one person", config: { ...SLACK, event: "dm", scope: "alice" } },
  { name: "review without a reviewer", config: { ...GITHUB, event: "review", target: " " } },
  { name: "review without a target key", config: { ...GITHUB, event: "review" } },
  { name: "assignee change to anyone", config: { ...JIRA, event: "assigned", target: "" } },
  {
    name: "contains with a comma",
    config: trigger("pagerduty", {
      scope: "Pay, ments",
      scopeOperator: "contains",
      instructions: "x",
    }),
  },
  ...REVIEWERS.flatMap((reviewer) => [
    {
      name: `reviewer target ${JSON.stringify(reviewer)}`,
      config: { ...GITHUB, event: "review", target: reviewer },
    },
    {
      name: `reviewer condition ${JSON.stringify(reviewer)}`,
      config: {
        ...GITHUB,
        event: "pr",
        conditions: [{ field: "reviewer", operator: "is" as const, value: reviewer }],
      },
    },
  ]),
];

const JQL_QUERIES = [
  "",
  "  summary ~ crash  ",
  "summary ~ crash ORDER BY created DESC",
  "summary ~ crash\torder\n by created",
  "summary ~ \"ORDER BY\" AND text ~ 'order by'",
  'summary ~ "a \\" ORDER BY b" ORDER BY rank',
  "ORDER BY created",
  "borderBy = 1 ORDER BYX",
  "x = 1  ORDER BY y",
  "x = 1 ORDER BY a ORDER BY b",
];

const SLACK_CHANNEL: TriggerFields = {
  event: "posted",
  scope: "eng-support",
  scopeOperator: "in",
  conditions: [
    { field: "author", operator: "is", value: "alerts-bot" },
    { field: "text", operator: "contains", value: "error" },
  ],
  instructions: "x",
  linkBack: true,
};
const GITHUB_ISSUE: TriggerFields = {
  event: "issue",
  scope: "octo-org/octo-repo",
  scopeOperator: "in",
  conditions: [{ field: "assignee", operator: "is", value: "me" }],
  instructions: "x",
  linkBack: true,
};

const EVENT_SWITCHES: {
  name: string;
  source: TriggerSource;
  fields: TriggerFields;
  event: string;
}[] = [
  { name: "channel to DM", source: "slack", fields: SLACK_CHANNEL, event: "dm" },
  {
    name: "DM to group DM",
    source: "slack",
    fields: { ...SLACK_CHANNEL, event: "dm", scope: "alice, bob", scopeOperator: "is" },
    event: "group_dm",
  },
  { name: "issue to pull request", source: "github", fields: GITHUB_ISSUE, event: "pr" },
  { name: "issue to review", source: "github", fields: GITHUB_ISSUE, event: "review" },
  {
    name: "touched link-back to review",
    source: "github",
    fields: { ...GITHUB_ISSUE, linkBack: true, linkBackTouched: true },
    event: "review",
  },
  {
    name: "review keeps its reviewer",
    source: "github",
    fields: { ...GITHUB_ISSUE, event: "review", target: "alice", linkBack: false },
    event: "review",
  },
  {
    name: "review to issue drops the target",
    source: "github",
    fields: { ...GITHUB_ISSUE, event: "review", target: "alice", linkBack: false },
    event: "issue",
  },
  {
    name: "created to assigned",
    source: "jira",
    fields: {
      event: "created",
      scope: "ES",
      conditions: [
        { field: "assignee", operator: "is", value: "me" },
        { field: "label", operator: "is", value: "omnigent" },
      ],
      instructions: "x",
      linkBack: false,
    },
    event: "assigned",
  },
  {
    name: "unknown event",
    source: "linear",
    fields: { event: "created", scope: "ENG", conditions: [], instructions: "x", linkBack: true },
    event: "closed",
  },
];

/** The saved prompt the decoding cases edit. */
const SAVED = encodeTriggerPrompt(trigger("jira", { scope: "ES" }), { sessionLink: SESSION_LINK });

const DECODE_CASES: { name: string; prompt: string }[] = [
  { name: "unchanged", prompt: SAVED },
  { name: "body edited", prompt: `${SAVED}x` },
  { name: "scope edited", prompt: SAVED.replace('"scope":"ES"', '"scope":"OPS"') },
  { name: "version 2", prompt: SAVED.replace("omnigent-trigger v1 ", "omnigent-trigger v2 ") },
  { name: "version 01", prompt: SAVED.replace("omnigent-trigger v1 ", "omnigent-trigger v01 ") },
  { name: "no fingerprint", prompt: SAVED.replace(/,"fingerprint":"[0-9a-f]+"/, "") },
  { name: "malformed JSON", prompt: SAVED.replace('{"source"', '{"source":') },
  { name: "NaN", prompt: SAVED.replace('{"source"', '{"x":NaN,"source"') },
  { name: "extra key", prompt: SAVED.replace('{"source"', '{"extra":[1,{"deep":true}],"source"') },
  {
    name: "new task session mode",
    prompt: SAVED.replace('"worker"', '"taskSessionMode":"new","worker"'),
  },
  {
    name: "null scope operator",
    prompt: SAVED.replace('"scopeOperator":"is"', '"scopeOperator":null'),
  },
  { name: "CRLF header", prompt: SAVED.replace(" -->\n", " -->\r\n") },
  { name: "no header", prompt: stripTriggerHeader(SAVED) },
  { name: "unknown source", prompt: SAVED.replace('"source":"jira"', '"source":"other"') },
  { name: "worker without agent", prompt: SAVED.replace('"agentId":null,', "") },
];

describe("trigger prompt parity fixture", () => {
  it("renders, saves and reads triggers as the fixture records", async () => {
    const renders = RENDER_CASES.map(({ name, config, context }) => {
      const ctx: TriggerConfigContext = { sessionLink: SESSION_LINK, ...context };
      const prompt = encodeTriggerPrompt(config, ctx);
      return {
        name,
        config,
        context: ctx,
        prompt,
        decoded: decodeTriggerPrompt(prompt),
        complete: triggerFieldsComplete(config, config.source),
      };
    });
    expect(renders.filter((render) => !render.complete).map((render) => render.name)).toEqual(
      INCOMPLETE_RENDER_CASES,
    );
    const fixture = {
      renders,
      completeness: COMPLETENESS_CASES.map(({ name, config }) => ({
        name,
        config,
        complete: triggerFieldsComplete(config, config.source),
      })),
      reviewers: REVIEWERS.map((input) => ({ input, parsed: parseGithubReviewer(input) })),
      jqlOrderBy: JQL_QUERIES.map((input) => ({ input, split: splitJqlOrderBy(input) })),
      emptyFields: TRIGGER_SOURCES.map((source) => ({
        source,
        fields: emptyTriggerFields(source),
      })),
      eventSwitches: EVENT_SWITCHES.map(({ name, source, fields, event }) => ({
        name,
        source,
        fields,
        event,
        switched: withTriggerEvent(source, fields, event),
      })),
      decodes: DECODE_CASES.map(({ name, prompt }) => ({
        name,
        prompt,
        decoded: decodeTriggerPrompt(prompt),
      })),
    };
    await expect(`${JSON.stringify(fixture, null, 2)}\n`).toMatchFileSnapshot(FIXTURE);
  });
});
