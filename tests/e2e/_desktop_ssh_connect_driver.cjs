"use strict";

// Laptop side of test_desktop_ssh_connect.py: runs the desktop app's
// production SSH connect code, answering ssh's prompts the way a user would in
// the consent window and following the sign-in link the way a browser would.
// Prints one JSON event per line on stdout.

const net = require("node:net");
const path = require("node:path");
const { createSshSession, startSshConnect } = require(
  path.join(__dirname, "..", "..", "web", "electron", "src", "ssh_connect.js"),
);

const [serverUrl, destination, sshConfig, busyPort, workspacePort] = process.argv.slice(2);
const emit = (event) => process.stdout.write(`${JSON.stringify(event)}\n`);

async function main() {
  // Something else holds this port on the laptop, so the box must not pick it.
  const blocker = net.createServer();
  await new Promise((resolve) => blocker.listen(Number(busyPort), "127.0.0.1", resolve));
  const { promise } = startSshConnect(serverUrl, destination, {
    hooks: {
      onOutput: (text) => process.stderr.write(text),
      onStatus: (status) => emit({ status: status.step }),
      prompt: async ({ kind, text }) => {
        emit({ prompt: kind, text });
        if (kind === "confirm") return "yes";
        if (kind === "secret") return process.env.TEST_KEY_PASSPHRASE;
        return null;
      },
      askInstall: async () => {
        emit({ askInstall: true });
        return false;
      },
    },
    openExternal: async (url) => {
      emit({ open: url });
      // Follows the workspace's redirect to the laptop's localhost:<port>.
      const response = await fetch(url);
      emit({ browser: response.status, landedPort: new URL(response.url).port });
    },
    readInstallScript: () => null,
    createSession: (options) => {
      const session = createSshSession({ ...options, sshArgs: ["-F", sshConfig] });
      // The laptop's browser reaches the test workspace through the same connection.
      const open = async () => {
        await session.open();
        const forward = await session.forwardLocal(Number(workspacePort));
        if (forward.code !== 0) throw new Error(`workspace forward failed: ${forward.stderr}`);
        // A listener open to the network would hold the port on 127.0.0.2 too.
        const probe = net.createServer();
        const loopbackOnly = await new Promise((resolve) => {
          probe.once("error", () => resolve(false));
          probe.listen(Number(workspacePort), "127.0.0.2", () => probe.close(() => resolve(true)));
        });
        emit({ loopbackOnly });
      };
      return { ...session, open };
    },
    log: (message) => process.stderr.write(`${message}\n`),
  });
  const result = await promise;
  blocker.close();
  emit({ result });
}

main().catch((error) => {
  emit({ crash: String(error?.stack ?? error) });
  process.exitCode = 1;
});
