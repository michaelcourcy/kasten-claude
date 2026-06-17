# OpenShift etcd backup with Kasten — permanent keeper PVC

Back up an **OpenShift control-plane etcd** with Kasten, writing the etcd snapshot
to a **permanent PVC** that Kasten then snapshots. No object storage client, no
cloud identity — Kasten is the data mover.

> **Credit / origin.** This blueprint is adapted from
> [**Jaiganeshjk12/etcd-bp-azcopy**](https://github.com/Jaiganeshjk12/etcd-bp-azcopy),
> which runs the same OpenShift `cluster-backup.sh` flow but uploads the result to
> Azure Blob Storage with `azcopy` + federated workload identity. This version
> **strips out all the azcopy and federated-identity logic** and replaces the
> ephemeral `backup-volume` with a **permanent PVC** mounted by a long-lived
> *keeper* Deployment. Kasten snapshots that PVC, so the PVC snapshot *is* the
> backup — there is nothing to upload and nothing to delete.

---

## Pattern

**Pattern 4 — database dump on a permanent Keeper PVC (PVC mounted by keeper).**

A dedicated **keeper Deployment** runs the backup-tool image, mounts the permanent
backup PVC at `/backup`, and is pinned to a control-plane node. Kasten's PVC
discovery sees the permanent PVC *before* `backupPrehook` runs, so it is correctly
included in the restore point. `backupPrehook` `KubeExec`s into the keeper pod and
runs the node's own `cluster-backup.sh`. The blueprint binds to the keeper
Deployment via a `BlueprintBinding` (preserves SRP/OCP).

### Architecture

```
control-plane node (master)
┌──────────────────────────────────────────────────────────┐
│  host /etc/kubernetes  (etcd static-pod resources, certs,  │
│                         cluster-backup.sh)                 │
│          │ hostPath, read-only                             │
│          ▼                                                 │
│   ┌──────────────────────────┐                            │
│   │ keeper pod (Deployment)  │   image: michaelcourcy/    │
│   │  - etcdctl / etcdutl     │          ocp-etcd-backup    │
│   │  - runs cluster-backup.sh│                            │
│   │      → /backup           │──── writes snapshot ───┐    │
│   └──────────────────────────┘                        │    │
│                                                       ▼    │
│                              permanent PVC  ocp-etcd-backup │
└──────────────────────────────────────────────────────────┘
                                       │
              Kasten snapshots this PVC ▼  = restore point
```

- **backupPrehook** (`KubeExec` into keeper): clean `/backup/etcd-backup`, run
  `cluster-backup.sh --force`, normalize `__POSSIBLY_DIRTY__` filenames, `sync`.
- Kasten takes a CSI snapshot of the keeper PVC → restore point.
- **backupPosthook** (`KubeExec` into keeper): verify the snapshot with
  `etcdutl snapshot status`.
- **No restore action** and **no delete action** (see below).

---

## Versions

| Component | Version |
|---|---|
| OpenShift | `4.18.6` |
| Kubernetes | `1.31.6` |
| Kasten | `8.5.12` (detect with `helm ls -n kasten-io`) |
| etcd (cluster) | `3.5.18` |
| Keeper image | `michaelcourcy/ocp-etcd-backup:8.5.12` (UBI9-minimal + `etcdctl`/`etcdutl` 3.5.18 + `kubectl` 1.31.6) |

There is **no operator** — etcd is part of the OpenShift control plane.

---

## The keeper image

`michaelcourcy/ocp-etcd-backup:8.5.12` (public). Built from
[`images/ocp-etcd-backup/Dockerfile`](images/ocp-etcd-backup/Dockerfile):

- Base: `registry.access.redhat.com/ubi9/ubi-minimal`
- `etcdctl` + `etcdutl` copied from `quay.io/coreos/etcd:v3.5.18` (matched to the
  cluster's etcd version)
- `kubectl` `v1.31.6` (matched to the cluster's Kubernetes version) — used by the
  `backupPrehook` operator-progression check (see below)
- `bash`, `tar`, `gzip`, `coreutils`, `util-linux` (for `sync`)

`cluster-backup.sh` is **not** baked in — it is invoked from the control-plane node
through the read-only `/etc/kubernetes` hostPath mount. The script's `dl_etcdctl`
function finds the `etcdctl`/`etcdutl` already in the image's `PATH` (no `podman`
in the container) and skips its own download, so the image must provide them.

To rebuild:

```bash
cd images/ocp-etcd-backup
docker build --platform linux/amd64 -t michaelcourcy/ocp-etcd-backup:8.5.12 .
docker push michaelcourcy/ocp-etcd-backup:8.5.12
```

---

## Prerequisites

### 1. VolumeSnapshotClass annotated for Kasten

Kasten only uses a `VolumeSnapshotClass` carrying `k10.kasten.io/is-snapshot-class: true`.
Annotate the one for your CSI driver:

```bash
# Azure disk (this test cluster):
kubectl annotate volumesnapshotclass csi-azuredisk-vsc k10.kasten.io/is-snapshot-class=true --overwrite
```

> **Storage class**: the manifests use `managed-csi` (Azure disk CSI) with the
> `csi-azuredisk-vsc` VolumeSnapshotClass — the values for our test cluster.
> Replace them with a CSI storage class that supports snapshots on your cluster
> (`ebs-sc` on AWS, `standard-rwo` on GKE, your custom class on bare-metal, etc.)
> and a matching `VolumeSnapshotClass` registered with Kasten. Do **not** use
> legacy in-tree classes (e.g. `gp2`) — they do not support CSI snapshots.

### 2. The keeper needs the `privileged` SCC

The keeper mounts host `/etc/kubernetes`, runs as root, and uses
`seLinuxOptions.type: spc_t`. That requires the `privileged` SCC and `privileged`
Pod Security Admission on the namespace (both done in the deploy step below).

### 3. The keeper needs to read ClusterOperators (operator-progression check)

Before taking the backup, `backupPrehook` verifies that no control-plane operator
is mid-rollout — a reliable etcd backup requires that no rollout is in progress,
otherwise the captured static-pod resources can be inconsistent with etcd.

`cluster-backup.sh` performs this check itself, but **only when run without
`--force`**, using `oc` + the node's `localhost.kubeconfig` (server
`https://localhost:6443`). A normal (non-`hostNetwork`) pod cannot reach that
endpoint, so — exactly like the upstream
[`etcd-bp-azcopy`](https://github.com/Jaiganeshjk12/etcd-bp-azcopy)
`checkOperatorProgression` phase — we run `--force` and replicate the check against
the **in-cluster API** with `kubectl get co`. This needs a small cluster-scoped
read grant, included in [`keeper.yaml`](keeper.yaml) as a `ClusterRole` +
`ClusterRoleBinding` (`get`/`list` on `clusteroperators.config.openshift.io` for the
`etcd-backup-keeper` ServiceAccount).

---

## Step 1 — Deploy the keeper (namespace, PVC, Deployment)

[`keeper.yaml`](keeper.yaml) creates namespace `etcd-backup`, ServiceAccount
`etcd-backup-keeper`, the permanent PVC `ocp-etcd-backup` (10Gi), and the keeper
Deployment pinned to a control-plane node.

```bash
kubectl apply -f keeper.yaml

# allow hostPath + spc_t + runAsUser 0
oc adm policy add-scc-to-user privileged -z etcd-backup-keeper -n etcd-backup
kubectl label ns etcd-backup \
  pod-security.kubernetes.io/enforce=privileged \
  pod-security.kubernetes.io/warn=privileged \
  pod-security.kubernetes.io/audit=privileged --overwrite

# the deployment was created before the SCC grant; restart so the pod is admitted
kubectl rollout restart deployment/ocp-etcd-backup-keeper -n etcd-backup
kubectl rollout status  deployment/ocp-etcd-backup-keeper -n etcd-backup
```

Confirm the keeper is running on a master and can see the host script:

```bash
POD=$(kubectl get pods -n etcd-backup -l app=ocp-etcd-backup-keeper -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n etcd-backup $POD -- \
  ls -l /etc/kubernetes/static-pod-resources/etcd-certs/configmaps/etcd-scripts/cluster-backup.sh
```

---

## Step 2 — (Optional) validate the backup by hand

Prove the flow works before involving Kasten:

```bash
POD=$(kubectl get pods -n etcd-backup -l app=ocp-etcd-backup-keeper -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n etcd-backup $POD -- bash -c '
  rm -rf /backup/etcd-backup && mkdir -p /backup/etcd-backup
  /etc/kubernetes/static-pod-resources/etcd-certs/configmaps/etcd-scripts/cluster-backup.sh --force /backup/etcd-backup
  cd /backup/etcd-backup
  shopt -s nullglob; for f in *__POSSIBLY_DIRTY__*; do mv "$f" "${f/__POSSIBLY_DIRTY__/}"; done
  ls -lh
  etcdutl snapshot status snapshot_*.db -w table
  sync'
```

You should get a `snapshot_*.db` (~90 MB) and a `static_kuberesources_*.tar.gz`.

---

## Step 3 — Deploy the blueprint

```bash
kubectl apply -f blueprint.yaml          # Blueprint in kasten-io
kubectl apply -f blueprintbinding.yaml   # binds to Deployments labelled etcd-backup-keeper=true
```

### Blueprint actions

| Action | Function | What it does |
|---|---|---|
| `backupPrehook` | `KubeExec` (keeper) | Checks the four control-plane operators (`kube-apiserver`, `kube-controller-manager`, `kube-scheduler`, `etcd`) are not `Progressing` and **aborts if a rollout is in flight**; then cleans `/backup/etcd-backup`, runs `cluster-backup.sh --force`, normalizes `__POSSIBLY_DIRTY__` filenames, runs `sync`. |
| `backupPosthook` | `KubeExec` (keeper) | Verifies a non-empty `snapshot_*.db` exists and runs `etcdutl snapshot status`; checks the `static_kuberesources_*.tar.gz` is present. |

There is intentionally **no `restore*` action** and **no `delete` action** — see the
two notes below.

---

## Step 4 — Back up through Kasten

You need a **Location** profile so Kasten wires up the Kanister hooks (the blueprint
itself never writes to it). Any S3/Azure/GCS Location profile works. Example with a
local MinIO S3 endpoint:

```bash
# Location profile must be type "Location" (not "Infra"), or restore can't extract it:
kubectl get profile <name> -n kasten-io -o jsonpath='{.spec.type}'
```

Create an on-demand policy bound to the `etcd-backup` namespace and run it:

```bash
kubectl apply -f - <<'EOF'
apiVersion: config.kio.kasten.io/v1alpha1
kind: Policy
metadata:
  name: etcd-backup-policy
  namespace: kasten-io
spec:
  comment: "OpenShift etcd backup via keeper blueprint"
  frequency: "@onDemand"
  actions:
    - action: backup
      backupParameters:
        profile:
          name: <YOUR_LOCATION_PROFILE>
          namespace: kasten-io
  selector:
    matchExpressions:
      - key: k10.kasten.io/appNamespace
        operator: In
        values: [etcd-backup]
EOF

kubectl create -f - <<'EOF'
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata:
  generateName: run-etcd-
  namespace: kasten-io
spec:
  subject:
    apiVersion: config.kio.kasten.io/v1alpha1
    kind: Policy
    name: etcd-backup-policy
    namespace: kasten-io
EOF
```

Watch it and confirm the hooks fired:

```bash
kubectl get runaction -n kasten-io -w
kubectl logs -n kasten-io deploy/kanister-svc --tail=200 | grep -E "etcdBackup|etcdVerify"
kubectl get restorepoint -n etcd-backup
```

---

## Restore

> ### ⚠️ Two different things are called "restore"
>
> 1. **Restoring the *backup artifact* (what this blueprint/Kasten does).**
>    A Kasten restore of the `etcd-backup` namespace brings back the keeper PVC (and
>    Deployment) with the `snapshot_*.db` + `static_kuberesources_*.tar.gz` files on
>    it. This is fully automated and tested below.
>
> 2. **Restoring the *control-plane etcd* onto the cluster (manual, disruptive).**
>    Actually rolling the cluster back to that snapshot is the OpenShift
>    *"Restoring to a previous cluster state"* procedure (`cluster-restore.sh` on a
>    master, then recover the other masters). This is **never** automated from a
>    Kasten hook — it takes the API server down and must be driven by an admin.

### 1. Restore the backup artifact with Kasten

```bash
RP=$(kubectl get restorepoint -n etcd-backup -o jsonpath='{.items[0].metadata.name}')
kubectl create -f - <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-etcd-
  namespace: etcd-backup
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: $RP
    namespace: etcd-backup
  targetNamespace: etcd-backup
EOF
```

> No `profile` is set on the `RestoreAction` — Kasten extracts the location profile
> from the `RestorePointContent` automatically.

Verify the snapshot is back and valid:

```bash
POD=$(kubectl get pods -n etcd-backup -l app=ocp-etcd-backup-keeper --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n etcd-backup $POD -- bash -c 'ls -lh /backup/etcd-backup; etcdutl snapshot status /backup/etcd-backup/snapshot_*.db -w table'
```

### 2. Restore the control-plane etcd onto the cluster (manual)

This blueprint deliberately does **not** automate this. Follow the official
OpenShift procedure, using the files now on the keeper PVC:

- Red Hat docs: **"Restoring to a previous cluster state"**
  <https://docs.openshift.com/container-platform/4.18/backup_and_restore/control_plane_backup_and_restore/disaster_recovery/scenario-2-restoring-cluster-state.html>

High-level (see the docs for the authoritative, version-specific steps):

1. Copy `snapshot_*.db` and `static_kuberesources_*.tar.gz` from the keeper PVC
   onto a chosen recovery master (e.g. under `/home/core/assets/backup/`). You can
   `kubectl cp` them out of the keeper pod first:
   ```bash
   kubectl cp etcd-backup/$POD:/backup/etcd-backup ./etcd-backup-artifact
   ```
2. On the recovery master (via `oc debug node/<master>` → `chroot /host`), run
   `/usr/local/bin/cluster-restore.sh /home/core/assets/backup`.
3. Restart the kubelet and recover the remaining control-plane nodes as described
   in the docs, then re-validate cluster operators.

### `restorePrehook` — intentionally omitted

CLAUDE.md normally asks for a forward-compatible `restorePrehook` (it is not yet
triggered in Kasten ≤ 8.5.x). For this workload there is **no namespace-level
quiesce/init that makes sense**: the only meaningful "restore" of etcd is the manual
control-plane procedure above, which must not run from a hook. Restoring the keeper
PVC needs no preparation. The blueprint therefore has no `restore*` actions by
design.

---

## No delete action

Deleting the Kasten restore point deletes the underlying PVC snapshot — which is the
only copy of the backup. There is no external object-store copy to clean up, so the
blueprint has **no `delete` action**. (This is the deliberate trade-off for dropping
the azcopy upload from the original repo.)

---

## Inspecting / navigating backups

The keeper pod is permanent, so you can always browse the current backup artifact:

```bash
POD=$(kubectl get pods -n etcd-backup -l app=ocp-etcd-backup-keeper -o jsonpath='{.items[0].metadata.name}')
kubectl exec -it -n etcd-backup $POD -- bash
#   ls -lh /backup/etcd-backup
#   etcdutl snapshot status /backup/etcd-backup/snapshot_*.db -w table
```

---

## Cleanup

```bash
# Kasten objects
kubectl delete policy etcd-backup-policy -n kasten-io --ignore-not-found
kubectl delete blueprintbinding ocp-etcd-backup-binding -n kasten-io --ignore-not-found
kubectl delete blueprint ocp-etcd-backup -n kasten-io --ignore-not-found

# Restore points and their snapshot content
kubectl delete restorepoint -l k10.kasten.io/appNamespace=etcd-backup -n etcd-backup
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=etcd-backup

# Keeper + permanent PVC (this deletes the backup data!)
kubectl delete -f keeper.yaml

# (optional) the MinIO S3 endpoint and profile used only to satisfy Kasten's wiring
kubectl delete namespace minio --ignore-not-found
kubectl delete profile <YOUR_LOCATION_PROFILE> -n kasten-io --ignore-not-found
```
