#!/usr/bin/env bash
# Index a repository's full history, as a Kubernetes Job.
#
# **Not `kubectl exec`.** Build plan section 3 sizes a large repository at about a
# restartable *day*: ~3,600 REST requests and a GraphQL pass that is point-limited
# rather than request-limited, so 6-12 hours. An exec dies with the terminal that
# started it; a Job survives, restarts where it stopped, and can be watched from
# anywhere.
#
# Resuming is safe by construction. Every pass keeps its own cursor (section 5.2
# rule 4), the checkpoint advances per *committed* thread and stops at the first
# failure (rule 5), and `index_thread` makes re-processing a no-op (section 5.1) --
# so a killed Job re-covers the remainder rather than starting over or skipping.
set -euo pipefail

namespace="ghlore"
release="ghlore"
context=""
repo=""
follow=0
extra=()

usage() {
  cat <<'EOF'
Usage: deploy/scripts/backfill.sh --repo OWNER/NAME [options]

Options:
      --repo OWNER/NAME   required; must be in the chart's allowlist
  -n, --namespace NAME    default: ghlore
  -r, --release NAME      default: ghlore
      --context NAME      kubectl context
  -f, --follow            stream the logs after creating the Job
      --                  everything after this is passed to `ghlored backfill`
  -h, --help              this

Examples:
  deploy/scripts/backfill.sh --repo huggingface/transformers --follow
  deploy/scripts/backfill.sh --repo huggingface/x -- --no-graphql
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) repo="$2"; shift 2 ;;
    -n|--namespace) namespace="$2"; shift 2 ;;
    -r|--release) release="$2"; shift 2 ;;
    --context) context="$2"; shift 2 ;;
    -f|--follow) follow=1; shift ;;
    --) shift; extra=("$@"); break ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$repo" ]] || { echo "error: --repo is required" >&2; exit 2; }

kube=(kubectl --namespace "$namespace")
[[ -n "$context" ]] && kube+=(--context "$context")

slug="$(echo "$repo" | tr '/.' '--' | tr '[:upper:]' '[:lower:]')"
job="${release}-backfill-${slug}"

# Take the image and the wiring from the running deployment rather than from values:
# a backfill must be the same build as the daemon that will serve what it indexes.
image="$("${kube[@]}" get deploy "$release" -o jsonpath='{.spec.template.spec.containers[0].image}')"
[[ -n "$image" ]] || { echo "error: could not read the image from deploy/$release" >&2; exit 2; }
db_url="$("${kube[@]}" get deploy "$release" \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="GHLORE_DATABASE_URL")].value}')"
secret="$("${kube[@]}" get deploy "$release" \
  -o jsonpath='{.spec.template.spec.containers[0].envFrom[1].secretRef.name}')"
config="$("${kube[@]}" get deploy "$release" \
  -o jsonpath='{.spec.template.spec.containers[0].envFrom[0].configMapRef.name}')"

echo "repo    $repo"
echo "job     $job"
echo "image   $image"
echo

"${kube[@]}" delete job "$job" --ignore-not-found >/dev/null

args="[\"backfill\", \"--repo\", \"$repo\""
for a in ${extra[@]+"${extra[@]}"}; do args="$args, \"$a\""; done
args="$args]"

"${kube[@]}" apply -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: $job
  namespace: $namespace
  labels:
    app: ghlore
    component: backfill
spec:
  # Generous: the pass is resumable, so a retry costs a re-walk of the remainder
  # rather than the whole history. Section 3 calls it "one restartable day".
  backoffLimit: 20
  # Keep the record for a day after it finishes, then clean up on its own.
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app: ghlore
        component: backfill
    spec:
      restartPolicy: OnFailure
      containers:
        - name: backfill
          image: "$image"
          args: $args
          envFrom:
            - configMapRef:
                name: $config
            - secretRef:
                name: $secret
          env:
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: $secret
                  key: POSTGRES_PASSWORD
            - name: GHLORE_DATABASE_URL
              value: "$db_url"
          resources:
            requests: {cpu: 200m, memory: 512Mi}
            limits: {memory: 3Gi}
YAML

echo
echo "watch it with:"
echo "  kubectl ${context:+--context $context} -n $namespace logs -f job/$job"
[[ $follow -eq 1 ]] && exec "${kube[@]}" logs -f "job/$job"
