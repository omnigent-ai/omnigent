"use strict";

// Reaches the workspace socket of a host on a machine the user connected over
// SSH: one shared, prompt-free ssh connection per destination, with each remote
// socket forwarded to a local one in a private directory.

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createSshSession } = require("./ssh_connect");

const IDLE_MS = 10 * 60_000;
// A file read waits on this, so give up soon and let the page use the server.
const OPEN_TIMEOUT_MS = 10_000;
// No password or host-key prompts: those fail, and the page uses the server.
const SSH_ARGS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"];

/**
 * @param {{
 *   createSession?: typeof createSshSession,
 *   makeTempDir?: () => string,
 *   idleMs?: number,
 * }} [deps]
 */
function createWorkspaceSshForwards(deps = {}) {
  const createSession = deps.createSession ?? createSshSession;
  const makeTempDir =
    deps.makeTempDir ?? (() => fs.mkdtempSync(path.join(os.tmpdir(), "omni-ws-ssh-")));
  const idleMs = deps.idleMs ?? IDLE_MS;
  /** @type {Map<string, {dir: string, session: any, ready: Promise<void>, forwards: Map<string, {localPath: string, pending: Promise<string>}>, count: number, timer: any}>} */
  const connections = new Map();
  // Connections whose graceful close is still running; quit must still reach them.
  const retiring = new Set();
  let shutDown = false;

  /**
   * End `connection` if it is still `destination`'s current one, so a failure from
   * an older connection can't close the one that replaced it.
   */
  function closeConnection(destination, connection) {
    if (connections.get(destination) !== connection) return;
    connections.delete(destination);
    clearTimeout(connection.timer);
    retiring.add(connection);
    void connection.session
      .close()
      .catch(() => {})
      .finally(() => {
        retiring.delete(connection);
        fs.rmSync(connection.dir, { recursive: true, force: true });
      });
  }

  /**
   * Cancel one remote socket's forward so the next read forwards it afresh. If ssh
   * can't cancel it, the master is gone, so the whole connection goes.
   */
  function dropForward(destination, connection, remotePath, forward) {
    if (connections.get(destination) !== connection) return;
    if (connection.forwards.get(remotePath) !== forward) return;
    connection.forwards.delete(remotePath);
    void connection.session.cancelSocket(forward.localPath, remotePath).then((result) => {
      if (result.code !== 0) closeConnection(destination, connection);
    });
  }

  function connect(destination) {
    let connection = connections.get(destination);
    if (!connection) {
      const dir = makeTempDir();
      fs.chmodSync(dir, 0o700);
      const session = createSession({
        destination,
        socketPath: path.join(dir, "m"),
        sshArgs: SSH_ARGS,
        openTimeoutMs: OPEN_TIMEOUT_MS,
      });
      const opened = {
        dir,
        session,
        ready: session.open(),
        forwards: new Map(),
        count: 0,
        timer: null,
      };
      opened.ready.catch(() => closeConnection(destination, opened));
      connections.set(destination, opened);
      connection = opened;
    }
    clearTimeout(connection.timer);
    connection.timer = setTimeout(() => closeConnection(destination, connection), idleMs);
    connection.timer.unref?.();
    return connection;
  }

  /**
   * A local socket that reaches `remotePath` on `destination`.
   *
   * @param {string} destination A destination validated by `validateDestination`.
   * @param {string} remotePath The host's socket path on that machine.
   * @returns {Promise<{socketPath: string, dropConnection: () => void, dropForward: () => void}>}
   *   `dropConnection` ends the connection this socket rides on, and `dropForward` only
   *   this socket's forward; both do nothing once they are already replaced.
   */
  async function localSocket(destination, remotePath) {
    if (shutDown) throw new Error("the workspace SSH connections are closed");
    const connection = connect(destination);
    const assertCurrent = () => {
      if (connections.get(destination) !== connection) {
        throw new Error(`the connection to ${destination} closed`);
      }
    };
    await connection.ready;
    assertCurrent();
    let forward = connection.forwards.get(remotePath);
    if (!forward) {
      connection.count += 1;
      const localPath = path.join(connection.dir, `w${connection.count}.sock`);
      const pending = connection.session.forwardSocket(localPath, remotePath).then((result) => {
        if (result.code !== 0) {
          throw new Error(`couldn't forward the workspace socket on ${destination}`);
        }
        return localPath;
      });
      forward = { localPath, pending };
      connection.forwards.set(remotePath, forward);
      // A refused forward usually means the shared connection is gone, so drop it.
      pending.catch(() => closeConnection(destination, connection));
    }
    const socketPath = await forward.pending;
    assertCurrent();
    return {
      socketPath,
      dropConnection: () => closeConnection(destination, connection),
      dropForward: () => dropForward(destination, connection, remotePath, forward),
    };
  }

  return {
    localSocket,
    /**
     * Synchronous, for quit: refuses new connections and kills the open ones now,
     * since forced exits can skip the async shutdown.
     */
    closeAll: () => {
      shutDown = true;
      for (const connection of [...connections.values(), ...retiring]) {
        clearTimeout(connection.timer);
        connection.session.abort();
        fs.rmSync(connection.dir, { recursive: true, force: true });
      }
      connections.clear();
      retiring.clear();
    },
  };
}

module.exports = { createWorkspaceSshForwards };
