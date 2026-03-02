# Kasten Blueprint — MariaDB Community Operator (Standalone)

## Scope

This blueprint targets **standalone MariaDB instances** (replicas: 1) managed by the
[mariadb-operator](https://github.com/mariadb-operator/mariadb-operator) community operator.
Galera clusters and replicated topologies are out of scope.

## Chosen pattern: 2 — Quiesce / Unquiesce

### Rationale

| Pattern | Feasible? | Notes |
|---------|-----------|-------|
| 1 — Fence and quiesce a replica | ❌ | Standalone has no replica |
| 2 — Quiesce | ✅ **Chosen** | See below |
| 3/4 — Dump to temp PVC | Feasible | Ruled out: no incrementality |

**Why pattern 2:**

MariaDB exposes a standard quiesce primitive for InnoDB workloads:

```sql
FLUSH TABLES WITH READ LOCK;
FLUSH LOGS;
```

This blocks new writes and flushes all dirty pages and binary logs to disk, producing a
crash-consistent snapshot that InnoDB recovers cleanly on startup. After Kasten completes
the PVC snapshot, `UNLOCK TABLES` is issued in `backupPosthook` to resume normal operation.

Advantages:
- **Incremental**: Kasten snapshots the PVC delta between backups — backup time stays constant
  as the database grows.
- **No extra PVC**: No dump pod or temporary storage needed.
- **Simple restore**: Kasten restores the PVC; MariaDB starts up cleanly from the snapshot.
  The `FLUSH TABLES WITH READ LOCK` lock is a session-level in-memory state — it does not
  persist on disk. InnoDB sees a clean checkpoint and starts normally without any unlock step.
  No `restorePosthook` is needed.

Limitation:
- Granular restore (single table or database) is not possible — the whole PVC is restored.
  If granular restore is required, switch to pattern 4 (logical dump on temp PVC).

## Operator ownership chain

```
MariaDB CR  (k8s.mariadb.com/v1alpha1)
  └── StatefulSet  (<name>)
        ├── Pod  (<name>-0)
        └── PVC  (storage-<name>-0)
```

Kasten discovers PVCs through the ownership chain from the MariaDB CR.
The blueprint is bound to the **MariaDB custom resource** via a BlueprintBinding.

## Blueprint actions

| Action | What it does |
|--------|-------------|
| `backupPrehook` | `FLUSH TABLES WITH READ LOCK; FLUSH LOGS;` — quiesces MariaDB |
| `backupPosthook` | `UNLOCK TABLES;` — unquiesces after Kasten PVC snapshot is ready |

## Dependencies

- mariadb-operator installed in the cluster (Helm chart `mariadb-operator/mariadb-operator`)
- A Kubernetes Secret holding the MariaDB root password (created by the operator by default)
- The blueprint and BlueprintBinding deployed in the `kasten-io` namespace
- Custom image `michaelcourcy/kasten-tools:<kasten-version>` for `KubeTask` phases that need
  `kubectl` (the standard `gcr.io/kasten-images/kanister-tools` image does not include it).
  Dockerfile: [`images/kasten-tools/Dockerfile`](images/kasten-tools/Dockerfile)

## Custom images

| Image | Base | Added | Dockerfile |
|-------|------|-------|------------|
| `michaelcourcy/kasten-tools:8.5.2` | `gcr.io/kasten-images/kanister-tools:8.5.2` | `kubectl` v1.32.0 | `images/kasten-tools/Dockerfile` |

To rebuild for a different Kasten version:
```bash
docker build --build-arg KASTEN_VERSION=<version> -t michaelcourcy/kasten-tools:<version> images/kasten-tools/
docker push michaelcourcy/kasten-tools:<version>
```

## Files

| File | Description |
|------|-------------|
| `operator/` | Helm values and MariaDB CR for the test deployment |
| `blueprint.yaml` | Kasten Blueprint |
| `blueprintbinding.yaml` | BlueprintBinding targeting all MariaDB CRs |
| `images/kasten-tools/Dockerfile` | Dockerfile for `michaelcourcy/kasten-tools` |
