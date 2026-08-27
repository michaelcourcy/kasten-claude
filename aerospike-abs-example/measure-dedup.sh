#!/usr/bin/env bash
# Measure Kopia deduplication efficiency of this blueprint's export.
# Implements Step 6 of ../AGENTS.md.
#
# The design choice under test: this blueprint rewrites a COMPLETE logical dump
# on every run (absctl --remove-files). That looks wasteful, but Kasten's export
# to object storage is content-addressed and deduplicated by Kopia. This script
# measures how much the export actually grows for a backup whose content barely
# changed, which is the number that decides whether the simple full-dump design
# is acceptable or whether chained incrementals are needed.
#
# Usage:
#   ./measure-dedup.sh size          # print the current Kopia repo size
#   ./measure-dedup.sh dump          # print the current dump size on the keeper PVC
#   ./measure-dedup.sh run <name>    # run backup+export and wait for both
#   ./measure-dedup.sh churn <n>     # update n records (simulate a delta)
#
# Requires: kubectl, aws cli, jq.
set -euo pipefail

NS="${NS:-aerospike-test}"
POLICY="${POLICY:-aerospike-absctl-backup}"
PROFILE_SECRET="${PROFILE_SECRET:-k10secret-c2j6h}"
BUCKET="${BUCKET:-mcourcy-sopra}"
REGION="${REGION:-eu-west-3}"
KEEPER_POD="${KEEPER_POD:-aerospike-backup-keeper-0}"
TOOLS_IMAGE="${TOOLS_IMAGE:-aerospike/aerospike-tools:13.0.2}"

# The Kopia repository for a namespace lives at a deterministic prefix:
#   k10/<cluster-uid>/migration/repo/<namespace-uid>
# where <cluster-uid> is the UID of the "default" namespace.
cluster_uid() { kubectl get ns default -o jsonpath='{.metadata.uid}'; }
ns_uid()      { kubectl get ns "$NS" -o jsonpath='{.metadata.uid}'; }

# Kasten writes TWO Kopia prefixes on export. Measured on Kasten 9.0.4:
#   migration/repo/<namespace-uid>/ -> holds the BULK (the exported volume data)
#   migration/<policy-name>/kopia/  -> a much smaller companion repository
# The namespace repo alone is ~99% of the total, but both are counted here so
# the number is complete.
meta_prefix() { echo "k10/$(cluster_uid)/migration/repo/$(ns_uid)"; }
data_prefix() { echo "k10/$(cluster_uid)/migration/${POLICY}/kopia"; }

with_creds() {
  local ak sk
  ak=$(kubectl get secret "$PROFILE_SECRET" -n kasten-io -o jsonpath='{.data.aws_access_key_id}' | base64 -d)
  sk=$(kubectl get secret "$PROFILE_SECRET" -n kasten-io -o jsonpath='{.data.aws_secret_access_key}' | base64 -d)
  AWS_ACCESS_KEY_ID="$ak" AWS_SECRET_ACCESS_KEY="$sk" AWS_DEFAULT_REGION="$REGION" "$@"
}

case "${1:-}" in
  size)
    MP=$(meta_prefix); DP=$(data_prefix)
    META=$(with_creds aws s3 ls "s3://${BUCKET}/${MP}/" --recursive --summarize 2>/dev/null \
             | awk '/Total Size:/{print $3}'); META=${META:-0}
    DATA=$(with_creds aws s3 ls "s3://${BUCKET}/${DP}/" --recursive --summarize 2>/dev/null \
             | awk '/Total Size:/{print $3}'); DATA=${DATA:-0}
    printf 'metadata repo : %12d bytes  (%s)\n' "$META" "$MP"
    printf 'exported data : %12d bytes  (%s)\n' "$DATA" "$DP"
    printf 'TOTAL         : %12d bytes  (%.1f MiB)\n' "$((META + DATA))" \
      "$(echo "scale=3; ($META + $DATA)/1048576" | bc)"
    ;;

  dump)
    echo "Dump size per namespace on the keeper PVC (KiB):"
    kubectl exec -n "$NS" "$KEEPER_POD" -c absctl -- \
      sh -c 'for d in /backup/*/; do [ -d "$d" ] || continue; du -sk "$d"; done'
    ;;

  run)
    NAME="${2:?usage: run <action-name>}"
    kubectl create -f - --validate=false >/dev/null <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata:
  name: ${NAME}
  namespace: kasten-io
spec:
  subject:
    kind: Policy
    name: ${POLICY}
    namespace: kasten-io
EOF
    echo "RunAction ${NAME} created; waiting..."
    for _ in $(seq 1 90); do
      st=$(kubectl get runaction "${NAME}" -n kasten-io -o jsonpath='{.status.state}' 2>/dev/null || true)
      case "$st" in
        Complete) echo "RunAction=${st}"; break ;;
        Failed|Cancelled) echo "RunAction=${st}"; exit 1 ;;
      esac
      sleep 10
    done
    # The export runs as a child action; wait for it to settle too.
    for _ in $(seq 1 90); do
      pending=$(kubectl get exportaction -n "$NS" \
        -o jsonpath='{range .items[*]}{.status.state}{"\n"}{end}' 2>/dev/null \
        | grep -cE 'Running|Pending' || true)
      [ "${pending:-0}" -eq 0 ] && break
      sleep 10
    done
    kubectl get exportaction -n "$NS" --sort-by=.metadata.creationTimestamp \
      -o custom-columns='NAME:.metadata.name,STATE:.status.state' 2>/dev/null | tail -2
    ;;

  churn)
    N="${2:-1000}"
    echo "Updating ${N} records in test.bench (simulating a delta)..."
    kubectl run "churn-$RANDOM" -n "$NS" --image="$TOOLS_IMAGE" --restart=Never --rm -i --quiet --command -- \
      asbench --hosts "aerocluster.${NS}.svc.cluster.local" --port 3000 \
        --namespace test --set bench --keys "${N}" --object-spec S200 \
        --workload I --threads 8 --random 2>&1 | tail -2
    ;;

  *)
    sed -n '2,24p' "$0"
    exit 1
    ;;
esac
