"use strict";

const fs = require("node:fs");
const http = require("node:http");
const { createRequire } = require("node:module");
const path = require("node:path");

async function startRadixFormFixture() {
  const webRoot = path.resolve(__dirname, "../../..");
  const webRequire = createRequire(path.join(webRoot, "package.json"));
  const viteRequire = createRequire(webRequire.resolve("vite"));
  const { outputFiles } = await viteRequire("esbuild").build({
    entryPoints: [path.join(__dirname, "designModalFixture.jsx")],
    bundle: true,
    write: false,
    nodePaths: [path.join(webRoot, "node_modules")],
    platform: "browser",
    format: "iife",
    jsx: "automatic",
    define: { "process.env.NODE_ENV": '"development"' },
  });
  const html = fs.readFileSync(path.join(__dirname, "designModalFixture.html"));
  const server = http.createServer((request, response) => {
    response.setHeader("Cache-Control", "no-store");
    if (request.url === "/app.js") {
      response.setHeader("Content-Type", "application/javascript; charset=utf-8");
      response.end(outputFiles[0].contents);
    } else if (request.url === "/modal") {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.end(html);
    } else {
      response.writeHead(404);
      response.end();
    }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return {
    url: `http://127.0.0.1:${server.address().port}/modal`,
    close: () =>
      new Promise((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
      }),
  };
}

module.exports = { startRadixFormFixture };
