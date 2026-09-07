const { spawnSync } = require("child_process");

function getNotarytoolAuthArgs(env = process.env) {
  const apiKeyValues = [env.APPLE_API_KEY, env.APPLE_API_KEY_ID, env.APPLE_API_ISSUER];
  if (apiKeyValues.some(Boolean)) {
    if (!apiKeyValues.every(Boolean)) {
      throw new Error(
        "DMG notarization requires APPLE_API_KEY, APPLE_API_KEY_ID, and APPLE_API_ISSUER.",
      );
    }
    return [
      "--key",
      env.APPLE_API_KEY,
      "--key-id",
      env.APPLE_API_KEY_ID,
      "--issuer",
      env.APPLE_API_ISSUER,
    ];
  }

  const appleIdValues = [env.APPLE_ID, env.APPLE_APP_SPECIFIC_PASSWORD, env.APPLE_TEAM_ID];
  if (appleIdValues.some(Boolean)) {
    if (!appleIdValues.every(Boolean)) {
      throw new Error(
        "DMG notarization requires APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD, and APPLE_TEAM_ID.",
      );
    }
    return [
      "--apple-id",
      env.APPLE_ID,
      "--password",
      env.APPLE_APP_SPECIFIC_PASSWORD,
      "--team-id",
      env.APPLE_TEAM_ID,
    ];
  }

  throw new Error(
    "DMG notarization credentials are missing; configure an Apple API key or Apple ID credentials.",
  );
}

function runXcrun(args, { captureOutput = false } = {}) {
  const result = spawnSync("xcrun", args, {
    encoding: "utf8",
    stdio: captureOutput ? ["ignore", "pipe", "pipe"] : "inherit",
  });

  if (captureOutput) {
    if (result.stdout) process.stdout.write(result.stdout);
    if (result.stderr) process.stderr.write(result.stderr);
  }
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`xcrun ${args[0]} failed with exit code ${result.status}.`);
  }

  return result.stdout;
}

function notarizeDmg(dmgPath, authArgs, run = runXcrun) {
  run(["codesign", "--verify", "--verbose=2", dmgPath]);

  const output = run(
    ["notarytool", "submit", dmgPath, ...authArgs, "--wait", "--output-format", "json"],
    { captureOutput: true },
  );
  const result = JSON.parse(output);
  if (result.status !== "Accepted") {
    throw new Error(
      `Apple notarization did not accept ${dmgPath} (status: ${result.status ?? "unknown"}).`,
    );
  }

  run(["stapler", "staple", "-v", dmgPath]);
  run(["stapler", "validate", "-v", dmgPath]);
}

module.exports = async function afterAllArtifactBuild(context) {
  if (process.env.OMNIGENT_NOTARIZE_DMG !== "true") return [];

  const dmgPaths = context.artifactPaths.filter((artifactPath) => artifactPath.endsWith(".dmg"));
  if (dmgPaths.length === 0) {
    throw new Error("DMG notarization was requested, but electron-builder produced no DMG files.");
  }

  const authArgs = getNotarytoolAuthArgs();
  for (const dmgPath of dmgPaths) {
    console.log(`Notarizing and stapling ${dmgPath}`);
    notarizeDmg(dmgPath, authArgs);
  }

  return [];
};

module.exports.getNotarytoolAuthArgs = getNotarytoolAuthArgs;
module.exports.notarizeDmg = notarizeDmg;
