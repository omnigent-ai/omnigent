"use strict";

async function startWorkspaceFixtures(startPod, startPage) {
  const pod = await startPod();
  try {
    return { pod, demoPage: await startPage() };
  } catch (error) {
    await pod.close();
    throw error;
  }
}

module.exports = { startWorkspaceFixtures };
