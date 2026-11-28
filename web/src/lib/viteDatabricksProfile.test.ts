import { expect, it } from "vitest";
import { profileForWorkspace } from "../../vite.databricksProfile";

const config = `
[DEFAULT]
host = https://inherited.example.com

[shared-host]
host = https://ws.example.com

[shared-host-2]
host = https://ws.example.com/
auth_type = databricks-cli

; [commented-out]
# host = https://ws.example.com
[pinned]
host         = WS.example.com
workspace_id = 111
auth_type    = databricks-cli

[pinned-elsewhere]
host = https://ws.example.com
workspace_id = 222

[other-host]
host = https://other.example.com
workspace_id = 111

[inherits-default-host]
workspace_id = 333
`;

it.each([
  {
    name: "one of several host matches is pinned",
    host: "https://ws.example.com",
    id: "111",
    expected: "pinned",
  },
  {
    name: "another workspace on the same host",
    host: "https://ws.example.com",
    id: "222",
    expected: "pinned-elsewhere",
  },
  {
    name: "no profile pinned to the workspace",
    host: "https://ws.example.com",
    id: "999",
    expected: undefined,
  },
  {
    name: "pinned only on another host",
    host: "https://third.example.com",
    id: "111",
    expected: undefined,
  },
  {
    name: "host inherited from DEFAULT",
    host: "https://inherited.example.com",
    id: "333",
    expected: "inherits-default-host",
  },
])("picks the workspace's profile: $name", ({ host, id, expected }) => {
  expect(profileForWorkspace(config, host, id)).toBe(expected);
});

it("leaves several profiles pinned to one workspace to the CLI", () => {
  const twice = `${config}\n[pinned-again]\nhost = https://ws.example.com\nworkspace_id = 111\n`;
  expect(profileForWorkspace(twice, "https://ws.example.com", "111")).toBeUndefined();
});
