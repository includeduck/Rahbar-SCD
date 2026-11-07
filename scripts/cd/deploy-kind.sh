#!/usr/bin/env bash
# Deploy RAHBAR to a throwaway kind cluster and smoke-test it through the Ingress.
# cd.yml runs this after pushing the images; it also runs locally (Git Bash or Linux).
#
#   scripts/cd/deploy-kind.sh <image-prefix> <tag>
#   scripts/cd/deploy-kind.sh docker.io/alice "$GITHUB_SHA"
#
# The three rahbar images must be in the local Docker daemon as <prefix>/rahbar-*:<tag>
# (pulled or built); they are loaded into kind, so the cluster never pulls anonymously.
# Needs: docker, kind, kubectl, kustomize, curl, python3 (or set PYTHON).
#
# Optional environment: CLUSTER (rahbar-cd), INGRESS_PORT (80), WORK_DIR (a temp dir),
# OVERLAY (smoke: prod's images and objects, with stubs, since kind has no Ollama),
# KEEP_CLUSTER=1 to leave the cluster running afterwards.
set -euo pipefail

PREFIX=${1:?image prefix, e.g. docker.io/alice}
TAG=${2:?image tag: the commit SHA, never latest}
CLUSTER=${CLUSTER:-rahbar-cd}
INGRESS_PORT=${INGRESS_PORT:-80}
OVERLAY=${OVERLAY:-smoke}
PYTHON=${PYTHON:-python3}
INGRESS_NGINX=controller-v1.15.1
NS=rahbar
HOST=rahbar.local
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
WORK=${WORK_DIR:-$(mktemp -d)}

step() { echo; echo "==> $*"; }

diagnose() {
  echo "::group::diagnostics"
  kubectl get pods -A -o wide || true
  kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -30 || true
  for pod in $(kubectl -n "$NS" get pods -o name 2>/dev/null); do
    echo "--- $pod"
    kubectl -n "$NS" logs "$pod" --all-containers --tail=30 || true
  done
  echo "::endgroup::"
}
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then diagnose; fi
  if [ -z "${KEEP_CLUSTER:-}" ]; then kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true; fi
  exit "$status"
}
trap cleanup EXIT

step "kind cluster $CLUSTER, Ingress on host port $INGRESS_PORT"
kind create cluster --name "$CLUSTER" --wait 180s --config - <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    kubeadmConfigPatches:
      - |
        kind: InitConfiguration
        nodeRegistration:
          kubeletExtraArgs:
            node-labels: "ingress-ready=true"
    extraPortMappings:
      - containerPort: 80
        hostPort: ${INGRESS_PORT}
        protocol: TCP
EOF

step "load images into the cluster"
platform=$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')
mkdir -p "$WORK"
for image in "$PREFIX/rahbar-api:$TAG" "$PREFIX/rahbar-ui:$TAG" "$PREFIX/rahbar-indexer:$TAG" \
             chromadb/chroma:1.0.12 redis:7.4.7-alpine; do
  docker image inspect "$image" >/dev/null 2>&1 || docker pull -q "$image"
  # With Docker Desktop's containerd image store, images carry attestation and other-
  # platform entries that kind's import cannot resolve ("content digest ... not found").
  # Fall back to exporting only this platform.
  if ! kind load docker-image --name "$CLUSTER" "$image" 2>/dev/null; then
    echo "direct load failed for $image; loading a $platform archive instead"
    docker save --platform "$platform" -o "$WORK/image.tar" "$image"
    kind load image-archive --name "$CLUSTER" "$WORK/image.tar"
    rm -f "$WORK/image.tar"
  fi
done

step "ingress-nginx $INGRESS_NGINX"
kubectl apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/$INGRESS_NGINX/deploy/static/provider/kind/deploy.yaml"
kubectl -n ingress-nginx wait --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout=240s

step "render the $OVERLAY overlay with images pinned to $TAG"
cp -r "$ROOT/k8s" "$WORK/"
# The image pins live in the prod overlay; smoke builds on top of it.
(
  cd "$WORK/k8s/overlays/prod"
  kustomize edit set image \
    "rahbar-api=$PREFIX/rahbar-api:$TAG" \
    "rahbar-ui=$PREFIX/rahbar-ui:$TAG" \
    "rahbar-indexer=$PREFIX/rahbar-indexer:$TAG"
)
kustomize build "$WORK/k8s/overlays/$OVERLAY" > "$WORK/rendered.yaml"
if grep -E '^\s+image: .*(:latest|:set-by-cd)\s*$' "$WORK/rendered.yaml"; then
  echo "refusing to deploy an image that is not pinned to $TAG"; exit 1
fi

step "apply"
# The ingress-nginx admission webhook can refuse the Ingress for a few seconds after
# the controller reports ready, so retry the whole (idempotent) apply.
for attempt in 1 2 3 4 5 6; do
  kubectl apply -f "$WORK/rendered.yaml" && break
  [ "$attempt" -eq 6 ] && exit 1
  echo "apply failed (attempt $attempt); retrying in 10 s"; sleep 10
done

step "wait for the indexer Job and every rollout"
kubectl -n "$NS" wait --for=condition=complete job/indexer --timeout=600s
kubectl -n "$NS" rollout status statefulset/chroma --timeout=300s
for deploy in redis api ui; do
  kubectl -n "$NS" rollout status "deployment/$deploy" --timeout=300s
done
kubectl -n "$NS" get pods -o wide

step "smoke test through the Ingress (Host: $HOST)"
base="http://localhost:$INGRESS_PORT"
page=$(curl -sf --retry 10 --retry-delay 3 --retry-all-errors -H "Host: $HOST" "$base/")
echo "$page" | grep -q '<title>RAHBAR' || { echo "the ui did not serve the chat page"; exit 1; }
echo "GET / served the chat page"

request_id="cd-smoke-${TAG:0:12}"
answer=$(curl -sf --retry 10 --retry-delay 3 --retry-all-errors -X POST "$base/api/ask" \
  -H "Host: $HOST" -H 'Content-Type: application/json' -H "X-Request-ID: $request_id" \
  -d '{"question": "What CGPA puts a student on academic warning?"}')
echo "$answer" | REQUEST_ID="$request_id" "$PYTHON" -c '
import json, os, sys
r = json.load(sys.stdin)
assert r["request_id"] == os.environ["REQUEST_ID"], "request id not echoed"
assert r["retrieved"], "nothing retrieved"
assert r["citations"], "no citations"
retrieved = {(p["source"], p["section"]) for p in r["retrieved"]}
bad = [c for c in r["citations"] if (c["source"], c["section"]) not in retrieved]
assert not bad, f"citations not in the retrieved set: {bad}"
print("POST /api/ask answered: provider=%s, %d retrieved, %d valid citation(s)"
      % (r["provider"], len(r["retrieved"]), len(r["citations"])))
'

wrong=$(curl -s -o /dev/null -w '%{http_code}' "$base/api/ask")
[ "$wrong" = "404" ] || { echo "a request without Host: $HOST got $wrong, expected 404"; exit 1; }
echo "a request for another host is refused (404)"

step "deployed $PREFIX/rahbar-*:$TAG and the smoke test passed"
