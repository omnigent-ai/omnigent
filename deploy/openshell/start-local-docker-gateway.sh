#!/usr/bin/env bash
set -euo pipefail

OPENSHELL_LOCAL_DIR="${OPENSHELL_LOCAL_DIR:-$HOME/.openshell-local}"
OPENSHELL_GATEWAY_PORT="${OPENSHELL_GATEWAY_PORT:-17670}"
OPENSHELL_GATEWAY_ENDPOINT="http://127.0.0.1:${OPENSHELL_GATEWAY_PORT}"
JWT_DIR="${OPENSHELL_LOCAL_DIR}/jwt"
GATEWAY_CONFIG="${OPENSHELL_LOCAL_DIR}/gateway.toml"
GATEWAY_LOG="${OPENSHELL_LOCAL_DIR}/gateway.log"

mkdir -p "${JWT_DIR}"

if [[ ! -f "${JWT_DIR}/signing.pem" ]]; then
  openssl genpkey -algorithm ed25519 -out "${JWT_DIR}/signing.pem"
fi

if [[ ! -f "${JWT_DIR}/public.pem" ]]; then
  openssl pkey -in "${JWT_DIR}/signing.pem" -pubout -out "${JWT_DIR}/public.pem"
fi

printf 'local-dev\n' > "${JWT_DIR}/kid"

cat > "${GATEWAY_CONFIG}" <<TOML
[openshell.gateway.gateway_jwt]
signing_key_path = "${JWT_DIR}/signing.pem"
public_key_path  = "${JWT_DIR}/public.pem"
kid_path         = "${JWT_DIR}/kid"
gateway_id       = "openshell"
ttl_secs         = 0

[openshell.gateway.auth]
allow_unauthenticated_users = true
TOML

if pgrep -a -x openshell-gateway | grep -F -- "--port ${OPENSHELL_GATEWAY_PORT}" >/dev/null; then
  echo "OpenShell gateway already appears to be running on port ${OPENSHELL_GATEWAY_PORT}."
else
  : > "${GATEWAY_LOG}"
  nohup openshell-gateway --config "${GATEWAY_CONFIG}" \
    --disable-tls --drivers docker --port "${OPENSHELL_GATEWAY_PORT}" \
    > "${GATEWAY_LOG}" 2>&1 &
fi

echo "Waiting for OpenShell gateway to listen on ${OPENSHELL_GATEWAY_ENDPOINT}..."
for _ in {1..60}; do
  if grep -q 'Server listening' "${GATEWAY_LOG}" 2>/dev/null; then
    break
  fi
  sleep 1
done

if ! grep -q 'Server listening' "${GATEWAY_LOG}" 2>/dev/null; then
  echo "OpenShell gateway did not become ready. Last log lines:" >&2
  tail -50 "${GATEWAY_LOG}" >&2 || true
  exit 1
fi

openshell gateway add "${OPENSHELL_GATEWAY_ENDPOINT}" --local || true
openshell status

echo "Gateway log: ${GATEWAY_LOG}"
