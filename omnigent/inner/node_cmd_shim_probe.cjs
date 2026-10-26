// Loaded with --require: prints the node and script a .cmd shim runs, then exits before the script.
require("node:fs").writeSync(
  1,
  "omnigent-node-cmd-shim-probe:" + JSON.stringify([process.execPath, process.argv[1]]) + "\n",
);
process.exit(0);
