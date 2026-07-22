# Grafana Mimir — Kasten Blueprint

Backup and restore [Grafana Mimir](https://grafana.com/docs/mimir/latest/) deployed with the
[`mimir-distributed`](https://grafana.com/docs/helm-charts/mimir-distributed/latest/) Helm chart,
using its **in-cluster MinIO** object storage backend.

## Why this pattern

Mimir is a multi-tenant Prometheus TSDB. **Its entire durable state lives in object storage**, not
on the workload PVCs:

| Data | Where it lives |
|---|---|
| Metrics (TSDB blocks) | object storage — bucket `mimir-tsdb` |
| Recording rules | object storage — bucket `mimir-ruler` |
| Alertmanager state | object storage — bucket `mimir-ruler` |
| Recent (~last 2 h) samples, not yet uploaded | ingester in-memory head + WAL, and Kafka (ingest-storage) |
| store-gateway / compactor / querier working dirs | local caches, rebuilt from object storage |

The `mimir-distributed` chart deploys **MinIO in-cluster by default** (`minio.enabled: true`), backed
by a single PVC named `mimir-minio`. Both the `mimir-tsdb` and `mimir-ruler` buckets live on that one
PVC. Therefore:

> **Kasten is the data mover by snapshotting the `mimir-minio` PVC.** That single PVC snapshot
> captures all metrics blocks, recording rules, and Alertmanager state.

This is **Pattern 5 — database snapshot via a local MinIO keeper (a PVC-backed workload that exposes
the S3 protocol)** from [CLAUDE.md](../CLAUDE.md).

**Making the snapshot as complete as possible.** Ingesters keep the most recent samples in memory and
in a WAL; those are only uploaded to object storage every ~2 h (or on shutdown). Mimir exposes
`POST /ingester/flush?wait=true`, which forces in-memory series to object storage **without stopping
ingestion**. The blueprint calls this in `backupPrehook` so the MinIO snapshot contains everything up
to (approximately) the moment of backup. The few seconds of samples that arrive between the flush and
the snapshot live only in the ingester WAL / Kafka and are outside the restore point — standard and
acceptable for a metrics TSDB.

**Only the MinIO object storage is backed up — and this scoping is mandatory.** The backup policy
uses `includeResources: app=minio`, so the restore point contains **only** the MinIO Deployment and
its PVC (plus the MinIO Service/Secret/ConfigMap). Everything else — ingester WAL, store-gateway and
compactor caches, Kafka — is transient and rebuilt from object storage. On restore, Kasten therefore
only scales the MinIO Deployment down and up to swap the PVC data; the Mimir compute cluster keeps
running untouched. This mirrors the Grafana maintainers' guidance: *point a Mimir cluster at the same
bucket and it discovers all the blocks*.

> ⚠️ **Do not back up the whole namespace.** Mimir's `mimir-rollout-operator` installs fail-closed
> admission webhooks (`no-downscale`, `prepare-downscale`, `pod-eviction`, `zpdb-validation`) that
> guard scaling of the ingester/store-gateway StatefulSets, plus a `min-time-between-zones-downscale:
> 12h` annotation. A full-namespace Kasten restore scales **every** workload to 0 (including the
> rollout-operator itself, which backs those webhooks), so the scale-downs are rejected and the
> restore **fails**. Scoping the backup to `app=minio` avoids touching any guarded workload, so the
> restore is fully automated. The Mimir compute cluster is (re)deployed independently via Helm/GitOps
> — Kasten restores only the object-storage **data**. This also honours the principle that *Kasten
> should not be responsible for restoring the operator itself*.

**What a restore does and does not roll back.** Because only the MinIO PVC is restored and
`restorePosthook` restarts only the store-gateways + compactor (never the ingesters), a restore is a
**point-in-time recovery of the long-term store**. Whatever the running ingesters hold in memory /
Kafka is **not** discarded — it keeps flowing forward and is shipped to the restored bucket on the
next flush, layered on top of the restored blocks (the compactor vertically compacts any overlapping
ranges). A restore recovers/repairs historical blocks; it does not roll back live recent metrics.

> **Hard rule (Grafana):** never run two sets of compactors against the same bucket simultaneously.
> A single restored cluster has one compactor set, so this is satisfied.

## Versions

Versions used when this blueprint was developed and tested:

| Component | Version |
|---|---|
| Kubernetes | `1.32` (EKS) |
| Kasten | `8.5.13` |
| Helm chart | `mimir-distributed 6.1.0` |
| Grafana Mimir | `3.1.2` (chart appVersion) |

Detect your Kasten version with:

```bash
helm ls -n kasten-io          # Helm install
# or, OpenShift OLM:
kubectl get csv -n kasten-io -o jsonpath='{.items[?(@.spec.displayName=="Kasten K10")].spec.version}'
```

---

## Step 2 — Deploy Mimir and create test data

### Deploy

```bash
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update grafana

kubectl create namespace mimir-test

helm install mimir grafana/mimir-distributed --version 6.1.0 \
  -n mimir-test -f test/test-values.yaml
```

> [test/test-values.yaml](test/test-values.yaml) disables the meta-monitoring operator and — for
> **fast validation only** — sets `querier.query_store_after: 0s` and
> `blocks_storage.bucket_store.ignore_blocks_within: 0s` so freshly flushed/restored blocks are
> served from object storage immediately instead of waiting out Mimir's default 10–12 h timers.
> These two settings are a testing convenience, **not** a blueprint requirement; omit them in
> production (older historical data still restores and serves immediately).

> **Storage class**: this deployment uses the cluster's **default** storage class for every PVC,
> including `mimir-minio`. On our test cluster that default is `ebs-sc` (the AWS EBS CSI class,
> which supports CSI snapshots). Ensure your cluster's default class — or the class you set via
> `minio.persistence.storageClassName` — is a **CSI storage class that supports snapshots** and has
> a matching `VolumeSnapshotClass` registered with Kasten (e.g. `managed-csi` on AKS,
> `standard-rwo` on GKE, your custom class on bare-metal). Do **not** use legacy in-tree classes
> (e.g. `gp2` on AWS) — they fail with "cannot find CSI PersistentVolumeSource" when a
> VolumeSnapshot is attempted.

Wait for all pods to be `Running`/`Ready` (~2–3 min):

```bash
kubectl get pods -n mimir-test
```

The chart deploys the ingest-storage architecture: distributor, 3 ingester zones, 3 store-gateway
zones, compactor, querier(s), query-frontend/scheduler, ruler, alertmanager, gateway (nginx), Kafka,
and **MinIO**. The MinIO config (endpoint, buckets, credentials) is:

```
endpoint : mimir-minio.mimir-test.svc:9000
buckets  : mimir-tsdb  (blocks),  mimir-ruler (rules + alertmanager)
creds    : grafana-mimir / supersecret        # chart defaults
```

### Create a known, verifiable dataset

Mimir ingests via the Prometheus **remote-write** protocol (protobuf + snappy). The helper
[`test/rw_push.py`](test/rw_push.py) is **dependency-free** (Python stdlib only — it hand-rolls the
protobuf and a literal-only snappy block) so it runs on a plain `python:3.12-slim` pod. It pushes 5
series named `kasten_backup_test{series="0".."4"}` with values `100..104`.

Run the helper pods in a **separate `mimir-tools` namespace** (not `mimir-test`) so they are not
part of the Kasten application — a bare `mc`/`mimir-util` pod inside `mimir-test` would be treated
as a workload by Kasten. ClusterIP services resolve cross-namespace, so the helpers reach Mimir/MinIO
by FQDN.

```bash
kubectl create namespace mimir-tools
# utility pod for pushing / querying / flushing
kubectl run mimir-util -n mimir-tools --image=python:3.12-slim --restart=Never -- sleep 36000
kubectl run mc         -n mimir-tools --image=minio/mc:latest  --restart=Never --command -- sleep 36000
kubectl wait --for=condition=Ready pod/mimir-util pod/mc -n mimir-tools --timeout=120s
kubectl cp test/rw_push.py mimir-tools/mimir-util:/tmp/rw_push.py

# Push the dataset (tenant "anonymous"; multitenancy is on by Mimir default)
kubectl exec -n mimir-tools mimir-util -- \
  python3 /tmp/rw_push.py http://mimir-gateway.mimir-test.svc:80/api/v1/push anonymous 0
# note the printed "ts=<epoch-ms>" — you need it to query the sample back
```

Verify it is queryable (query at "now" works right after the push, while the sample is still <5m old
and served from the ingester head):

```bash
kubectl exec -n mimir-tools mimir-util -- python3 -c '
import urllib.request, json
url="http://mimir-gateway.mimir-test.svc:80/prometheus/api/v1/query?query=kasten_backup_test"
req=urllib.request.Request(url); req.add_header("X-Scope-OrgID","anonymous")
d=json.load(urllib.request.urlopen(req,timeout=30))
for r in sorted(d["data"]["result"], key=lambda x:x["metric"].get("series","")):
    print(r["metric"]["series"], "=>", r["value"][1])
'
# expected: 0=>100 1=>101 2=>102 3=>103 4=>104
```

At this point the samples are in the ingester head, **not yet in MinIO**. To make them durable in
object storage (what the blueprint's `backupPrehook` does), flush every ingester:

```bash
for ip in $(kubectl get pods -n mimir-test -l app.kubernetes.io/component=ingester \
             -o jsonpath='{range .items[*]}{.status.podIP}{" "}{end}'); do
  kubectl exec -n mimir-tools mimir-util -- python3 -c "
import urllib.request
r=urllib.request.urlopen(urllib.request.Request('http://$ip:8080/ingester/flush?wait=true',data=b'',method='POST'),timeout=120)
print('$ip flush -> HTTP', r.status)"
done
```

Confirm blocks landed in the `mimir-tsdb` bucket:

```bash
kubectl exec -n mimir-tools mc -- sh -c '
mc alias set m http://mimir-minio.mimir-test.svc:9000 grafana-mimir supersecret >/dev/null
mc ls m/mimir-tsdb/anonymous/'      # -> one or more <ULID>/ block directories
```

---

## Cleanup / teardown

```bash
# Kasten restore-point content for this namespace
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=mimir-test

# The workload and its PVCs
helm uninstall mimir -n mimir-test
kubectl delete pvc --all -n mimir-test        # Helm leaves StatefulSet/MinIO PVCs behind
kubectl delete namespace mimir-test

# The helper pods
kubectl delete namespace mimir-tools
```

---

## Blueprint

| File | Purpose |
|---|---|
| [blueprint.yaml](blueprint.yaml) | The Kanister blueprint (`backupPrehook`, `restorePosthook`). |
| [blueprintbinding.yaml](blueprintbinding.yaml) | Binds the blueprint to the MinIO Deployment via an opt-in label. |
| [images/kasten-tools/Dockerfile](images/kasten-tools/Dockerfile) | The tool image used by both hooks. |

### Custom image

Both hooks run as `KubeTask` pods that need `kubectl` (enumerate ingester pods / delete
store-gateway + compactor pods across namespaces) and `curl` (call the ingester admin API). They use
**`michaelcourcy/kasten-tools:8.5.2`** — Kasten's `gcr.io/kasten-images/kanister-tools:8.5.2`
base with `kubectl` + `jq` added (base already has `curl`). Dockerfile:
[images/kasten-tools/Dockerfile](images/kasten-tools/Dockerfile). Rebuild/push:

```bash
cd images/kasten-tools
docker buildx build --platform linux/amd64 \
  --build-arg KASTEN_VERSION=8.5.2 \
  -t <your-registry>/kasten-tools:8.5.2 --push .
```

Both `KubeTask`s run in the **`kasten-io`** namespace so the pod inherits the Kasten
service account's cross-namespace RBAC (list ingester pods, roll StatefulSets in the
Mimir namespace).

### Blueprint actions

| Action | Runs | What it does |
|---|---|---|
| `backupPrehook` | before PVC snapshots | Enumerates the Running ingester pods in the Mimir namespace and calls `POST /ingester/flush?wait=true` (port 8080) on each, forcing in-memory/WAL series to the MinIO object storage. Does **not** stop ingestion. Fails the backup if any ingester cannot be flushed. |
| `restorePosthook` | after the MinIO PVC is restored | Deletes the store-gateway and compactor pods (recreated by their StatefulSet controllers) so they re-read the restored bucket, then **waits** (`kubectl wait --for=condition=Ready`, up to 15 m) for them to come back. A store-gateway is Ready only after its initial bucket sync, so a completed wait means the restored blocks are loaded and queryable — the RestoreAction reports `Complete` only once recovery is actually usable. Idempotent. |

**Intentionally absent** (keep the table and YAML in sync — these are documented omissions, not gaps):

- **No `backupPosthook`** — the flush does not quiesce or stop ingestion, so there is nothing to
  undo after the snapshot completes.
- **No `restorePrehook`** — Mimir needs no pre-restore preparation. MinIO is not reconciled by an
  operator (nothing fights the PVC swap), and recovery is purely *post-restore block
  re-discovery*. Because Mimir's recovery logic lives entirely in `restorePosthook` — which **is**
  triggered in current Kasten — Mimir restore is **not** blocked by the
  [known `restorePrehook`-not-triggered limitation](#restoreprehook-note) that affects
  operator-managed databases.

### Install the blueprint and bind it

```bash
kubectl apply -f blueprint.yaml            # into kasten-io

# Option A — opt-in BlueprintBinding (fleet automation):
kubectl apply -f blueprintbinding.yaml
kubectl label deploy mimir-minio -n mimir-test grafana-mimir-minio=true

# Option B — direct annotation on the MinIO Deployment (single instance):
kubectl annotate deploy mimir-minio -n mimir-test \
  kanister.kasten.io/blueprint=grafana-mimir-blueprint
```

### Scope the backup to MinIO only (mandatory policy filter)

The backup policy **must** be scoped to the MinIO object storage with `includeResources: app=minio`.
The MinIO Deployment, its PVC, Service, Secret and ConfigMap all carry `app=minio`; nothing else in
the namespace does. This captures exactly the durable data and the one workload Kasten needs to
scale to swap that data:

```yaml
# in the Policy spec: spec.actions[].backupParameters.filters
filters:
  includeResources:
    - matchLabels:
        app: minio
```

> **Why `includeResources`, not a whole-namespace backup** — a full-namespace restore makes Kasten
> scale every workload to 0, which the `mimir-rollout-operator`'s fail-closed webhooks reject (see
> the ⚠️ callout under [Why this pattern](#why-this-pattern)); the restore then fails. Scoping to
> `app=minio` means the restore only touches the (unguarded) MinIO Deployment, so it succeeds and is
> fully automated. It also keeps the restore point small and incremental (CSI snapshots of the
> ever-growing TSDB bucket).

> **Policy profile requirement**: the policy's `backupParameters.profile` must reference a
> **Location** profile (S3/GCS/Azure Blob), not an Infra profile, so the `RestorePointContent`
> records a Kanister-compatible profile that the restore extracts automatically.
> Check with: `kubectl get profile <name> -n kasten-io -o jsonpath='{.spec.type}'`

<a name="restoreprehook-note"></a>
### Note on `restorePrehook` in Kasten ≤ 8.5.x

Kasten ≤ 8.5.x does not trigger `restorePrehook` (a known limitation). **This blueprint does not
use `restorePrehook`**, so it is unaffected — its entire restore logic is in `restorePosthook`,
which is triggered normally. Combined with the MinIO-only backup scope, **the Mimir restore is fully
automated — no manual pre-restore step is required.**

---

## Step 5 — End-to-end backup and restore through Kasten

Prerequisites: blueprint applied and the MinIO Deployment bound (above); a **Location** profile in
`kasten-io`. Run the `mimir-util` / `mc` helper pods in a **separate** namespace (e.g. `mimir-tools`)
so they are not part of the Kasten application — they reach the Mimir/MinIO ClusterIP services
cross-namespace.

### Backup

Create an on-demand policy scoped to MinIO and trigger it:

```yaml
apiVersion: config.kio.kasten.io/v1alpha1
kind: Policy
metadata: { name: mimir-backup, namespace: kasten-io }
spec:
  comment: "Grafana Mimir — back up only the MinIO object storage; flush ingesters via blueprint"
  frequency: "@onDemand"
  actions:
    - action: backup
      backupParameters:
        profile: { name: <location-profile>, namespace: kasten-io }
        filters:
          includeResources:
            - matchLabels: { app: minio }
  selector:
    matchExpressions:
      - key: k10.kasten.io/appNamespace
        operator: In
        values: [mimir-test]
```

```bash
# trigger
kubectl create -f - <<'EOF'
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata: { generateName: run-mimir-, namespace: kasten-io }
spec: { subject: { kind: Policy, name: mimir-backup, namespace: kasten-io } }
EOF
```

**Confirm the flush ran** (the blueprint's `backupPrehook`). The KubeTask stdout is in the Kanister
controller logs (the `Out` field), and the ingesters log the flush:

```bash
kubectl logs -n kasten-io -l component=kanister --tail=20000 | grep -oP 'Out":"\K[^"]*' | grep -i flush
# -> "-> flushing ingester <ip>:8080 (wait=true)" / "HTTP 204" for each ingester
kubectl logs -n mimir-test mimir-ingester-zone-a-0 --since=10m | grep 'flushing TSDB blocks'
```

Verify the backup reached `Complete` and that Kasten snapshotted **only** the MinIO PVC:

```bash
kubectl get volumesnapshot -n mimir-test -l k10.kasten.io/appNamespace=mimir-test \
  -o custom-columns=NAME:.metadata.name,SRC:.spec.source.persistentVolumeClaimName
# -> exactly one k10-csi-snap-… sourced from mimir-minio
```

### Restore (fully automated — no manual pre-flight)

```bash
# 1) simulate loss of the durable object storage
kubectl exec -n mimir-tools mc -- sh -c \
  'mc alias set m http://mimir-minio.mimir-test.svc:9000 grafana-mimir supersecret >/dev/null; \
   mc rm --recursive --force m/mimir-tsdb/; mc rm --recursive --force m/mimir-ruler/'

# 2) restore (RestoreAction lives in the APPLICATION namespace; no profile needed)
RP=$(kubectl get restorepoint -n mimir-test --sort-by=.metadata.creationTimestamp \
       -o jsonpath='{.items[-1].metadata.name}')
kubectl create -f - <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata: { generateName: restore-mimir-, namespace: mimir-test }
spec:
  subject: { apiVersion: apps.kio.kasten.io/v1alpha1, kind: RestorePoint, name: ${RP}, namespace: mimir-test }
  targetNamespace: mimir-test
EOF
```

Kasten scales the MinIO Deployment down, restores its PVC from the snapshot, scales it back up, then
`restorePosthook` deletes the store-gateway + compactor pods and **waits** until they are Ready
(i.e. have re-synced the restored bucket). The RestoreAction reaches `Complete` only once recovery
is queryable — typically ~3–4 min for this test topology.

### Verify recovery

```bash
# instant queries only look back 5m; use a range query around the sample's original timestamp
SEC=<push-epoch-seconds>
kubectl exec -n mimir-tools mimir-util -- python3 -c "
import urllib.request, json
url='http://mimir-gateway.mimir-test.svc:80/prometheus/api/v1/query_range?query=kasten_backup_test&start=$((SEC-300))&end=$((SEC+300))&step=60'
req=urllib.request.Request(url); req.add_header('X-Scope-OrgID','anonymous')
d=json.load(urllib.request.urlopen(req,timeout=20))
print(sorted((r['metric']['series'], r['values'][-1][1]) for r in d['data']['result']))"
# -> [('0','<v>'),('1',...),...] the recovered series/values
```

> **Query-timing gotchas when validating** (they cost real debugging time):
> 1. **Instant queries look back only 5 min** from the eval time, and the eval instant must not
>    precede the sample. Prefer a `query_range` around the sample timestamp; if you use an instant
>    query, don't truncate a millisecond `ts` down to a whole second (that can land the eval instant
>    *before* the sample and return nothing).
> 2. By default store-gateways **ignore blocks younger than `ignore_blocks_within` (10 h)** and
>    queriers read only data older than `query_store_after` (12 h) from the store — so freshly
>    flushed/restored blocks are *not* served from object storage until they age. For fast
>    validation set `query_store_after: 0s` and `blocks_storage.bucket_store.ignore_blocks_within: 0s`
>    (see [test/test-values.yaml](test/test-values.yaml)). In production these defaults are fine:
>    older historical data is served from the restored bucket immediately; only very recent data
>    waits out the normal timing.
> 3. The **query-frontend caches results**. When iterating, vary the query or query the querier pod
>    directly to bypass the cache.

### End-to-end validation result

On the reference environment (versions above) the full cycle passed: backup flushed all three
ingesters (HTTP 204) and snapshotted only `mimir-minio`; after wiping the bucket, the restore
`Complete`d fully automatically and the wiped metrics were immediately queryable from the restored
blocks. Because `restorePosthook` restarts only the store-gateway + compactor, live data the
ingesters received after the backup was preserved (new blocks appear alongside the restored ones).

