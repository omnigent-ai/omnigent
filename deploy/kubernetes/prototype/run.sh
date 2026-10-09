#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../../.." && pwd)
state=${PROTOTYPE_STATE_DIR:-/tmp/omnigent-nginx-prototype}
kind_bin=${KIND_BIN:-kind}
cluster=omnigent-nginx-prototype
context=kind-$cluster
prototype_port=${PROTOTYPE_PORT:-18081}
if [[ ! "$prototype_port" =~ ^[0-9]+$ ]] || (( prototype_port < 1024 || prototype_port > 65535 )); then
  printf 'PROTOTYPE_PORT must be a port between 1024 and 65535.\n' >&2
  exit 1
fi
mkdir -p "$state"
k() { kubectl --kubeconfig "$state/kubeconfig" --context "$context" -n omnigent-prototype "$@"; }

case "${1:-help}" in
  up)
    if [[ ! -f "$state/kubeconfig" ]]; then
      cat > "$state/kind.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30080
        hostPort: $prototype_port
        listenAddress: "127.0.0.1"
        protocol: TCP
EOF
      "$kind_bin" create cluster --name "$cluster" --kubeconfig "$state/kubeconfig" \
        --config "$state/kind.yaml" \
        --image "${PROTOTYPE_NODE_IMAGE:-kindest/node:v1.32.2@sha256:f226345927d7e348497136874b6d207e0b32cc52154ad8323129352923a3142f}" \
        --wait 60s
    fi
    DOCKER_BUILDKIT=1 docker build --target runtime \
      --network "${PROTOTYPE_BUILD_NETWORK:-default}" \
      --build-arg VITE_OMNIGENT_HOST_ROUTING=true \
      --build-arg "PYPI_INDEX_URL=${PYPI_INDEX_URL:-https://pypi.org/simple}" \
      --build-arg "NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY:-}" \
      -t omnigent-server:nginx-prototype -f "$repo/deploy/docker/Dockerfile" "$repo"
    "$kind_bin" load docker-image omnigent-server:nginx-prototype --name "$cluster"
    k apply -f "$here/ingress.yaml"
    k delete job migrate --ignore-not-found
    k apply -f "$here/database.yaml"
    k rollout status deployment/postgres --timeout=180s
    k wait --for=condition=complete job/migrate --timeout=180s
    existing_server=$(k get deployment/omnigent --ignore-not-found -o name)
    k apply -f "$here/server.yaml"
    if [[ -n "$existing_server" ]]; then
      k rollout restart deployment/omnigent
    fi
    k rollout status deployment/omnigent --timeout=180s
    k rollout status deployment/nginx --timeout=180s
    printf 'Ready: http://localhost:%s\n' "$prototype_port"
    ;;
  rollout)
    k rollout restart deployment/omnigent
    k rollout status deployment/omnigent --timeout=180s
    ;;
  verify)
    DOCKER_BUILDKIT=1 docker build --network "${PROTOTYPE_BUILD_NETWORK:-default}" \
      -t omnigent-prototype-client:local -f "$here/Client.Dockerfile" "$here"
    cd "$repo"
    exec uv run --no-sync python "$here/verify.py" --kubeconfig "$state/kubeconfig" \
      --url "http://localhost:$prototype_port" --output "$state/verification" "${@:2}"
    ;;
  kubectl)
    shift
    k "$@"
    ;;
  down)
    "$kind_bin" delete cluster --name "$cluster"
    rm -f "$state/kubeconfig"
    ;;
  *)
    printf 'Usage: %s {up|rollout|verify|kubectl ...|down}\n' "$0"
    ;;
esac
