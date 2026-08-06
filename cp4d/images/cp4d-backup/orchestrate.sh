#!/usr/bin/env bash
# BackupAction preHook orchestrator. Runs (via a KubeTask in kasten-io, using the
# cluster-wide Kasten SA) BEFORE Kasten's PVC discovery, so every PVC it creates is
# included in the restore point.
#
# For each CP4D analytics project it ensures a permanent, GUID-keyed PVC in $BACKUP_NS
# and launches one export pod (bounded concurrency) that writes the unzipped bundle
# into that PVC. It then prunes PVCs of deleted projects and deletes the export pods
# (so no export pod — and no credential — is captured in the restore point).
#
# Exit non-zero (=> the whole backup fails) if any project could not be exported.
set -euo pipefail
: "${BACKUP_NS:?BACKUP_NS is required}"
: "${IMAGE:?IMAGE (keeper image) is required}"
: "${STORAGE_CLASS:=managed-csi}"
: "${KEEPER_SA:=cp4d-backup-keeper}"
: "${PVC_SIZE:=1Gi}"
: "${MAX_PARALLEL:=4}"
: "${EXPORT_TIMEOUT:=900s}"
: "${ENCRYPTION_KEY:=}"

# Environment (all overridable; defaults above):
#   BACKUP_NS       (required) namespace holding the per-project PVCs and export pods
#   IMAGE           (required) keeper image run by each export pod
#   STORAGE_CLASS   storage class for the per-project PVCs (CSI, snapshot-capable)
#   KEEPER_SA       ServiceAccount the export pods run as (must have RBAC in BACKUP_NS)
#   PVC_SIZE        size requested for each per-project PVC
#   MAX_PARALLEL    max export pods running concurrently
#   EXPORT_TIMEOUT  per-pod wait timeout (kubectl wait --timeout value)
#   ENCRYPTION_KEY  optional; passed to export-project.sh (empty => plaintext bundle)

cpdctl-login.sh   # authenticate cpdctl from the cross-namespace credential secret

# sanitize NAME -> DNS-1123-safe fragment (lowercase, [a-z0-9-] only, no
# leading/trailing dash, <=40 chars). Used to build a readable PVC name; the
# 8-char GUID prefix appended by the caller guarantees uniqueness.
sanitize(){ echo "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' \
              | sed -E 's/-+/-/g; s/^-//; s/-$//' | cut -c1-40 | sed -E 's/-$//'; }

# --- enumerate all analytics projects (paginated) -> /tmp/all.tsv (guid \t name) ---
BM=""; : > /tmp/all.tsv
while :; do
  if [ -z "$BM" ]; then cpdctl project list --limit 100 --output json > /tmp/p.json
  else                   cpdctl project list --limit 100 --bookmark "$BM" --output json > /tmp/p.json; fi
  jq -r '.resources[] | [.metadata.guid, .entity.name] | @tsv' /tmp/p.json >> /tmp/all.tsv
  n=$(jq '.resources | length' /tmp/p.json)
  BM=$(jq -r '.bookmark // empty' /tmp/p.json)
  { [ "$n" -lt 100 ] || [ -z "$BM" ]; } && break
done
TOTAL=$(grep -c . /tmp/all.tsv || true)
echo "discovered ${TOTAL} project(s)"

BATCH="b$(date +%s)"
declare -A PID_SEEN
FAILED_PIDS=()

# launch PID NAME
#   Ensure the permanent PVC for project PID exists (created idempotently via
#   `kubectl apply`, keyed/labelled by GUID so it survives across backups and is
#   discovered by Kasten) and (re)create its export pod, which runs
#   export-project.sh to write the bundle into that PVC. Records PID in PID_SEEN
#   (consumed by the prune step). Returns as soon as the pod is applied — use
#   wait_pod to await completion.
launch(){ # pid name
  local pid="$1" name="$2" pvc podname
  pvc="cp4d-$(sanitize "$name")-${pid:0:8}"
  podname="export-${pid:0:8}"
  PID_SEEN["$pid"]=1
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${pvc}
  namespace: ${BACKUP_NS}
  labels: { cp4d.io/project-id: "${pid}", app.kubernetes.io/managed-by: cp4d-projects-backup }
  annotations: { cp4d.io/project-name: "${name}" }
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ${STORAGE_CLASS}
  resources: { requests: { storage: ${PVC_SIZE} } }
EOF
  kubectl delete pod "${podname}" -n "${BACKUP_NS}" --ignore-not-found >/dev/null 2>&1
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${podname}
  namespace: ${BACKUP_NS}
  labels: { cp4d.io/export-batch: "${BATCH}" }
spec:
  serviceAccountName: ${KEEPER_SA}
  restartPolicy: Never
  containers:
    - name: export
      image: ${IMAGE}
      imagePullPolicy: Always
      command: ["export-project.sh"]
      env:
        - { name: PID,            value: "${pid}" }
        - { name: PROJECT_NAME,   value: "${name}" }
        - { name: ENCRYPTION_KEY, value: "${ENCRYPTION_KEY}" }
      volumeMounts: [ { name: backup, mountPath: /backup } ]
  volumes:
    - name: backup
      persistentVolumeClaim: { claimName: ${pvc} }
EOF
  echo "  launched ${podname} -> pvc ${pvc}"
}

# wait_pod PODNAME  -> 0 if the pod reached phase=Succeeded, 1 otherwise
#   Blocks up to EXPORT_TIMEOUT. On failure prints the phase and the last few
#   log lines (indented) for diagnosis. Never itself aborts the script (the
#   `kubectl wait` failure is swallowed), so the caller decides how to react.
wait_pod(){ # podname -> 0 ok / 1 fail
  local p="$1" ph
  kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/${p}" -n "${BACKUP_NS}" \
    --timeout="${EXPORT_TIMEOUT}" >/dev/null 2>&1 || true
  ph=$(kubectl get pod "${p}" -n "${BACKUP_NS}" -o jsonpath='{.status.phase}' 2>/dev/null || echo Unknown)
  [ "${ph}" = Succeeded ] && return 0
  echo "  !! ${p} phase=${ph}"
  kubectl logs "${p}" -n "${BACKUP_NS}" --tail=8 2>/dev/null | sed 's/^/     /'
  return 1
}

# process   (reads "guid<TAB>name" lines from stdin)
#   Launch export pods in bounded batches of MAX_PARALLEL: launch up to
#   MAX_PARALLEL pods, wait for that whole batch, then start the next. Any pod
#   that does not succeed has its GUID appended to FAILED_PIDS (retried once by
#   the caller). Blank lines are skipped.
process(){ # reads guid \t name from stdin, launches in bounded batches
  local -a pods=() pids=(); local pid name cnt=0 idx
  while IFS=$'\t' read -r pid name; do
    [ -z "${pid}" ] && continue
    launch "${pid}" "${name}"
    pods+=("export-${pid:0:8}"); pids+=("${pid}"); cnt=$((cnt+1))
    if [ "${cnt}" -ge "${MAX_PARALLEL}" ]; then
      for idx in "${!pods[@]}"; do wait_pod "${pods[$idx]}" || FAILED_PIDS+=("${pids[$idx]}"); done
      pods=(); pids=(); cnt=0
    fi
  done
  for idx in "${!pods[@]}"; do wait_pod "${pods[$idx]}" || FAILED_PIDS+=("${pids[$idx]}"); done
}

process < /tmp/all.tsv

# one retry for any failed project (export is idempotent)
if [ "${#FAILED_PIDS[@]}" -gt 0 ]; then
  echo "retrying ${#FAILED_PIDS[@]} failed project(s)"
  mapfile -t RETRY < <(printf '%s\n' "${FAILED_PIDS[@]}"); FAILED_PIDS=()
  : > /tmp/retry.tsv
  for pid in "${RETRY[@]}"; do grep -P "^${pid}\t" /tmp/all.tsv >> /tmp/retry.tsv || true; done
  process < /tmp/retry.tsv
fi

# prune PVCs whose CP4D project no longer exists
for pvc in $(kubectl get pvc -n "${BACKUP_NS}" -l app.kubernetes.io/managed-by=cp4d-projects-backup \
               -o jsonpath='{.items[*].metadata.name}'); do
  pid=$(kubectl get pvc "${pvc}" -n "${BACKUP_NS}" -o jsonpath='{.metadata.labels.cp4d\.io/project-id}')
  if [ -z "${PID_SEEN[$pid]:-}" ]; then
    echo "pruning ${pvc} (project ${pid} no longer exists)"
    kubectl delete pvc "${pvc}" -n "${BACKUP_NS}" --ignore-not-found >/dev/null
  fi
done

# delete export pods so neither the pod nor any transient state is captured in the backup
kubectl delete pod -n "${BACKUP_NS}" -l cp4d.io/export-batch="${BATCH}" --ignore-not-found >/dev/null 2>&1 || true

if [ "${#FAILED_PIDS[@]}" -gt 0 ]; then
  echo "BACKUP PREHOOK FAILED: ${#FAILED_PIDS[@]} project(s) failed to export: ${FAILED_PIDS[*]}"
  exit 1
fi
echo "OK: all ${TOTAL} project(s) exported to per-project PVCs in ${BACKUP_NS}"
