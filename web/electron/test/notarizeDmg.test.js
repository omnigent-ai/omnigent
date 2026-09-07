const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { getNotarytoolAuthArgs, notarizeDmg } = require("../build/afterAllArtifactBuild");

describe("DMG notarization", () => {
  it("maps App Store Connect API credentials to notarytool arguments", () => {
    assert.deepEqual(
      getNotarytoolAuthArgs({
        APPLE_API_KEY: "/tmp/AuthKey_TEST.p8",
        APPLE_API_KEY_ID: "KEYID",
        APPLE_API_ISSUER: "issuer-id",
      }),
      ["--key", "/tmp/AuthKey_TEST.p8", "--key-id", "KEYID", "--issuer", "issuer-id"],
    );
  });

  it("maps Apple ID credentials to notarytool arguments", () => {
    assert.deepEqual(
      getNotarytoolAuthArgs({
        APPLE_ID: "developer@example.com",
        APPLE_APP_SPECIFIC_PASSWORD: "app-password",
        APPLE_TEAM_ID: "TEAMID",
      }),
      ["--apple-id", "developer@example.com", "--password", "app-password", "--team-id", "TEAMID"],
    );
  });

  it("rejects incomplete credentials", () => {
    assert.throws(
      () => getNotarytoolAuthArgs({ APPLE_API_KEY: "/tmp/key.p8" }),
      /APPLE_API_KEY_ID/,
    );
  });

  it("verifies, notarizes, staples, and validates a DMG", () => {
    const calls = [];
    const run = (args, options) => {
      calls.push({ args, options });
      return args[0] === "notarytool" ? JSON.stringify({ status: "Accepted" }) : undefined;
    };

    notarizeDmg("dist/Omnigent.dmg", ["--key", "/tmp/key.p8"], run);

    assert.deepEqual(calls, [
      {
        args: ["codesign", "--verify", "--verbose=2", "dist/Omnigent.dmg"],
        options: undefined,
      },
      {
        args: [
          "notarytool",
          "submit",
          "dist/Omnigent.dmg",
          "--key",
          "/tmp/key.p8",
          "--wait",
          "--output-format",
          "json",
        ],
        options: { captureOutput: true },
      },
      {
        args: ["stapler", "staple", "-v", "dist/Omnigent.dmg"],
        options: undefined,
      },
      {
        args: ["stapler", "validate", "-v", "dist/Omnigent.dmg"],
        options: undefined,
      },
    ]);
  });

  it("fails when Apple does not accept the DMG", () => {
    const run = (args) =>
      args[0] === "notarytool" ? JSON.stringify({ status: "Invalid" }) : undefined;

    assert.throws(() => notarizeDmg("dist/Omnigent.dmg", [], run), /status: Invalid/);
  });
});
