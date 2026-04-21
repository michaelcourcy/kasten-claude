# CNPG Barman Blueprint (Pattern 4C — MinIO Keeper)

Kasten blueprint for CloudNativePG clusters that use **barman-cloud WAL archiving to a local MinIO instance**. Kasten snapshots the MinIO PVC (which contains the full barman archive), enabling Point-In-Time Recovery (PITR) from any Kasten restore point.

> ⚠️ **Deprecation notice**: barman-cloud integration in CNPG is deprecated and will be **fully removed in CNPG 1.30.0**. Consider migrating to the [cnpg/](../cnpg/) Pattern 1 blueprint (fence and quiesce a replica) for new deployments. This blueprint targets CNPG ≤ 1.29.x.

## Versions

| Component | Version |
|---|---|
| Kubernetes | `1.32` (EKS) |
| Kasten | `8.5.4` |
| CNPG operator / Helm chart | `1.29.0` / `cloudnative-pg 0.28.0` |
| PostgreSQL | `18.3` |
| MinIO | `RELEASE.2025-04-22T22-12-26Z` |

## Pattern

**Pattern 4C — Database backup via a local MinIO keeper (vendor operator data mover).**

A dedicated MinIO `Deployment` (the *keeper*) with a permanent PVC stores the CNPG barman archive. The `backupPrehook` blueprint action:
1. Creates a CNPG `Backup` CR to trigger a barman base backup.
2. Polls until the base backup completes.
3. Forces a WAL segment switch (`pg_switch_wal()`) so the post-backup checkpoint WAL is flushed to MinIO before Kasten snapshots the PVC.

Kasten then snapshots the MinIO PVC (and only that PVC — CNPG data PVCs are excluded via policy filter). The MinIO snapshot contains a self-consistent barman archive: a base backup plus all WALs up to the switch.

**Why this pattern:**  
Barman owns data movement and immutability. Kasten's role is solely to snapshot the MinIO PVC at a moment when the archive is consistent. This avoids duplicating CNPG's existing WAL archiving infrastructure.

## Architecture

```
┌──────────────────────────────────┐
│  Namespace: cnpg-barman          │
│                                  │
│  ┌──────────┐   WAL stream       │
│  │  CNPG    │──────────────────► │
│  │  Cluster │   barman-cloud     │
│  │          │   base backups     │
│  └──────────┘        │           │
│       │              ▼           │
│  PVCs (excluded   ┌────────┐     │
│  from snapshot)   │ MinIO  │     │
│                   │ Keeper │     │
│                   │  PVC   │◄────┼── Kasten snapshots this
│                   └────────┘     │
└──────────────────────────────────┘
```

The BlueprintBinding targets Deployments labelled `cnpg-barman-minio: "true"`. The MinIO Deployment name must follow the convention `<cluster-name>-minio` — the blueprint derives the CNPG cluster name by stripping the `-minio` suffix.

The CNPG Cluster must carry the label `cnpg-backup-pattern: barman-minio`, which prevents the [cnpg-blueprint-binding](../cnpg/blueprintbinding.yaml) (Pattern 1) from also binding to it.

## Blueprint actions

| Action | Hook | What it does |
|---|---|---|
| `backupPrehook` | Before PVC snapshot | Creates CNPG `Backup` CR, waits for completion, forces WAL switch; outputs `backupName` and `namespace` as restore-point artifacts |
| `delete` | On restore-point deletion | Deletes the CNPG `Backup` CR from the application namespace so the CNPG operator removes the corresponding barman data from MinIO; silently no-ops if the namespace or Backup CR no longer exists |

No restore hooks are implemented — PITR recovery is a **manual procedure** using a CNPG recovery cluster manifest (see [PITR Recovery Procedure](#pitr-recovery-procedure) below).

> **Why a `delete` action is necessary**: deleting a Kasten restore point only removes the MinIO PVC snapshot. The CNPG `Backup` CR and the barman base-backup data it references inside MinIO are not cleaned up automatically. Without the `delete` action, the MinIO bucket grows unboundedly. The `delete` action triggers the CNPG operator's normal garbage-collection path by deleting the `Backup` CR.

## Prerequisites

- CNPG operator installed (`cloudnative-pg` Helm chart).
- `michaelcourcy/kasten-tools:8.5.2` image available (adds `kubectl` to `gcr.io/kasten-images/kanister-tools:8.5.2`).
- Blueprint and BlueprintBinding deployed in `kasten-io` namespace.
- Kasten backup policy configured with the PVC filter described below.
- The Pattern 1 `cnpg-blueprint-binding` must have the `NotIn: barman-minio` exclusion condition (already present in `cnpg/blueprintbinding.yaml`).

## Deploying the workload

### 1. Create the namespace

```bash
kubectl create namespace cnpg-barman
```

### 2. Deploy MinIO keeper

Review [minio-keeper.yaml](minio-keeper.yaml) before applying — in particular the `storageClassName` on the PVC, which must be replaced with a CSI storage class that supports snapshots on your cluster.

> **Storage class**: `ebs-sc` is the AWS EBS CSI storage class used in our test environment.
> Replace it with a storage class that supports CSI snapshots on your cluster
> (e.g. `managed-csi` on AKS, `standard-rwo` on GKE, your custom class on bare-metal, etc.).
> The class must have a matching `VolumeSnapshotClass` registered with Kasten.
> Do **not** use legacy in-tree classes (e.g. `gp2` on AWS) — they do not support CSI snapshots.

```bash
kubectl apply -f minio-keeper.yaml
kubectl wait deployment pg-cluster-minio -n cnpg-barman --for=condition=Available --timeout=3m
```

### 3. Create the MinIO bucket

```bash
kubectl run mc-init -n cnpg-barman --image=minio/mc:latest --restart=Never --rm -it -- \
  /bin/sh -c "
    mc alias set local http://pg-cluster-minio:9000 minioadmin minioadmin123
    mc mb local/cnpg-backup
    mc mb local/cnpg-backup-recovered
    mc ls local
  "
```

### 4. Deploy the CNPG cluster

Review [cnpg-cluster.yaml](cnpg-cluster.yaml) and replace `storageClassName: ebs-sc` with your cluster's CSI storage class (same requirement as above).

```bash
kubectl apply -f cnpg-cluster.yaml
kubectl wait cluster.postgresql.cnpg.io pg-cluster -n cnpg-barman --for=condition=Ready --timeout=5m
```

### 5. Create test data

```bash
PRIMARY=$(kubectl get pods -n cnpg-barman \
  -l "cnpg.io/cluster=pg-cluster,cnpg.io/instanceRole=primary" \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n cnpg-barman "$PRIMARY" -- psql -U postgres -c "
  CREATE DATABASE kasten_test;
"

kubectl exec -n cnpg-barman "$PRIMARY" -- psql -U postgres -d kasten_test -c "
  CREATE TABLE employees (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    department VARCHAR(50),
    salary INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
  );
  INSERT INTO employees (name, department, salary) VALUES
    ('Alice Martin',  'Engineering', 95000),
    ('Bob Chen',      'Engineering', 88000),
    ('Carol Smith',   'Marketing',   72000),
    ('David Johnson', 'Sales',       68000),
    ('Eve Williams',  'Engineering', 102000);
  SELECT * FROM employees ORDER BY id;
"
```

### 6. Deploy the blueprint and BlueprintBinding

```bash
kubectl apply -f ../cnpg/blueprintbinding.yaml   # ensure NotIn: barman-minio exclusion is present
kubectl apply -f blueprint.yaml -n kasten-io
kubectl apply -f blueprintbinding.yaml -n kasten-io
```

## Kasten policy configuration

Create a backup policy on the `cnpg-barman` namespace with a **location profile** (Kanister actions need it for `KubeTask`).

**Critical: exclude CNPG data PVCs from the snapshot.** Only the MinIO PVC must be snapshotted. Configure the policy's volume filter to exclude PVCs with the CNPG data-role label:

```yaml
backupParameters:
  filters:
    excludeResources:
      - matchLabels:
          cnpg.io/pvcRole: PG_DATA
```

Without this filter, Kasten snapshots both the CNPG data PVCs and the MinIO PVC. The restore would then overwrite the CNPG data PVCs with stale content before PITR has a chance to run — producing incorrect results.

## Verifying a backup

After a RunAction completes, check:

```bash
# Confirm the Backup CR was created and completed
kubectl get backups.postgresql.cnpg.io -n cnpg-barman -l cnpg.io/cluster=pg-cluster

# Verify base backup and WALs are in MinIO
MINIO_POD=$(kubectl get pods -n cnpg-barman -l app=pg-cluster-minio -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n cnpg-barman "$MINIO_POD" -- sh -c "
  mc alias set local http://localhost:9000 minioadmin minioadmin123 2>/dev/null
  mc ls --recursive local/cnpg-backup/ | tail -20
"
```

Expected output: a `base/` directory with a timestamped backup and a `wals/` directory with compressed WAL files, the last of which should be roughly 1–16 MiB (post-switch WAL).

## PITR Recovery Procedure

> **No restore hooks** — recovery is manual. The procedure below replaces what `restorePrehook`/`restorePosthook` would otherwise automate.

These steps recover the `cnpg-barman` namespace to the state captured in a Kasten restore point. You can optionally specify a `targetTime` to stop WAL replay before the end of the archive (true PITR).

### Step 1 — Record the target time (optional, for PITR)

Before triggering restore, record the timestamp you want to recover to. The timestamp must fall within the archive — i.e., after the base backup and before the last WAL in the archive.

```bash
# List available base backups to understand the archive range
MINIO_POD=$(kubectl get pods -n cnpg-barman -l app=pg-cluster-minio -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n cnpg-barman "$MINIO_POD" -- sh -c "
  mc alias set local http://localhost:9000 minioadmin minioadmin123 2>/dev/null
  mc ls local/cnpg-backup/pg-cluster/base/
"
```

### Step 2 — Delete the CNPG cluster

CNPG must not be running when Kasten restores the MinIO PVC. Deleting the Cluster CR also removes all CNPG-managed PVCs (this is expected — the data PVCs are not snapshotted anyway).

```bash
kubectl delete cluster.postgresql.cnpg.io pg-cluster -n cnpg-barman
# Wait for all pg-cluster-N pods to disappear
kubectl wait pods -n cnpg-barman -l cnpg.io/cluster=pg-cluster --for=delete --timeout=3m
```

### Step 3 — Trigger Kasten restore

List available restore points and trigger a restore:

```bash
kubectl get restorepoint -n cnpg-barman

kubectl create -f - --validate=false <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-
  namespace: cnpg-barman
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: <RESTORE_POINT_NAME>
    namespace: cnpg-barman
  targetNamespace: cnpg-barman
  profile:
    name: <LOCATION_PROFILE_NAME>   # type=Location, e.g. "us-east-1"
    namespace: kasten-io
EOF
```

Wait for the RestoreAction to complete (100%). Kasten restores the MinIO PVC and all other resources (Deployments, Services, Secrets, etc.) from the restore point.

### Step 4 — Clean up stale CNPG services

Kasten restores the CNPG Services from the restore point. CNPG refuses to own services it did not create. Delete them so CNPG can recreate them on startup:

```bash
kubectl delete service -n cnpg-barman -l cnpg.io/cluster=pg-cluster
```

Also delete the CNPG Cluster CR if Kasten restored it (it will be replaced with the recovery cluster manifest):

```bash
kubectl delete cluster.postgresql.cnpg.io pg-cluster -n cnpg-barman 2>/dev/null || true
kubectl wait pods -n cnpg-barman -l cnpg.io/cluster=pg-cluster --for=delete --timeout=3m 2>/dev/null || true
```

### Step 5 — Create a CNPG recovery cluster

Create a new Cluster CR that bootstraps from the barman archive in MinIO. The `externalClusters[].name` **must exactly match the original cluster name** (`pg-cluster`) because CNPG uses it as the barman server path in S3 (`s3://cnpg-backup/pg-cluster/`).

The `backup.barmanObjectStore.destinationPath` must point to a **different bucket** (`cnpg-backup-recovered`) — CNPG refuses to start if the write destination already contains an archive.

```bash
kubectl apply -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-cluster
  namespace: cnpg-barman
  labels:
    cnpg-backup-pattern: barman-minio
spec:
  instances: 2
  storage:
    size: 1Gi
    storageClass: ebs-sc       # replace with your CSI storage class
  bootstrap:
    recovery:
      source: pg-cluster
      # Uncomment and adjust for PITR — must be within the archive range:
      # recoveryTarget:
      #   targetTime: "2026-04-21 06:45:00"
  externalClusters:
    - name: pg-cluster          # must match original cluster name (S3 path)
      barmanObjectStore:
        endpointURL: http://pg-cluster-minio.cnpg-barman.svc:9000
        destinationPath: s3://cnpg-backup/
        s3Credentials:
          accessKeyId:
            name: pg-cluster-minio-creds
            key: rootUser
          secretAccessKey:
            name: pg-cluster-minio-creds
            key: rootPassword
        wal:
          compression: gzip
  backup:
    barmanObjectStore:
      endpointURL: http://pg-cluster-minio.cnpg-barman.svc:9000
      destinationPath: s3://cnpg-backup-recovered/   # different bucket — required
      s3Credentials:
        accessKeyId:
          name: pg-cluster-minio-creds
          key: rootUser
        secretAccessKey:
          name: pg-cluster-minio-creds
          key: rootPassword
      wal:
        compression: gzip
        maxParallel: 2
    retentionPolicy: "7d"
EOF
```

### Step 6 — Wait and verify

```bash
kubectl wait cluster.postgresql.cnpg.io pg-cluster -n cnpg-barman \
  --for=condition=Ready --timeout=10m

PRIMARY=$(kubectl get pods -n cnpg-barman \
  -l "cnpg.io/cluster=pg-cluster,cnpg.io/instanceRole=primary" \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n cnpg-barman "$PRIMARY" -- \
  psql -U postgres -d kasten_test -c "SELECT * FROM employees ORDER BY id;"
```

### PITR notes

- `recoveryTarget.targetTime` must be an ISO-8601 timestamp in UTC (`"YYYY-MM-DD HH:MM:SS"`).
- The timestamp must be **after** the base backup start time and **before** the last committed transaction in the WAL archive. If you specify a time beyond the archive, PostgreSQL will exit with `FATAL: recovery ended before configured recovery target was reached`.
- To recover to the very end of the archive (latest available point), **omit** `recoveryTarget` entirely.
- After recovery completes and the cluster is `Ready`, the recovered cluster will start archiving new WALs to `cnpg-backup-recovered`. If you want to resume archiving to the original bucket, recreate the cluster with `destinationPath: s3://cnpg-backup/` after manually clearing the archive (not recommended — prefer a fresh backup policy run instead).

## Tearing down

```bash
kubectl delete cluster.postgresql.cnpg.io pg-cluster -n cnpg-barman 2>/dev/null || true
kubectl delete namespace cnpg-barman
kubectl delete blueprint cnpg-barman-blueprint -n kasten-io
kubectl delete blueprintbinding cnpg-barman-blueprint-binding -n kasten-io

# Clean up restore point contents created by Kasten
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=cnpg-barman
```
