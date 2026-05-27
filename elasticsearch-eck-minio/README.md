# Elasticsearch (ECK) + MinIO Keeper — Kasten Blueprint

Pattern 5 (local MinIO keeper) for an ECK-managed Elasticsearch cluster.
Elasticsearch's native S3 snapshot repository writes to a local MinIO instance;
Kasten snapshots the MinIO PVC, capturing the full ES snapshot archive in each
restore point.

This blueprint is the **Pattern 5 alternative** to the existing
[`elasticsearch-eck/`](../elasticsearch-eck/) blueprint (Pattern 3 — permanent
workload PVC with shared filesystem repository). Use this one when:

- You don't want to mount an RWX repository PVC across all ES nodes.
- You want **per-restore-point ES snapshot lifecycle** (snapshots survive
  across backups, are deleted when the corresponding Kasten restore point
  is retired).

## Versions

| Component | Version |
|---|---|
| Kubernetes | `1.32` (EKS) |
| Kasten | `8.5.7` |
| ECK operator | `2.16.1` |
| Elasticsearch | `8.17.0` |
| MinIO | `RELEASE.2025-04-22T22-12-26Z` |

## Pattern

**Pattern 5 — Database dump or snapshot via a local MinIO keeper.**

```
┌──────────────────────────────────────────────────────────────────┐
│  Namespace: elasticsearch-eck-minio                              │
│                                                                  │
│  Elasticsearch (3 nodes)         MinIO keeper Deployment         │
│  ┌──────────────────────┐        ┌──────────────────────┐        │
│  │ es-cluster-es-...    │ ──S3── │ es-cluster-minio     │        │
│  │ (data PVCs)          │ snap   │  ├─ minio container  │        │
│  └──────────────────────┘        │  └─ curl sidecar     │        │
│                                  │     (KubeExec target)│        │
│                                  └─────────┬────────────┘        │
│                                            │                     │
│                                  ┌─────────▼────────────┐        │
│                                  │ es-cluster-minio PVC │ ◄──┐   │
│                                  │ /data/es-snapshots/  │    │   │
│                                  └──────────────────────┘    │   │
└──────────────────────────────────────────────────────────────┼───┘
                                                               │
                                          Kasten CSI snapshot ─┘
```

- A MinIO `Deployment` with a permanent PVC stores the Elasticsearch S3
  snapshot archive. Kasten snapshots **only this PVC** (the ES data PVCs are
  excluded — they are recreated from the ES snapshot on restore).
- The keeper pod has a `curl` sidecar container so that the blueprint can
  `KubeExec` into the keeper to talk to the Elasticsearch HTTP API. Using
  `KubeExec` into the keeper rather than a `KubeTask` in `kasten-io` lets the
  blueprint use `.Object.metadata.labels` / `.Object.metadata.namespace`
  directly — the keeper Deployment **is** the BlueprintBinding context.
- Workload identity (ES cluster name + kind) is carried as labels on the
  MinIO Deployment so that one generic blueprint can serve many keepers.

### Blueprint actions

| Action | What it does |
|---|---|
| `backupPrehook` | `KubeExec` into the keeper's `curl` sidecar → `PUT /_snapshot/kasten-repo/kasten-<timestamp>` on the Elasticsearch HTTP API to create a new native snapshot, polls until `state=SUCCESS`, then emits the snapshot name as a `kasten-snapshot` artifact. |
| `backupPosthook` | No-op (placeholder for symmetry). |
| `restorePrehook` | Defined for forward compatibility (Kasten ≤ 8.5.x does not yet trigger it). When triggered: close all open indices to allow restore. |
| `restorePosthook` | Waits for the ES cluster to be healthy after Kasten restores the MinIO PVC, re-registers the snapshot repository (idempotent), then `POST /_snapshot/kasten-repo/<snapshot>/_restore` with `indices: "*"` and `include_global_state: false`. Polls until the restore completes. |
| `delete` | Called by Kasten when a restore point is retired. Receives the `kasten-snapshot` artifact and issues `DELETE /_snapshot/kasten-repo/<snapshot>`. Idempotent — treats `snapshot_missing_exception` as success. |

## Snapshot lifecycle (no per-backup cleanup)

Unlike [`elasticsearch-eck/`](../elasticsearch-eck/), this blueprint **never
deletes Elasticsearch snapshots during a backup run**. Each backup creates a
new ES snapshot with a unique name; the snapshot's identity is stored as a
Kanister artifact alongside the Kasten restore point. When Kasten retires the
restore point, the `delete` action removes the corresponding ES snapshot from
the live MinIO bucket.

The total size of the live MinIO bucket grows in proportion to the number of
live Kasten restore points (not to the absolute backup count), since each
retired restore point causes its ES snapshot to be deleted.

## Prerequisites

- ECK operator installed in `elastic-system`.
- A CSI storage class that supports volume snapshots. Examples used here:
  - `ebs-sc` — AWS EBS CSI on EKS.

  > **Storage class**: `ebs-sc` is the AWS EBS CSI storage class used in our
  > test environment. Replace it with a storage class that supports CSI
  > snapshots on your cluster (e.g. `managed-csi` on AKS, `standard-rwo` on
  > GKE, your custom class on bare-metal). The class must have a matching
  > `VolumeSnapshotClass` registered with Kasten. Do **not** use legacy
  > in-tree classes (e.g. `gp2`) — they do not support CSI snapshots.

## Deployment

### 1. Create the namespace and the MinIO keeper

```bash
kubectl create ns elasticsearch-eck-minio
kubectl apply -f minio-keeper.yaml
kubectl wait --for=condition=Available --timeout=180s \
  deployment/es-cluster-minio -n elasticsearch-eck-minio
```

### 2. Create the MinIO bucket used by Elasticsearch

```bash
kubectl exec -n elasticsearch-eck-minio deploy/es-cluster-minio -c minio -- \
  mc alias set local http://localhost:9000 minioadmin minioadminpassword
kubectl exec -n elasticsearch-eck-minio deploy/es-cluster-minio -c minio -- \
  mc mb --ignore-existing local/es-snapshots
```

### 3. Create the Elasticsearch cluster

```bash
kubectl apply -f elasticsearch.yaml
kubectl wait --for=jsonpath='{.status.health}'=green --timeout=600s \
  elasticsearch/es-cluster -n elasticsearch-eck-minio
```

### 4. Register the S3 snapshot repository (one-time setup)

The repository is registered once at deployment time and **not** managed by the
blueprint. Re-running this command is safe (PUT is idempotent).

```bash
ELASTIC_PASS=$(kubectl get secret es-cluster-es-elastic-user \
  -n elasticsearch-eck-minio -o jsonpath='{.data.elastic}' | base64 -d)

kubectl exec -n elasticsearch-eck-minio sts/es-cluster-es-default -- \
  curl -sk -u "elastic:${ELASTIC_PASS}" \
       -X PUT "https://localhost:9200/_snapshot/kasten-repo" \
       -H 'Content-Type: application/json' \
       -d '{
             "type": "s3",
             "settings": {
               "bucket": "es-snapshots",
               "endpoint": "es-cluster-minio.elasticsearch-eck-minio.svc:9000",
               "protocol": "http",
               "path_style_access": true
             }
           }'
echo
```

Expected response: `{"acknowledged":true}`.

### 5. Create test data (small dataset for fast dev cycle)

```bash
kubectl exec -n elasticsearch-eck-minio sts/es-cluster-es-default -- \
  curl -sk -u "elastic:${ELASTIC_PASS}" \
       -X PUT "https://localhost:9200/test-index" \
       -H 'Content-Type: application/json' \
       -d '{"settings":{"number_of_shards":1,"number_of_replicas":1}}'
echo

for i in 1 2 3 4 5; do
  kubectl exec -n elasticsearch-eck-minio sts/es-cluster-es-default -- \
    curl -sk -u "elastic:${ELASTIC_PASS}" \
         -X POST "https://localhost:9200/test-index/_doc" \
         -H 'Content-Type: application/json' \
         -d "{\"id\":${i},\"msg\":\"hello number ${i}\"}"
  echo
done

kubectl exec -n elasticsearch-eck-minio sts/es-cluster-es-default -- \
  curl -sk -u "elastic:${ELASTIC_PASS}" \
       -X POST "https://localhost:9200/test-index/_refresh"
echo
```

Verify the documents are searchable:

```bash
kubectl exec -n elasticsearch-eck-minio sts/es-cluster-es-default -- \
  curl -sk -u "elastic:${ELASTIC_PASS}" \
       "https://localhost:9200/test-index/_search?pretty"
```

## Teardown

```bash
# Delete any Kasten restore points that point at this app first, then:
kubectl delete -f elasticsearch.yaml --ignore-not-found
kubectl delete -f minio-keeper.yaml --ignore-not-found
kubectl delete ns elasticsearch-eck-minio --ignore-not-found

# Clean up retained Kasten snapshot contents (replace selector if needed)
kubectl delete restorepointcontent \
  -l k10.kasten.io/appNamespace=elasticsearch-eck-minio --ignore-not-found
```

## Status

Step 2 (deploy + test data) — in progress.
Step 3 (manual workflow validation), Step 4 (blueprint), Step 5 (end-to-end)
— pending.
