"use strict";

/**
 * Authenticate before committing any server-switch state or navigation.
 *
 * @param {{
 *   authenticate: () => Promise<boolean>,
 *   beforeLoad?: () => void | Promise<void>,
 *   load: () => Promise<void>,
 * }} params
 * @returns {Promise<boolean>}
 */
async function loadServerAfterAuth({ authenticate, beforeLoad, load }) {
  if (!(await authenticate())) return false;
  await beforeLoad?.();
  await load();
  return true;
}

/**
 * A cancelled cold-start login must land on setup instead of about:blank.
 *
 * @param {{ loadServer: () => Promise<boolean>, loadSetup: () => Promise<void> }} params
 * @returns {Promise<boolean>}
 */
async function loadInitialDestination({ loadServer, loadSetup }) {
  const loaded = await loadServer();
  if (!loaded) await loadSetup();
  return loaded;
}

function isSetupIdle(state) {
  return state?.serverUrl === null && !state.pendingServerLoads;
}

function withServerLoad(state, load) {
  if (!state || state.pendingServerLoads) return Promise.resolve(false);
  state.pendingServerLoads = 1;
  return Promise.resolve()
    .then(load)
    .finally(() => (state.pendingServerLoads = 0));
}

module.exports = { loadServerAfterAuth, loadInitialDestination, isSetupIdle, withServerLoad };
