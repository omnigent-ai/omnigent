// Tunnels the browser's dictation WebSocket to Omnigent's server-side
// speech-to-text stream. The upgrade is relayed at the TCP level, so the
// office needs no WebSocket implementation and credentials stay here.

import type { IncomingMessage } from "node:http";
import { connect as connectTcp, type Socket } from "node:net";
import { connect as connectTls } from "node:tls";
import type { Duplex } from "node:stream";

const FORWARDED_HEADERS = ["sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol"];

export function buildUpstreamUpgrade(
  request: IncomingMessage,
  upstream: URL,
  upstreamPath: string,
  authHeaders: Record<string, string>,
): string {
  const lines = [
    `GET ${upstreamPath} HTTP/1.1`,
    `Host: ${upstream.host}`,
    "Connection: Upgrade",
    "Upgrade: websocket",
    // Omnigent accepts loopback origins in local mode; present its own.
    `Origin: ${upstream.origin}`,
  ];
  for (const name of FORWARDED_HEADERS) {
    const value = request.headers[name];
    if (typeof value === "string") lines.push(`${name}: ${value}`);
  }
  for (const [name, value] of Object.entries(authHeaders)) lines.push(`${name}: ${value}`);
  return `${lines.join("\r\n")}\r\n\r\n`;
}

export function proxyDictation(
  request: IncomingMessage,
  clientSocket: Duplex,
  head: Buffer,
  omnigentBaseUrl: string,
  authHeaders: Record<string, string>,
): void {
  const upstream = new URL(omnigentBaseUrl);
  const secure = upstream.protocol === "https:";
  const port = Number(upstream.port || (secure ? 443 : 80));
  const onConnect = () => {
    upstreamSocket.write(buildUpstreamUpgrade(request, upstream, "/v1/dictation/stream", authHeaders));
    if (head.length) upstreamSocket.write(head);
    upstreamSocket.pipe(clientSocket);
    clientSocket.pipe(upstreamSocket);
  };
  const upstreamSocket: Socket = secure
    ? connectTls({ host: upstream.hostname, port, servername: upstream.hostname }, onConnect)
    : connectTcp({ host: upstream.hostname, port }, onConnect);
  const close = () => {
    upstreamSocket.destroy();
    clientSocket.destroy();
  };
  upstreamSocket.on("error", () => {
    clientSocket.end("HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
    close();
  });
  clientSocket.on("error", close);
  upstreamSocket.on("close", close);
  clientSocket.on("close", close);
}
