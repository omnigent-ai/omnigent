const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

try {
  const config = JSON.parse(
    fs.readFileSync(path.join(__dirname, "gateway-auth.json"), "utf8"),
  );
  const request = { kind: "gateway_auth", token: config.token };
  const socket = net.connect(config.port, "127.0.0.1");
  const fail = () => {
    socket.destroy();
    process.exitCode = 1;
  };
  const deadline = setTimeout(fail, 9000);
  let buffer = "";

  socket.on("connect", () => socket.write(JSON.stringify(request) + "\n"));
  socket.on("data", (data) => {
    buffer += data;
    if (!buffer.includes("\n")) return;
    try {
      const response = JSON.parse(buffer.split("\n")[0]);
      if (typeof response.token !== "string" || !response.token) {
        fail();
        return;
      }
      process.stdout.write(response.token);
      socket.end();
    } catch {
      fail();
    }
  });
  socket.on("error", fail);
  socket.on("close", () => clearTimeout(deadline));
} catch {
  // Configuration errors must not echo the bridge secret.
  process.exitCode = 1;
}
