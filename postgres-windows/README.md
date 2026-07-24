# PostgreSQL on a Windows KubeVirt VM — Kasten Quiesce Blueprint

Application-consistent Kasten/Kanister backup of a **PostgreSQL** database running
**inside a Windows Server VM** on **OpenShift Virtualization (KubeVirt)**.

## Pattern

**Quiesce (Kasten is the data mover).** Kasten snapshots the VM's DataVolume PVC
and **owns the guest-filesystem freeze/thaw**. This blueprint only issues a
Postgres-aware `CHECKPOINT` in `backupPrehook` so the captured disk is clean and
fast to recover — Postgres replays WAL (crash recovery) when the restored VM
boots. Nothing is copied by the blueprint itself.

```
backupPrehook (CHECKPOINT)  ->  Kasten freezes guest FS  ->  snapshot VM PVC  ->  Kasten thaws
```

### Why this pattern

- The DB lives on the single Windows OS disk (one DataVolume) — there is no
  replica and no separate mountable data PVC, so replica-fence / keeper-PVC
  patterns don't apply.
- Quiesce is incremental (block-level CSI snapshots), keeps Kasten as the data
  mover, and is the highest-ranked feasible pattern for this workload.
- A logical `pg_dump` to the OS disk was rejected: it bloats the disk and is not
  incremental.

### How the guest is driven (QEMU guest-agent `guest-exec`)

KubeVirt exposes no direct guest-exec API, so the blueprint runs guest commands
through the QEMU guest agent over libvirt, from the VM's `virt-launcher` pod:

```
kubectl exec <virt-launcher-pod> -c compute -- \
  virsh qemu-agent-command <namespace>_<vmname> '{"execute":"guest-exec", ...}'
```

The KubeTask pods run in `kasten-io` as the **`kanister-svc`** service account,
which already has `get pods` + `pods/exec` in the application namespace (no extra
RBAC required). The Postgres password is read from a Kubernetes Secret and
embedded in the PowerShell command as a single-quoted literal; it is **not**
passed via the guest-exec `env` array (that would wipe the guest `PATH`) and is
never echoed to logs.

Both actions share **one worker script** (a YAML anchor) dispatched by a
per-phase argument (`checkpoint` / `verify-restore`), so the guest-exec logic
exists in exactly one place.

## Prerequisites

1. **A Windows VM running PostgreSQL** with the QEMU guest agent connected
   (`kubectl get vmi <vm> -n <ns> -o jsonpath='{.status.conditions[?(@.type=="AgentConnected")].status}'`
   must be `True`).
2. PostgreSQL reachable on `127.0.0.1:5432` inside the guest with a known
   superuser password (`scram-sha-256` auth).

### Tools image

The blueprint's KubeTask phases need `kubectl` + `jq` (and `kando` from the
kanister-tools base). It uses `michaelcourcy/kasten-tools:8.5.2`, built from the shared
[../images/kasten-tools/Dockerfile](../images/kasten-tools/Dockerfile).

## Install the blueprint

```bash
# 1. Credentials Secret (kasten-io). Do not commit real passwords.
kubectl create secret generic postgres-winvm-creds -n kasten-io \
  --from-literal=password='<your-postgres-password>'

# 2. Blueprint
kubectl apply -f blueprint.yaml

# 3a. Bind a single VM by annotation:
kubectl annotate vm <vm-name> -n <app-ns> \
  kanister.kasten.io/blueprint=postgres-winvm-blueprint

# 3b. OR fleet-bind by label (edit the label key/values as needed):
kubectl label vm <vm-name> -n <app-ns> kasten.bjones/postgres-winvm=true
kubectl apply -f blueprintbinding.yaml
```

## ⚠️ Manual restore steps (Kasten ≤ 8.5.x)

The blueprint intentionally has **no `restorePrehook`**: for this quiesce pattern
the restore replaces the VM DataVolume and Postgres performs crash recovery on
boot, so there are **no manual pre-restore steps** required. The `restorePosthook`
verification depends on Kasten triggering restore hooks; until that ships, verify
the restore manually after the VM boots:

```bash
# After the restored VM's guest agent reconnects:
kubectl get vmi <vm-name> -n <app-ns> \
  -o jsonpath='{.status.conditions[?(@.type=="AgentConnected")].status}'
# then via guest-exec, confirm: service postgresql-x64-18 Running and
#   SELECT count(*) FROM database;  -> foobar
```

## Step 5 — Test end-to-end

```bash
# 1. Trigger the backup with a RunAction.
kubectl create -f - --validate=false <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata:
  generateName: run-
  namespace: kasten-io
spec:
  subject:
    apiVersion: config.kio.kasten.io/v1alpha1
    kind: Policy
    name: postgres-winvm-backup
    namespace: kasten-io
EOF

# 2. Watch the blueprint execute.
kubectl logs -n kasten-io -l component=executor --tail=200 -f
```

Verify the RunAction reaches `Complete`

```bash
# List restore points (application namespace)
kubectl get restorepoint -n <app-ns>
```
