# Kasten Blueprint Development — Session Memory

## Project
Working directory: `/Users/michael.courcy/kasten.io/github/kasten-claude`
Cluster: AWS EKS, dedicated test environment. All namespaces expendable except `kasten-io` and `kube-system`.

## Storage Classes
| StorageClass | Provisioner | CSI snapshots |
|---|---|---|
| `gp2` (default) | `kubernetes.io/aws-ebs` (in-tree) | ❌ Not supported |
| `ebs-sc` | `ebs.csi.aws.com` (CSI) | ✅ Supported |

Always deploy test workloads with `storageClassName: ebs-sc`.
VolumeSnapshotClasses: `ebs-snapshot-class` (Delete), `k10-clone-ebs-snapshot-class` (Retain).

## Custom Image: michaelcourcy/kasten-tools:8.5.2
Built to add `kubectl` to `gcr.io/kasten-images/kanister-tools:8.5.2` (which lacks kubectl).
Source Dockerfile at `/tmp/mariadb-kanister-tools/Dockerfile`.
Use this image for KubeTask phases that need kubectl.
KubeTask with kubectl MUST run in `kasten-io` namespace for RBAC.

## MariaDB Community Operator Blueprint
Location: `mariadb-community-operator/`
Files: `blueprint.yaml`, `blueprintbinding.yaml`, `README.md`

### Current Status (as of session 2)
- **Backup**: ✅ Working — backupPrehook/backupPosthook execute correctly
- **Restore**: ❌ Bug confirmed — restorePrehook never executes

### Confirmed Kasten Engineering Statement
**`restorePrehook` is not yet implemented** — confirmed by Kasten engineering. It will be available in a near-future release. Blueprint action is never triggered during restore.

Evidence from executor logs (Kasten executor-svc):
- `restorePreHook` Kasten phase runs and completes in < 1ms (no-op)
- Zero Kanister ActionSets are created for the restore hook
- This happens with ALL binding approaches tested:
  - BlueprintBinding on MariaDB CR (`k8s.mariadb.com/mariadbs`)
  - `kanister.kasten.io/blueprint` annotation on StatefulSet
- Contrast: `backupPreHook` phase DOES trigger Kanister ActionSets correctly

### Root Cause of Restore Failure
MariaDB operator reconciles StatefulSet to replicas=1 continuously.
Kasten's `restoreAppPrepare` deletes the PVC, the operator immediately recreates it (new empty PVC),
Kasten times out waiting for the PVC deletion to propagate.

### Intended Fix (pending restorePrehook bug fix)
Blueprint `restorePrehook` should patch the MariaDB CR with `spec.suspend: true`.
The `spec.suspend` field is confirmed valid: `kubectl explain mariadb.spec.suspend`.

## Key Patterns Learned (general, applies to all blueprints)
- KubeTask must run in `kasten-io` namespace for cross-namespace kubectl operations
- `kanister.kasten.io/blueprint` annotation on StatefulSet is preserved by mariadb-operator
- Kasten executor phases: `backupPreHook` → triggers ActionSets; `restorePreHook` → does NOT (not yet implemented per engineering, coming soon)
- Restore phase order: createNamespace → lockKubernetesNamespace → restorePreHook → restorePrecheck
  → RestoreSpecsForNamespace → RestoreSpecsForResources → restoreVolumes → restoreAppPrepare
  → restoreApplication → restoreImages → RestoreSpecsForCRs → restoreSanitize → restoreKanister
  → restorePostHook → dropRestorePointHold → metrics
- **Template variables**: `.Namespace.Name` is ONLY populated when Kasten invokes a policy hook.
  `.Object.metadata.namespace` works for resource-bound blueprints (BlueprintBinding/annotation).
  Never use `.Namespace.Name` in a resource-bound blueprint — it resolves to empty string.
- **No `configMaps:` field in blueprint phases** — ConfigMaps use `objects:` with `kind: ConfigMap`. Access via `.Phases.<name>.ConfigMaps.<key>.Data.<field>`.
- **Objects block chicken-and-egg**: you cannot reference `.Phases.<name>.ConfigMaps.<x>.Data.*` inside the `objects` block of the same phase — use a static/hardcoded name for the second object.
- **kanister-tools:8.5.2 image** does NOT have `jq` or `python3`. Use `kubectl -o jsonpath` and bash built-ins only.
- **Policy hook ActionSet structure**: use `kubectl create` (not `apply`) with `generateName`.
- **`jsonpath '{.items[0].metadata.name}'` exits 1 on empty list** — kills script with `set -o errexit` silently. Fix: `|| true`. Use `'{.items[*].metadata.name}'` (exits 0) for loops.
- **Labels added in `backupPrehook` are NOT in the captured PVC spec** — PVC discovery runs before backupPrehook; captured spec = pre-hook state. Labels set in backupPrehook are NOT preserved on restored PVC. Use Kanister artifact (`kando output`) for reliable tracking across backup/restore.
- **`filters.excludeResources` by `name:` works in RestoreAction** — `name: "pvc-name"` excludes that specific PVC from being restored from snapshot. Confirmed via Kasten UI ("1/1 volumes" when 2 of 3 excluded).
- **Operator must be deleted before PVCs** — if operator CR is still running when PVCs are deleted, the operator immediately recreates them empty. Always delete the CR first, wait for pods to stop, then delete PVCs.

## RestoreAction — Correct Spec (ALWAYS USE THIS)
RestoreAction lives in the **app namespace**, not kasten-io. Must include a Location profile for Kanister blueprint hooks.
```yaml
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-
  namespace: <APP_NAMESPACE>          # app namespace, NOT kasten-io
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: <RESTORE_POINT_NAME>        # from: kubectl get restorepoint -n <APP_NAMESPACE>
    namespace: <APP_NAMESPACE>
  targetNamespace: <APP_NAMESPACE>
  profile:
    name: <LOCATION_PROFILE>          # type=Location (e.g. us-east-1), NOT Infra
    namespace: kasten-io
```
Profile type check: `kubectl get profile <name> -n kasten-io -o jsonpath='{.spec.type}'`

## CancelAction — Correct Spec
CancelAction for RestoreAction uses the **app namespace** in spec.subject:
```yaml
kind: CancelAction
namespace: <APP_NAMESPACE>   # or kasten-io for RunAction
spec.subject:
  kind: RestoreAction
  name: <name>
  namespace: <APP_NAMESPACE>
```

## PVC Info Blueprint
Location: `pvc-info/`; Status: ✅ Working
Pattern: Policy preHook that writes PVC/PV info to ConfigMap `pvc-info-snapshot`

## Elasticsearch ECK Blueprint
Location: `elasticsearch-eck/`; Status: Backup ✅ / Restore ❌ (restorePrehook bug)
Pattern: Pattern 3 sub-case A — database snapshot on permanent RWX PVC (`/mnt/snapshot-repo`)
- WaitV2 for CRDs: `apiVersion: v1` + `group: elasticsearch.k8s.elastic.co` (separate fields)
- ES 8.x snapshot DELETE API: handle `snapshot_missing_exception` explicitly (not query param)

## Couchbase Autonomous Operator Blueprint
Location: `couchbase-operator/`
Files: `blueprint.yaml`, `blueprintbinding.yaml`, `keeper.yaml`, `README.md`
Pattern: Pattern 4 (keeper Deployment, sub-case B)

### Status (2026-04-09)
- **Backup**: ✅ Working — backupPrehook runs cbbackupmgr + sync, backupPosthook verifies
- **Restore**: ✅ Working — restorePosthook resumes cluster + polls REST API + runs cbbackupmgr restore

### Architecture
- Keeper Deployment `cb-example-keeper` mounts `cb-example-keeper` PVC (10Gi, ebs-sc) at `/backup`
- Image: `couchbase/server:7.6.6` (has cbbackupmgr built in, no custom image needed)
- Keeper carries env vars: `USERNAME`/`PASSWORD` from `cb-example-auth` secret, `CLUSTER` as value
- **No ConfigMap needed** — blueprint uses env vars from keeper pod (KubeExec inherits them)
- Secret `cb-example-auth` in app namespace: admin credentials
- BlueprintBinding targets label `couchbase-keeper: "true"` (generic — covers all keepers)
- Archive path: `/backup/data/kasten-repo` (not `/backup` — EBS root has `lost+found`)
- Cluster name derived in KubeTask: `${KEEPER_NAME%-keeper}` strips `-keeper` from `.Deployment.Name`
- Naming convention: keeper Deployment and PVC = `<workload>-keeper` (e.g. `cb-example-keeper`)
- operator v2.9.0 creates `couchbase-operator-0000/0001/0002` pods NOT owned by StatefulSet — they are force-killed in the quiesce step

### Key Technical Findings
- `CouchbaseBackup` CR causes nil pointer panic in operator 2.9.0 — use direct cbbackupmgr
- restorePrehook (8.5.x): not triggered — manual workaround: scale down ALL deployments/statefulsets + force-kill all pods before restore
- stale lock: restored backup PVC always has stale `lock.lk` + `.tmp/` — must `rm -f lock.lk && rm -rf .tmp` before cbbackupmgr restore
- `--restore-partial-backups` flag required — archive may contain incomplete backups from failed runs
- **`--purge` flag required** on cbbackupmgr restore — any previous failed restore attempt leaves `.restore/` progress state in the backup PVC; without `--purge` the next restore fails with "last restore did not finish properly"
- **`sync` after cbbackupmgr backup is CRITICAL** — EBS snapshots are block-device snapshots that bypass the OS page cache. Without `sync`, cbbackupmgr's sqlite index files (196KB each, one per active vbucket) remain as 0-byte files on the block device. Restoring from such a snapshot fails with "invalid Rift index: unknown index version". Always call `sync` immediately after cbbackupmgr backup completes, before Kasten takes the PVC snapshot.
- **REST API readiness vs Available status**: CouchbaseCluster becomes `Available` before the REST API accepts connections. The cbRestore phase polls `http://<host>:8091/pools/default` before running cbbackupmgr to avoid "failed to bootstrap client" errors.
- Couchbase pods owned directly by CouchbaseCluster (no StatefulSet) — label: `couchbase_cluster=<name>`
- WaitV2 for CouchbaseCluster: `apiVersion: v2, group: couchbase.com`
- WaitV2 condition: `{{ range .status.conditions }}{{ if and (eq .type "Available") (eq .status "True") }}true{{ end }}{{ end }}`

## PSMDB (Percona MongoDB) — Quiesce Blueprint
Location: `psmdb-percona-operator/`
Files: `blueprint.yaml`, `blueprintbinding.yaml`, `README.md`
Pattern: Pattern 1 — Fence and quiesce a secondary replica
Target: Unsharded PSMDB replica sets only

### Status: ✅ Backup working / Restore manual (no restorePosthook)
- `backupPrehook`: fsyncLock on a secondary, emit quiesced PVC name as artifact
- `backupPosthook`: fsyncUnlock the labeled pod
- Restore: manual — delete CR + PVCs, trigger RestoreAction excluding non-quiesced PVCs

### Key findings
- Quiesced PVC name stored as Kanister artifact (label not preserved through restore)
- PVC labels after backupPrehook are NOT in captured spec (discovery runs before hook)
- Short name `psmdb-backup` works for kubectl get; full name `perconaservermongodbbackups`

## PSMDB pbm (Percona MongoDB) — MinIO Keeper Blueprint
Location: `psmdb-percona-operator-pbm/`
Files: `blueprint.yaml`, `blueprintbinding.yaml`, `minio-keeper.yaml`, `psmdb-values.yaml`, `README.md`
Pattern: Pattern 4C — MinIO keeper (vendor operator data mover via pbm)
Target: **Sharded** PSMDB clusters (1+ shards + config server)

### Status (2026-04-24): ✅ Backup + ✅ Restore — both fully automated
- `backupPrehook`: creates `PerconaServerMongoDBBackup` CR, polls until `status.state=ready`, emits 4 artifacts
- `restorePosthook`: waits for PSMDB cluster ready, creates `PerconaServerMongoDBRestore` CR, polls until ready
- `delete`: runs `pbm delete-backup --yes <pbmName>` in backup-agent container, then deletes backup CR

### Architecture
- MinIO keeper Deployment `<cluster>-minio` + PVC 10Gi + Service in app namespace
- MinIO credentials secret: `<cluster>-minio-creds` (keys: `rootUser`, `rootPassword`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
- pbm storage name in PSMDB CR: `minio-backup`; bucket: `pbm-backup`
- BlueprintBinding targets Deployments with label `psmdb-minio: "true"`
- Cluster name derived by stripping `-minio` from keeper Deployment name
- Policy filter: exclude PVCs with `matchLabels: app.kubernetes.io/name: percona-server-mongodb`

### Key Technical Findings
- pbm `backup-agent` container is present on every mongod pod (rs0 and cfg) — has `pbm` CLI pre-configured
- `pbm delete-backup --yes <pbmName>` cleanly removes archive data from MinIO
- Artifacts emitted: `backupCRName` (for restore CR), `pbmName` (for delete), `clusterName`, `namespace`
- Short name `psmdb-backup` / `psmdb-restore` works for kubectl commands
- Backup covers ALL shards and config server atomically (pbm handles cross-shard consistency)
- Policy type: logical backup; no incremental support via operator CR mechanism
- No conflict with `psmdb-percona-operator` blueprint — it binds `perconaservermongodbs` CRs; this binds Deployments

## CNPG (CloudNativePG) Blueprint
Location: `cnpg/`
Files: `blueprint.yaml`, `blueprintbinding.yaml`, `README.md`
Pattern: Pattern 1 — Fence and quiesce a replica
CNPG operator: `1.29.0`, Helm chart: `cloudnative-pg 0.28.0`, PostgreSQL: `18.3`, Kasten: `8.5.4`

### Status (2026-04-20)
- **Backup**: ✅ Working — pauseWalReplay/resumeWalReplay execute correctly via KubeTask
- **Restore**: ✅ Working — CNPG auto-promotes restored replica PVC to primary, restorePosthook WaitV2 works

### Architecture
- Blueprint binds to `clusters` in `postgresql.cnpg.io` group via BlueprintBinding
- `backupPrehook`: KubeTask in kasten-io, `kubectl exec` into replica pod → `pg_wal_replay_pause()`
- `backupPosthook`: KubeTask in kasten-io, `kubectl exec` into replica pod → `pg_wal_replay_resume()`
- `restorePrehook`: KubeTask deletes Cluster CR (CNPG stops all pods AND garbage-collects ALL PVCs via finalizer)
- `restorePosthook`: WaitV2 for cluster condition `Ready`

### Critical CNPG Behaviors Discovered
- **Deleting Cluster CR = PVCs also deleted** — CNPG has a finalizer that cleans up PVCs on Cluster deletion. `kubectl cnpg hibernate on` is the only safe way to delete Cluster CR while preserving PVCs. Direct `kubectl delete cluster` removes ALL PVCs.
- **PVC label requirement**: CNPG-managed PVCs must have labels `cnpg.io/cluster`, `cnpg.io/instanceName`, `cnpg.io/instanceRole`, `cnpg.io/pvcRole`, etc. PVCs created without these labels are NOT recognized by the operator.
- **Auto-promotion on restore**: When Cluster CR is recreated and ONLY the replica PVC exists (no pg-cluster-1), CNPG reads `latestGeneratedNode` from PVC annotations, promotes the replica data to primary, creates a fresh replica via streaming replication. No `initdb` is run if the PVC already has pgdata.
- **Kasten policy PVC filter**: Use `excludeResources` with `matchLabels: cnpg.io/instanceRole: primary` in the backup policy to backup ONLY the quiesced replica PVC. This ensures the restore uses the explicitly quiesced data.
- **WaitV2 for CNPG cluster**: `group: postgresql.cnpg.io`, `resource: clusters`, condition `{{ range .status.conditions }}{{ if and (eq .type "Ready") (eq .status "True") }}true{{ end }}{{ end }}`
- **Kasten `until` loop**: `grep -qE "complete|failed"` is case-sensitive — Kasten state is `Complete` (capital C), so use `grep -qE "Complete|Failed"`.
