import { appendFileSync, chmodSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const statePath = process.env.SETUP_UV_TEST_STATE;
const state = JSON.parse(readFileSync(statePath, "utf8"));
const attempt = state.attempts.length + 1;
state.attempts.push({ attempt, time: Date.now() });
writeFileSync(statePath, JSON.stringify(state));
appendFileSync(process.env.GITHUB_STATE, `attempt=${attempt}\n`);

if (attempt <= Number(process.env.SETUP_UV_TEST_FAILURES)) {
  console.error(`Injected uv setup failure ${attempt}`);
  process.exit(1);
}

const binDirectory = path.join(process.env.RUNNER_TEMP, "setup-uv-test-bin");
const uvPath = path.join(binDirectory, "uv");
mkdirSync(binDirectory, { recursive: true });
writeFileSync(uvPath, `#!/bin/sh\necho "uv ${process.env.INPUT_VERSION}"\n`);
chmodSync(uvPath, 0o755);
appendFileSync(process.env.GITHUB_PATH, `${binDirectory}\n`);
