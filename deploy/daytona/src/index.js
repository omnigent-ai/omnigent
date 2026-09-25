// Omnigent <-> Daytona egress relay.
//
// Daytona Tier 1/2 sandboxes can only reach an allowlisted set of public
// domains; *.workers.dev is on that list. This Worker lives there and
// transparently reverse-proxies EVERY request (plain HTTP and WebSocket
// upgrades alike) to the real Omnigent server, so the in-sandbox host's
// dial-back — the host tunnel WS, the runner tunnel WS, and the host's
// plain HTTP calls — all reach the server through the firewall.
//
// SECURITY: this terminates TLS and re-originates, so it can see the
// per-launch host token and tunnel payload. Deploy only a relay you
// control; see README.md (this directory) "Security considerations".
export default {
  async fetch(request, env) {
    if (!env.UPSTREAM_URL) {
      return new Response("relay misconfigured: UPSTREAM_URL unset", { status: 500 });
    }
    const upstream = new URL(env.UPSTREAM_URL);
    const incoming = new URL(request.url);
    // Preserve path + query; swap the origin and keep any base path carried
    // by UPSTREAM_URL (a server mounted under a prefix, e.g. …/omnigent), so
    // relay /health reaches <base>/health rather than the deployment root.
    upstream.pathname = upstream.pathname.replace(/\/+$/, "") + incoming.pathname;
    upstream.search = incoming.search;
    // Reconstruct the request against the upstream origin. Passing the
    // original `request` as init copies method, headers (incl.
    // X-Omnigent-Host-Token and the WebSocket Upgrade), and body.
    // Cloudflare proxies the WebSocket handshake through fetch().
    const proxied = new Request(upstream.toString(), request);
    return fetch(proxied);
  },
};
