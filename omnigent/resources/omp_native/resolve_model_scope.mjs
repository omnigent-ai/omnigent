// Run through OMP's extension loader so wrappers and global installs resolve
// canonical modules exactly as the native process does. No Pi/Node dependency.
import { readFileSync } from "node:fs";
import { getPackageDir } from "@oh-my-pi/pi-coding-agent/config";
import { filterAvailableModelsByEnabledPatterns } from "@oh-my-pi/pi-coding-agent/config/model-resolver";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";

const PREFIX = "OMNIGENT_OMP_NATIVE_RESOLVER:";

export default function resolveNativeMetadata() {
  try {
    const input = JSON.parse(readFileSync(0, "utf8"));
    let result;
    if (input.operation === "package-root") {
      result = getPackageDir() ?? null;
    } else {
      const { patterns, models, modelRoles } = input;
      const settings = Settings.isolated({ modelRoles: modelRoles ?? {} });
      const scoped = filterAvailableModelsByEnabledPatterns(models, patterns, settings);
      result = scoped.map((model) => models.indexOf(model));
    }
    process.stdout.write(`${PREFIX}${JSON.stringify(result)}\n`);
  } catch {
    // Native startup diagnostics may contain local config; never echo inputs.
    process.stdout.write(`${PREFIX}null\n`);
  }
}
