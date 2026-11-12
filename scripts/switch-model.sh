#!/usr/bin/env bash
# Switch RAHBAR's generation model in one command (docs/SWITCHING-MODELS.md, "Quick switch").
#
#   scripts/switch-model.sh llama     llama3.2:3b on host Ollama (the default; no key, nothing leaves)
#   scripts/switch-model.sh dots      Dots3-Note Preview on OpenRouter (free)
#   scripts/switch-model.sh gemma     Gemma 4 31B on OpenRouter (free)
#   scripts/switch-model.sh status    show what the api is running now
#   make model M=gemma                the same through make
#
# Each switch applies the right configuration, restarts the api (it reads its settings and the
# key only at start), clears the semantic cache so no answer from the previous model is served,
# and then reads the live settings back to prove the switch took. Hosted models need the
# rahbar-secret Secret (docs/SWITCHING-MODELS.md, 1.2); the script refuses without it.
#
# On minikube inside WSL 2 the cluster reaches Ollama through host.docker.internal, not
# host.minikube.internal (README step 2), so the script uses k8s/overlays/dev-wsl there and
# renders the hosted overlay with that address. Set RAHBAR_WSL=0 or 1 to override the detection.
set -euo pipefail

NS=${NS:-rahbar}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEFAULT_HOSTED=dots-studio/dots-3-note-preview:free   # the model in k8s/overlays/hosted

usage() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

if [[ -n ${RAHBAR_WSL:-} ]]; then
  wsl=$RAHBAR_WSL
elif grep -qi microsoft /proc/version 2>/dev/null; then
  wsl=1
else
  wsl=0
fi
if [[ $wsl == 1 ]]; then base=dev-wsl; ollama_url=http://host.docker.internal:11434
else base=dev; ollama_url=http://host.minikube.internal:11434; fi

status() {
  local provider model ollama
  provider=$(kubectl -n "$NS" exec deploy/api -- printenv LLM_PROVIDER)
  model=$(kubectl -n "$NS" exec deploy/api -- printenv LLM_MODEL)
  ollama=$(kubectl -n "$NS" exec deploy/api -- printenv OLLAMA_URL)
  echo "api is running: provider=$provider model=$model (embeddings via $ollama)"
  if kubectl -n "$NS" get networkpolicy allow-api-hosted-llm-egress >/dev/null 2>&1; then
    echo "hosted egress rule: present"; else echo "hosted egress rule: absent"; fi
  if kubectl -n "$NS" get secret rahbar-secret >/dev/null 2>&1; then
    echo "key Secret: present"; else echo "key Secret: absent"; fi
}

# kubectl apply also re-applies the indexer Job, whose pod template cannot change once the Job
# exists (ADR 0001). That one error is expected; anything else stops the switch.
apply() {
  local out rest
  if ! out=$("$@" 2>&1); then
    rest=$(grep -vE ' (unchanged|configured|created)$' <<<"$out" \
           | grep -vE 'Job "indexer" is invalid' || true)
    if [[ -n $rest ]]; then
      echo "$rest" >&2
      echo "apply failed; nothing was restarted" >&2
      exit 1
    fi
    echo "(the existing indexer Job was left as it is: its pod template cannot change)"
  fi
}

apply_hosted() {
  kubectl kustomize "$ROOT/k8s/overlays/hosted" \
    | sed -e "s#http://host.minikube.internal:11434#$ollama_url#" -e "s#$DEFAULT_HOSTED#$model#" \
    | kubectl apply -f -
}

case ${1:-} in
  status) status; exit 0 ;;
  llama) provider="ollama-host"; model="llama3.2:3b" ;;
  dots) provider=hosted; model=dots-studio/dots-3-note-preview:free ;;
  gemma) provider=hosted; model=google/gemma-4-31b-it:free ;;
  *) usage ;;
esac

echo "switching to $model ($provider), overlay base: $base"
if [[ $provider == hosted ]]; then
  kubectl -n "$NS" get secret rahbar-secret >/dev/null 2>&1 || {
    echo "no rahbar-secret: create it first (docs/SWITCHING-MODELS.md, 1.2)" >&2; exit 1; }
  apply apply_hosted
else
  apply kubectl apply -k "$ROOT/k8s/overlays/$base"
  kubectl -n "$NS" delete networkpolicy allow-api-hosted-llm-egress --ignore-not-found >/dev/null
fi

# The ConfigMap must say what we asked for before the api is restarted.
got=$(kubectl -n "$NS" get cm rahbar-config -o jsonpath='{.data.LLM_PROVIDER} {.data.LLM_MODEL} {.data.OLLAMA_URL}')
if [[ $got != "$provider $model $ollama_url" ]]; then
  echo "ConfigMap reads '$got', expected '$provider $model $ollama_url'; not restarting" >&2
  exit 1
fi

kubectl -n "$NS" rollout restart deploy/api >/dev/null
kubectl -n "$NS" rollout status deploy/api --timeout=5m >/dev/null
kubectl -n "$NS" exec deploy/redis -- redis-cli FLUSHALL >/dev/null
echo "api restarted, cache cleared"
status
