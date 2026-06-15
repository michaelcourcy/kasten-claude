# CNPG Barman Cloud **Plugin** Blueprint (Pattern 5 — MinIO Keeper)

Kasten blueprint for CloudNativePG clusters that archive WAL and base backups to a **local MinIO
instance via the [Barman Cloud Plugin](https://cloudnative-pg.io/plugin-barman-cloud/)** (CNPG-I).
Kasten snapshots the MinIO PVC (which contains the full barman archive), enabling Point-In-Time
Recovery (PITR) from any Kasten restore point.

This is the **plugin-based successor** to [../cnpg-barman/](../cnpg-barman/), which used the
**in-tree** barman-cloud integration (`spec.backup.barmanObjectStore` + `Backup` with
`method: barmanObjectStore`). That in-tree integration is deprecated and will be removed in CNPG
1.30.0. The design and Kasten integration here are identical — only the CNPG-side syntax changed:

| Concern | in-tree (`cnpg-barman/`) | plugin (this folder) |
|---|---|---|
| Object-store config | `spec.backup.barmanObjectStore` on the Cluster | dedicated `ObjectStore` CR (`barmancloud.cnpg.io/v1`) |
| WAL archiving | built into the operator | `spec.plugins[barman-cloud.cloudnative-pg.io]` + injected sidecar |
| Backup CR | `method: barmanObjectStore` | `method: plugin` + `pluginConfiguration.name` |
| Recovery source | `externalClusters[].barmanObjectStore` | `externalClusters[].plugin` |
| Recovery write target | **separate bucket required** | same bucket, distinct `serverName` prefix |
| Prerequisites | none beyond the operator | CNPG ≥ 1.26, cert-manager, plugin install |

## Versions

Exact versions used when this blueprint was developed and tested end-to-end:

| Component | Version |
|---|---|
| Kubernetes | `1.32` (EKS) |
| Kasten | `8.5.9` |
| CNPG operator | `cloudnative-pg 1.29.0` |
| Barman Cloud Plugin | `plugin-barman-cloud v0.13.0` |
| cert-manager | `v1.19.2` |
| PostgreSQL | `18.3` (`ghcr.io/cloudnative-pg/postgresql:18.3`) |
| MinIO | `RELEASE.2025-04-22T22-12-26Z` |

> Detect your own Kasten version with `helm ls -n kasten-io`.

## Pattern

**Pattern 5 — Database backup via a local MinIO keeper (vendor operator data mover).**

A dedicated MinIO `Deployment` (the *keeper*) with a permanent PVC stores the CNPG barman archive.
The Barman Cloud Plugin, configured by an `ObjectStore` CR, archives WAL and base backups to that
MinIO. The `backupPrehook` blueprint action:

1. Creates a CNPG `Backup` CR with `method: plugin` to trigger a barman base backup.
2. Polls until the base backup reaches `completed`.
3. Forces a WAL segment switch (`pg_switch_wal()`) so the post-backup checkpoint WAL is flushed to
   MinIO before Kasten snapshots the PVC.

Kasten then snapshots the MinIO PVC (and only that PVC — CNPG data PVCs are excluded via policy
filter). The MinIO snapshot contains a self-consistent barman archive: a base backup plus all WALs
up to the switch.

> **Why the WAL switch is necessary here, but not in a typical barman setup**
>
> When barman archives to an external object store, the final WAL segment produced by
> `pg_backup_stop()` will eventually arrive in the bucket on its own — the archiver keeps running and
> recovery fetches WAL on demand. There is no snapshot deadline, so the timing of that last segment
> does not matter in practice.
>
> Here the situation is different. The Backup CR reaches `completed` once the **base backup files**
> are fully uploaded to MinIO. But `pg_backup_stop()` also generates a WAL record at the *stop LSN* —
> the exact position PostgreSQL must replay to reach a consistent state from that base backup. That
> record lands in the *currently-active, partially-filled* WAL segment on the primary pod's `pg_wal`
> directory. Because PostgreSQL's WAL archiver only ships **complete** 16 MB segments, that partial
> segment stays local and unarchived — potentially indefinitely on an idle cluster.
>
> Kasten snapshots the MinIO PVC immediately after the blueprint phase returns. The snapshot is
> frozen at that instant. If the stop-LSN WAL segment has not been archived yet, the snapshot is
> missing the one WAL piece that makes the base backup usable for recovery.
>
> `pg_switch_wal()` closes the current segment and hands it to the WAL archiver right now. The
> 15-second wait gives the archiver time to upload it to MinIO. After that, the snapshot contains
> both the base backup and the complementary WAL needed to reach a consistent recovery point.

**Why this pattern:**
The goal is to give Kasten full ownership of data protection while keeping barman's scope strictly
local to the namespace. Barman writes to a MinIO instance that lives in the same namespace — it
never leaves it. Kasten then takes over everything beyond that boundary:

- **Encryption at rest** — Kasten encrypts snapshot data at the storage layer; barman has no equivalent.
- **Secure, authenticated data movement** — Kasten handles egress to the backup target; the application team never needs credentials for the external location profile.
- **Immutability** — Kasten enforces immutability at the backup target; barman retention policies alone cannot guarantee this.
- **Broad backup target support** — Kasten can protect to NFS, Veeam Vault, VBR, and other targets that barman-cloud does not support.
- **Incrementality** — Kasten's PVC snapshot mechanism is incremental; a plain barman export to an external store is a full transfer each time.
- **Local restore cache** — because MinIO is in the same namespace, a restore from the most recent restore point reads directly from the local PVC snapshot, which is fast.
- **Credential isolation** — the application team only needs MinIO credentials inside their namespace; they never share the Kasten location profile credentials.
- **Repeatable, self-contained configuration** — every component lives entirely within the application namespace; the same manifests deploy verbatim in a new namespace just by changing the name, making this an ideal candidate for productisation with Helm.

## Architecture

```
┌──────────────────────────────────────┐
│  Namespace: cnpg-barman-cloud         │
│                                       │
│  ┌──────────┐   WAL + base backups    │
│  │  CNPG    │  via plugin sidecar      │
│  │  Cluster │──────────────────────►  │
│  │ (+ plugin│   (barman-cloud)         │
│  │  sidecar)│        │                 │
│  └──────────┘        ▼                 │
│   PVCs (excluded  ┌────────┐           │
│   from snapshot)  │ MinIO  │           │
│                   │ Keeper │           │
│                   │  PVC   │◄──────────┼── Kasten snapshots this
│                   └────────┘           │
│                                        │
│  ObjectStore CR ──► tells the plugin   │
│  (barmancloud)      where/how to write │
└──────────────────────────────────────┘

Plugin controller runs once per cluster in: cnpg-system (namespace of the CNPG operator)
```

Each Postgres pod runs **two containers**: `postgres` and the injected `plugin-barman-cloud`
sidecar that performs WAL archiving and base backups.

The BlueprintBinding targets Deployments labelled `cnpg-barman-cloud-minio: "true"`. The MinIO
Deployment name must follow the convention `<cluster-name>-minio` — the blueprint derives the CNPG
cluster name by stripping the `-minio` suffix.

The CNPG Cluster carries the label `cnpg-backup-pattern: barman-cloud-plugin`, which prevents the
[cnpg-blueprint-binding](../cnpg/blueprintbinding.yaml) (Pattern 1) from also binding to it.

## Blueprint actions

| Action | Hook | What it does |
|---|---|---|
| `backupPrehook` | Before PVC snapshot | Creates CNPG `Backup` CR (`method: plugin`), waits for completion, forces WAL switch; outputs `backupName` and `namespace` as restore-point artifacts |
| `delete` | On restore-point deletion | Deletes the CNPG `Backup` CR from the application namespace; silently no-ops if the namespace or Backup CR no longer exists |

No restore hooks are implemented — PITR recovery is a **manual procedure** using a CNPG recovery
cluster manifest (see [PITR Recovery Procedure](#pitr-recovery-procedure) below).

> **Why a `delete` action?** Deleting a Kasten restore point only removes the MinIO PVC snapshot.
> The CNPG `Backup` CR objects accumulate in the namespace otherwise. The `delete` action removes the
> matching `Backup` CR so they do not pile up.
>
> Note that with the plugin model, **pruning of the barman data inside MinIO is governed by the
> `ObjectStore.spec.retentionPolicy`**, applied by the plugin on its own schedule — not by deleting
> the `Backup` CR. This differs from the in-tree behaviour. Set `retentionPolicy` appropriately
> (see [Sizing retentionPolicy](#sizing-retentionpolicy-relative-to-the-kasten-backup-interval)).

## Prerequisites

- **CNPG operator ≥ 1.26** (tested on 1.29.0). Check: `kubectl -n cnpg-system get deploy cnpg-cloudnative-pg -o jsonpath='{.spec.template.spec.containers[0].image}'`
- **cert-manager** installed (the plugin uses it for the operator↔plugin mTLS). Check: `kubectl get deploy -n cert-manager`
- **Barman Cloud Plugin** installed in the operator namespace (`cnpg-system`):
  ```bash
  kubectl apply -f https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/v0.13.0/manifest.yaml
  kubectl -n cnpg-system rollout status deploy/barman-cloud
  ```
- `michaelcourcy/kasten-tools:8.5.2` image available (adds `kubectl` to `gcr.io/kasten-images/kanister-tools:8.5.2`).
- Blueprint and BlueprintBinding deployed in `kasten-io`.
- Kasten backup policy configured with the PVC filter described below.
- The Pattern 1 `cnpg-blueprint-binding` must list `barman-cloud-plugin` in its `NotIn` exclusion
  (already present in [../cnpg/blueprintbinding.yaml](../cnpg/blueprintbinding.yaml)).

## Deploying the workload

### 1. Create the namespace

```bash
kubectl create namespace cnpg-barman-cloud
```

### 2. Deploy the MinIO keeper

Review [minio-keeper.yaml](minio-keeper.yaml) before applying — in particular the
`storageClassName` on the PVC.

> **Storage class**: `ebs-sc` is the AWS EBS CSI storage class used in our test environment.
> Replace it with a storage class that supports CSI snapshots on your cluster
> (e.g. `managed-csi` on AKS, `standard-rwo` on GKE, your custom class on bare-metal, etc.).
> The class must have a matching `VolumeSnapshotClass` registered with Kasten.
> Do **not** use legacy in-tree classes (e.g. `gp2` on AWS) — they do not support CSI snapshots.

```bash
kubectl apply -f minio-keeper.yaml
kubectl -n cnpg-barman-cloud rollout status deploy/pg-cluster-minio --timeout=3m
```

### 3. Create the MinIO bucket

```bash
kubectl run mc-init -n cnpg-barman-cloud --image=minio/mc:latest --restart=Never --rm -it --command -- \
  /bin/sh -c "
    mc alias set local http://pg-cluster-minio:9000 minioadmin minioadmin123
    mc mb -p local/cnpg-backup
    mc ls local
  "
```

> Only **one** bucket is needed. Unlike the in-tree blueprint, the recovery cluster reuses the same
> bucket under its own `serverName` prefix (see [PITR Recovery](#pitr-recovery-procedure)).

### 4. Deploy the ObjectStore CR

The `ObjectStore` CR ([objectstore.yaml](objectstore.yaml)) tells the plugin where and how to write
the archive. It references the MinIO credentials secret created by `minio-keeper.yaml`.

```bash
kubectl apply -f objectstore.yaml
```

### 5. Deploy the CNPG cluster

Review [cnpg-cluster.yaml](cnpg-cluster.yaml) and replace `storageClass: ebs-sc` with your cluster's
CSI storage class (same requirement as above). The cluster references the ObjectStore through
`spec.plugins`.

```bash
kubectl apply -f cnpg-cluster.yaml
kubectl wait clusters.postgresql.cnpg.io pg-cluster -n cnpg-barman-cloud --for=condition=Ready --timeout=5m
```

Confirm WAL archiving is healthy (the `ContinuousArchiving` condition must be `True`):

```bash
kubectl -n cnpg-barman-cloud get clusters.postgresql.cnpg.io pg-cluster \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}'
```

> Use the **fully-qualified** `clusters.postgresql.cnpg.io` form. The short name `cluster` may
> resolve to an unrelated Kasten CRD (`clusters.dist.kio.kasten.io`) on clusters that have Kasten
> installed.

### 6. Create test data

```bash
PRIMARY=$(kubectl get pods -n cnpg-barman-cloud \
  -l "cnpg.io/cluster=pg-cluster,cnpg.io/instanceRole=primary" \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n cnpg-barman-cloud "$PRIMARY" -c postgres -- psql -U postgres -c "CREATE DATABASE kasten_test;"

kubectl exec -n cnpg-barman-cloud "$PRIMARY" -c postgres -- psql -U postgres -d kasten_test -c "
  CREATE TABLE employees (
    id SERIAL PRIMARY KEY, name VARCHAR(100), department VARCHAR(50),
    salary INTEGER, created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW());
  INSERT INTO employees (name, department, salary) VALUES
    ('Alice Martin','Engineering',95000),('Bob Chen','Engineering',88000),
    ('Carol Smith','Marketing',72000),('David Johnson','Sales',68000),
    ('Eve Williams','Engineering',102000);
  SELECT * FROM employees ORDER BY id;
"
```

### 7. Deploy the blueprint and BlueprintBinding

```bash
kubectl apply -f ../cnpg/blueprintbinding.yaml   # ensure barman-cloud-plugin is in the NotIn list
kubectl apply -f blueprint.yaml -n kasten-io
kubectl apply -f blueprintbinding.yaml -n kasten-io
```

## Kasten policy configuration

Create a backup policy on the `cnpg-barman-cloud` namespace with a **location profile** (Kanister
actions need it for `KubeTask`).

**Critical: exclude CNPG data PVCs from the snapshot.** Only the MinIO PVC must be snapshotted:

```yaml
backupParameters:
  filters:
    excludeResources:
      - matchExpressions:
          - key: cnpg.io/pvcRole
            operator: In
            values:
              - PG_DATA
```

This filter serves two purposes:

- **Correctness at restore time** — if the CNPG data PVCs are restored alongside the MinIO PVC, the
  operator would find PVCs with stale PostgreSQL data that predate the WAL archive, breaking recovery.
- **Avoiding redundant storage** — the CNPG data PVCs are already fully represented in the MinIO
  barman archive. The MinIO PVC alone is sufficient for a complete restore.

## Understanding PITR range and WAL retention

### What a Kasten restore point contains

A Kasten restore point is a self-contained, immutable snapshot of the MinIO PVC taken at backup
time. It contains the **entire barman archive** present on MinIO at that moment:

- **All base backups** not yet pruned by the `ObjectStore.spec.retentionPolicy`.
- **All WAL files** archived since the oldest retained base backup.

WAL archiving is continuous: the plugin sidecar uploads a WAL segment to MinIO every time PostgreSQL
seals one, independently of whether a base backup is running.

### Concrete example

Suppose Kasten runs daily backups and `retentionPolicy: "7d"`. On **January 3rd at 10:00** the
`backupPrehook` creates a base backup and Kasten snapshots MinIO. That snapshot contains the
**January 2nd 10:00 base backup** (and earlier, within the 7-day window) plus every WAL between
January 2nd 10:00 and January 3rd 10:00. It is therefore sufficient to recover to any moment between
those two times.

### Sizing `retentionPolicy` relative to the Kasten backup interval

`ObjectStore.spec.retentionPolicy` controls which base backups (and their WALs) are kept on the
**live MinIO PVC**. If it is shorter than the Kasten backup interval, the previous base backup may be
pruned before the next Kasten snapshot captures it, drastically reducing the PITR window of that
restore point.

**Rule of thumb: set `retentionPolicy` to at least 2× the Kasten backup interval.**

| Kasten backup frequency | Minimum recommended `retentionPolicy` |
|---|---|
| Daily | `3d` (default `7d` here gives comfortable headroom) |
| Every 12 hours | `2d` |
| Weekly | `14d` |

## Verifying a backup

After a RunAction completes:

```bash
# Confirm the Backup CR was created (method=plugin) and completed
kubectl get backups.postgresql.cnpg.io -n cnpg-barman-cloud

# Verify base backup and WALs are in MinIO
kubectl run mc-chk -n cnpg-barman-cloud --image=minio/mc:latest --restart=Never --rm -it --command -- \
  /bin/sh -c "
    mc alias set local http://pg-cluster-minio:9000 minioadmin minioadmin123 2>/dev/null
    echo base: ; mc ls --recursive local/cnpg-backup/pg-cluster/base/
    echo wals: ; mc ls --recursive local/cnpg-backup/pg-cluster/wals/ | tail -5
  "
```

Expected: a `base/<timestamp>/` directory with `backup.info` + `data.tar.gz`, and a `wals/`
directory whose last segment is the post-switch WAL.

## PITR Recovery Procedure

> **No restore hooks** — recovery is manual. The procedure below replaces what
> `restorePrehook`/`restorePosthook` would otherwise automate. Validated end-to-end in this repo.

### Step 1 — (optional) record the PITR target time

```bash
kubectl run mc-ls -n cnpg-barman-cloud --image=minio/mc:latest --restart=Never --rm -it --command -- \
  /bin/sh -c "mc alias set local http://pg-cluster-minio:9000 minioadmin minioadmin123 2>/dev/null; mc ls local/cnpg-backup/pg-cluster/base/"
```

### Step 2 — delete the CNPG cluster

CNPG must not be running when Kasten restores the MinIO PVC. Deleting the Cluster CR also removes all
CNPG data PVCs (expected — they are not snapshotted anyway).

```bash
kubectl delete clusters.postgresql.cnpg.io pg-cluster -n cnpg-barman-cloud
kubectl wait pods -n cnpg-barman-cloud -l cnpg.io/cluster=pg-cluster --for=delete --timeout=3m
```

### Step 3 — trigger the Kasten restore

No `profile` is needed — Kasten extracts the location profile from the `RestorePointContent`. Two
`excludeResources` entries avoid manual cleanup:

- **Label filter** `cnpg.io/cluster: pg-cluster` — skips CNPG-managed services/endpoints.
- **Type + name filter** `postgresql.cnpg.io/clusters: pg-cluster` — skips the original Cluster CR
  (replaced by `pg-cluster-restored` next).

```bash
RESTORE_POINT=$(kubectl get restorepoint -n cnpg-barman-cloud -o jsonpath='{.items[-1].metadata.name}')

kubectl create -f - <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-
  namespace: cnpg-barman-cloud
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: ${RESTORE_POINT}
    namespace: cnpg-barman-cloud
  targetNamespace: cnpg-barman-cloud
  filters:
    excludeResources:
      - matchLabels:
          cnpg.io/cluster: pg-cluster
      - group: postgresql.cnpg.io
        resource: clusters
        name: pg-cluster
EOF
```

Wait for the RestoreAction to reach `Complete` (100%). Kasten restores the MinIO PVC (and Deployment,
Secrets, ObjectStore, etc.) — but not the CNPG-managed services or the original Cluster CR.

### Step 4 — create a CNPG recovery cluster

Create `pg-cluster-restored`, bootstrapping from the barman archive in MinIO. The
`externalClusters[].plugin.parameters.serverName` **must exactly match the original cluster name**
(`pg-cluster`), because the plugin uses it as the barman server path (`s3://cnpg-backup/pg-cluster/`).

Unlike the in-tree blueprint, **no second bucket is required**: the recovered cluster archives its
own new WALs under its own `serverName` (`pg-cluster-restored`), a distinct prefix in the same
bucket.

```bash
kubectl apply -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-cluster-restored
  namespace: cnpg-barman-cloud
  labels:
    cnpg-backup-pattern: barman-cloud-plugin
spec:
  instances: 2
  storage:
    size: 1Gi
    storageClass: ebs-sc        # replace with your CSI storage class
  # Continue archiving the recovered cluster's own WALs (own serverName prefix).
  plugins:
    - name: barman-cloud.cloudnative-pg.io
      isWALArchiver: true
      parameters:
        barmanObjectName: pg-cluster-store
  bootstrap:
    recovery:
      source: pg-cluster
      # Uncomment for true PITR — must be within the archive range:
      # recoveryTarget:
      #   targetTime: "2026-06-15 07:48:00"
  externalClusters:
    - name: pg-cluster
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: pg-cluster-store
          serverName: pg-cluster      # must match original cluster name (S3 path)
EOF
```

### Step 5 — wait and verify

```bash
kubectl wait clusters.postgresql.cnpg.io pg-cluster-restored -n cnpg-barman-cloud \
  --for=condition=Ready --timeout=10m

PRIMARY=$(kubectl get pods -n cnpg-barman-cloud \
  -l "cnpg.io/cluster=pg-cluster-restored,cnpg.io/instanceRole=primary" \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n cnpg-barman-cloud "$PRIMARY" -c postgres -- \
  psql -U postgres -d kasten_test -c "SELECT * FROM employees ORDER BY id;"
```

### PITR notes

- `recoveryTarget.targetTime` must be an ISO-8601 timestamp (`"YYYY-MM-DD HH:MM:SS"`).
- It must be **after** the base backup start time and **before** the last committed transaction in the
  WAL archive, or PostgreSQL exits with `FATAL: recovery ended before configured recovery target was
  reached`.
- To recover to the latest available point, **omit** `recoveryTarget` entirely.

## Tearing down

```bash
kubectl delete clusters.postgresql.cnpg.io pg-cluster pg-cluster-restored -n cnpg-barman-cloud 2>/dev/null || true
kubectl delete namespace cnpg-barman-cloud
kubectl delete blueprint cnpg-barman-cloud-blueprint -n kasten-io
kubectl delete blueprintbinding cnpg-barman-cloud-blueprint-binding -n kasten-io
kubectl delete policy cnpg-barman-cloud-backup -n kasten-io 2>/dev/null || true

# Clean up restore point contents created by Kasten
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=cnpg-barman-cloud

# (Optional) remove the plugin itself — only if no other cluster uses it
# kubectl delete -f https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/v0.13.0/manifest.yaml
```
