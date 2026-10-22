#!/usr/bin/env bash
# BackupAction preHook orchestrator. Runs (via a KubeTask in kasten-io, using the
# cluster-wide Kasten SA) BEFORE Kasten's PVC discovery, so every PVC it creates is
# included in the restore point.
#
# For each CP4D analytics project it ensures the keeper is an editor on that project
# (self-enrolling where needed — see below), ensures a permanent, GUID-keyed PVC in
# $BACKUP_NS, and launches one export pod (bounded concurrency) that writes the unzipped
# bundle into that PVC. It then prunes PVCs of deleted projects and deletes the export
# pods (so no export pod — and no credential — is captured in the restore point).
#
# Exit non-zero (=> the whole backup fails) if any project could not be enrolled or exported.
set -euo pipefail
: "${BACKUP_NS:?BACKUP_NS is required}"
: "${IMAGE:?IMAGE (keeper image) is required}"
: "${STORAGE_CLASS:=managed-csi}"
: "${KEEPER_SA:=cp4d-backup-keeper}"
: "${PVC_SIZE:=1Gi}"
: "${MAX_PARALLEL:=4}"
: "${EXPORT_TIMEOUT:=900s}"
: "${ENCRYPTION_KEY:=}"
: "${CRED_NS:=cpd}"
: "${CRED_SECRET:=cp4d-backup-cpdctl-creds}"

# Environment (all overridable; defaults above):
#   BACKUP_NS       (required) namespace holding the per-project PVCs and export pods
#   IMAGE           (required) keeper image run by each export pod
#   STORAGE_CLASS   storage class for the per-project PVCs (CSI, snapshot-capable)
#   KEEPER_SA       ServiceAccount the export pods run as (must have RBAC in BACKUP_NS)
#   PVC_SIZE        size requested for each per-project PVC
#   MAX_PARALLEL    max export pods running concurrently
#   EXPORT_TIMEOUT  per-pod wait timeout (kubectl wait --timeout value)
#   ENCRYPTION_KEY  optional; passed to export-project.sh (empty => plaintext bundle)
#   CRED_NS         namespace holding the credential secret (read cross-namespace)
#   CRED_SECRET     name of that secret (keys: url, username, apikey, uid)

cpdctl-login.sh   # authenticate cpdctl from the cross-namespace credential secret

# --- the keeper's own identity, needed to add itself as a project member -------------
# CP4D requires BOTH user_name and the numeric uid in a member payload (user_name alone
# is rejected: "Field state cannot be set to ACTIVE without specifying a member id"),
# and there is no cpdctl "whoami". So the uid is stored in the credential secret
# alongside the API key rather than resolved at run time.
if [ -n "${CPD_USERNAME:-}" ] && [ -n "${CPD_UID:-}" ]; then
  SVC_USER="$CPD_USERNAME"; SVC_UID="$CPD_UID"
else
  SVC_USER=$(kubectl get secret -n "$CRED_NS" "$CRED_SECRET" -o jsonpath='{.data.username}' | base64 -d)
  SVC_UID=$(kubectl  get secret -n "$CRED_NS" "$CRED_SECRET" -o jsonpath='{.data.uid}'      | base64 -d)
fi
if [ -z "${SVC_UID}" ]; then
  echo "FATAL: secret ${CRED_NS}/${CRED_SECRET} has no 'uid' key."
  echo "       The keeper needs its own numeric uid to enrol itself on projects. Add it:"
  echo "         kubectl patch secret ${CRED_SECRET} -n ${CRED_NS} -p \\"
  echo "           \"{\\\"data\\\":{\\\"uid\\\":\\\"\$(printf %s '<uid>' | base64)\\\"}}\""
  echo "       Get <uid> from: GET /usermgmt/v1/user/<username>  ->  .uid"
  exit 1
fi
echo "keeper identity: ${SVC_USER} (uid ${SVC_UID})"

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

# --- ensure the keeper is an editor on every project (self-enrol where missing) -------
# Seeing a project and being able to export it are separate rights: cluster-wide
# visibility comes from the manage_project permission, but `asset export` requires
# editor-or-above MEMBERSHIP on that specific project (else SPACES0045E). manage_project
# also permits member management, so the keeper can add itself — meaning a project that
# nobody enrolled us on is repaired here instead of being silently skipped.
BM=""; : > /tmp/mine.txt
while :; do
  if [ -z "$BM" ]; then cpdctl project list --member "$SVC_USER" --roles admin,editor --limit 100 --output json > /tmp/m.json
  else                   cpdctl project list --member "$SVC_USER" --roles admin,editor --limit 100 --bookmark "$BM" --output json > /tmp/m.json; fi
  jq -r '.resources[]?.metadata.guid // empty' /tmp/m.json >> /tmp/mine.txt
  n=$(jq '.resources // [] | length' /tmp/m.json)
  BM=$(jq -r '.bookmark // empty' /tmp/m.json)
  { [ "$n" -lt 100 ] || [ -z "$BM" ]; } && break
done
sort -u -o /tmp/mine.txt /tmp/mine.txt
echo "already an editor on $(grep -c . /tmp/mine.txt || true) of ${TOTAL} project(s)"

ENROLL_FAILED=()
while IFS=$'\t' read -r pid name; do
  [ -z "${pid}" ] && continue
  grep -qxF "${pid}" /tmp/mine.txt && continue
  echo "  enrolling as editor on '${name}' (${pid})"
  if ! cpdctl project member create --project-id "${pid}" \
        --members "[{\"user_name\":\"${SVC_USER}\",\"id\":\"${SVC_UID}\",\"role\":\"editor\",\"state\":\"ACTIVE\",\"type\":\"user\"}]" \
        >/tmp/enrol.out 2>&1; then
    echo "  !! could not enrol on '${name}' (${pid}):"; sed 's/^/     /' /tmp/enrol.out
    ENROLL_FAILED+=("${pid}")
  fi
done < /tmp/all.tsv

# Fail before moving any data: an un-enrollable project cannot be exported, and a
# backup that quietly omits a project is worse than one that fails visibly.
if [ "${#ENROLL_FAILED[@]}" -gt 0 ]; then
  echo "BACKUP PREHOOK FAILED: could not become an editor on ${#ENROLL_FAILED[@]} project(s): ${ENROLL_FAILED[*]}"
  echo "  Check that ${SVC_USER} holds the 'manage_project' permission (it grants both"
  echo "  cluster-wide project visibility and member management), and that uid ${SVC_UID} is correct."
  exit 1
fi

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
