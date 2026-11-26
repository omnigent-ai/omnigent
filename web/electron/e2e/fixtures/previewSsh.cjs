#!/usr/bin/env node
"use strict";

// Stand-in for the external SSH executable, not for desktop identity or browser policy.
const fs = require("node:fs");

const args = process.argv.slice(2);
fs.appendFileSync(process.env.OMNIGENT_TEST_SSH_LOG, `${JSON.stringify(args)}\n`);
if (args.at(-2) !== "preview-host" || !args.at(-1).includes("omnigent host status --server")) {
  process.exit(1);
}
const target = process.env.OMNIGENT_TEST_SSH_SERVER;
process.stdout.write(
  JSON.stringify({
    daemons: [
      {
        mode: "server",
        process: "online",
        host_status: "online",
        error: null,
        host_id: process.env.OMNIGENT_TEST_SSH_HOST,
        target,
        server_url: target,
      },
    ],
  }),
);
