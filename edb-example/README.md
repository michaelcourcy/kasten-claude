# EDB Postgres for Kubernetes — Kasten Blueprint

Backup and restore for **EDB Postgres for Kubernetes** clusters
(`postgresql.k8s.enterprisedb.io`), using **Pattern 1: Fence and quiesce a replica**.

The blueprint does not invent its own way of quiescing the database. EDB ships an official
**Kasten add-on**: when you enable it, the operator picks one instance, marks it, and publishes
the exact commands a backup tool must run to stop and restart that instance cleanly. The
blueprint reads those marks and runs those commands.

## Versions

| Component | Version |
|---|---|
| Kubernetes | `1.31.6` (OpenShift `4.18.6`) |
| Kasten | `9.0.5` |
| EDB operator (OperatorHub CSV) | `cloud-native-postgresql.v1.25.9` (certified operator) |
| EDB operator image actually run | `quay.io/enterprisedb/cloud-native-postgresql:1.25.4` (see [Operator image](#operator-image-on-a-cluster-without-an-edb-subscription)) |
| PostgreSQL | `16.10` (`quay.io/enterprisedb/postgresql:16.10`) |
| Tool image | `ghcr.io/kastenhq/blueprint-ai/kasten-tools:9.0.5` |
| Storage class | `managed-csi` (Azure Disk CSI) |

---

## EDB Postgres for Kubernetes and CloudNativePG — what is actually different

The short answer: **they are the same operator, and EDB's version adds enterprise features on
top.** CloudNativePG was written by EDB and later donated to the CNCF. EDB Postgres for
Kubernetes — today marketed as *EDB Postgres AI for CloudNativePG Cluster* — is EDB's supported
fork of it. The architecture, the `Cluster` custom resource, the bootstrap flow, the failover
logic and the instance-pod layout are the same. This is why the [cnpg-example](../cnpg-example/)
blueprint in this repository looks so similar to this one.

What differs in practice:

| | CloudNativePG | EDB Postgres for Kubernetes |
|---|---|---|
| Owner | CNCF project, Apache 2.0 | EDB, commercial (subscription for production) |
| API group | `postgresql.cnpg.io` | `postgresql.k8s.enterprisedb.io` |
| Label prefix | `cnpg.io/` | `k8s.enterprisedb.io/` |
| `kubectl` plugin | `kubectl cnpg` | `kubectl cnp` |
| Database engine | community PostgreSQL | community PostgreSQL, **EDB Postgres Advanced Server** (EPAS, Oracle compatibility), **EDB Postgres Extended** |
| Transparent Data Encryption | not available | available with EPAS / PG Extended 15+ |
| Support window | community, roughly 6 months per minor | long-term support releases, 18 months |
| Architectures | `amd64`, `arm64` | also `ppc64le` and `s390x` (IBM Power, IBM Z) |
| Distribution | public images, Helm chart | certified OpenShift operator; images in EDB's subscriber registry |
| **Kasten integration** | none — you write the quiescing yourself | **built in**: the `kasten` add-on, described below |

Two consequences matter when you protect these clusters.

**1. A CNPG blueprint does not work on EDB unchanged.** Every API group and every label prefix is
different, so [cnpg-example](../cnpg-example/) binds to nothing on an EDB cluster. That part is a
simple rename.

**2. The right mechanism is not the same either.** On CNPG, `cnpg-example` pauses WAL replay on a
replica (`pg_wal_replay_pause()`). The replica keeps running, so the snapshot is taken from a live
data directory and PostgreSQL has to crash-recover from it at restore time. On EDB you can do
better: the add-on **stops PostgreSQL on the elected instance**, so what Kasten snapshots is a
cleanly shut-down data directory. The blueprint proves this in its own log:

```
Database cluster state:               shut down in recovery
```

EDB calls this a **cold backup**, and it is the only mode EDB supports for external snapshot
tools such as Kasten.

### One more place you may already have EDB: Cloud Pak for Data

IBM Cloud Pak for Data installs this operator itself, repackaged, to run its internal metadata
databases (`zen-metastore-edb`, `common-service-db`, and others). On the cluster this blueprint was
developed on, that install was `cloud-native-postgresql.v1.25.3` in the `cpd-operators` namespace,
with images from `icr.io/cpopen`. It is scoped to the CP4D namespaces only, so it does not manage
anything you create elsewhere.

Those CP4D clusters use a sibling add-on, `external-backup-adapter-cluster`, wired to IBM's own
`icpdsupport/*` labels rather than the `kasten-enterprisedb.io/*` ones. **Do not point this
blueprint at them**: their backup is CP4D's business, covered by [cp4d-example](../cp4d-example/).

---

## How the EDB Kasten add-on works

You enable it with one annotation on the `Cluster`:

```yaml
metadata:
  annotations:
    k8s.enterprisedb.io/addons: '["kasten"]'
```

Once the cluster is healthy, the operator chooses the replica whose replication is furthest ahead,
records it on the `Cluster`, and decorates the namespace so a backup tool can tell what to back up
and what to leave alone:

| Object | Mark the operator adds | Meaning |
|---|---|---|
| `Cluster` | annotation `k8s.enterprisedb.io/backupInstance: edb-cluster-2` | the elected instance |
| elected pod | label `kasten-enterprisedb.io/hasHooks: "true"` | run the hooks in this pod |
| elected pod | annotations `kasten-enterprisedb.io/pre-backup-command`, `post-backup-command`, `pre-backup-on-error`, and the matching `*-container` keys | the commands to run, and in which container |
| elected instance PVCs (`PG_DATA` and `PG_WAL`) | label `kasten-enterprisedb.io/elected: "true"` | snapshot these |
| every other pod and PVC | label `kasten-enterprisedb.io/excluded: "true"` | do not snapshot these |

The commands themselves are fixed by the operator:

```
pre-backup :  /controller/manager snapshot hook pre backup
post-backup:  /controller/manager snapshot hook post backup
on-error   :  /controller/manager snapshot hook pre onError
```

The pre-backup command adds the instance to `k8s.enterprisedb.io/fencedInstances` on the
`Cluster`. The operator then shuts PostgreSQL down in that pod — the pod keeps running (so
`kubectl exec` still works), but the database is stopped. The post-backup command takes the
instance back out of that list and the operator restarts it, after which it catches up with the
primary through normal streaming replication.

### The one thing the operator does **not** do

The operator also writes `kanister.kasten.io/blueprint: edb-hooks` on the elected pod, which looks
like it should be enough to wire everything up. **It is not.** We tested it: with the blueprint
named `edb-hooks` deployed in `kasten-io` and no `BlueprintBinding`, a Kasten backup ran to
completion and **never fenced anything**. Kasten does not act on that annotation when it is on a
bare pod, and it reports no warning — you get a green backup of a running database.

So you must bind the blueprint yourself. [blueprintbinding.yaml](blueprintbinding.yaml) binds it to
the `Cluster` resource, which is also the right object: the blueprint then knows which cluster it
is working on and cannot accidentally fence the elected instance of a different cluster in the same
namespace.

The blueprint keeps the name `edb-hooks` anyway, so the operator's annotation and the deployed
blueprint agree.

---

## Blueprint actions

| Action | What it does |
|---|---|
| `backupPrehook` | Finds the elected instance, runs the operator's pre-backup command to fence it, checks that the fence really took effect (the `Cluster` lists the instance **and** `postmaster.pid` is gone), logs the `pg_controldata` cluster state, then runs `sync` in that pod. Fails the backup if the add-on is not enabled. |
| `backupPosthook` | Runs the operator's post-backup command to unfence the instance, waits up to a minute for the operator to drop it from the fence list, and removes it by hand if it is still there. |
| `restorePosthook` | Removes the fencing annotation from the restored `Cluster` if it carries one, then waits for the cluster to report a ready instance. |

### Why `backupPrehook` ends with `sync`

A CSI snapshot copies the **block device**, and Kasten does not freeze the filesystem. Data that
PostgreSQL wrote while shutting down can still be sitting in the kernel page cache, not yet on the
disk. The snapshot would then contain files with the right names and missing content, and you
would only find out at restore time. `sync` is therefore the last thing the prehook does — anything
after it could dirty the cache again.

Both volumes of the elected instance (`PG_DATA` at `/var/lib/postgresql/data` and `PG_WAL` at
`/var/lib/postgresql/wal`) are mounted in that one pod, so a single `sync` covers the whole
snapshotted set. And because the blueprint finds the pod through the operator's own election —
the same election the policy filter uses to pick PVCs — the set that gets flushed cannot drift
away from the set that gets snapshotted.

### Why `backupPrehook` refuses to run when the add-on is off

If the `kasten` add-on is not enabled there is no elected instance, nothing gets fenced, and
nothing gets labelled `excluded` either. A backup would then quietly snapshot every PVC of a
running cluster — a hot copy that looks exactly like a successful backup. The prehook stops with
an explicit message instead:

```
ERROR: cluster edb-test/edb-cluster has no elected backup instance.
       The EDB Kasten add-on is not enabled on this cluster. Add:
         k8s.enterprisedb.io/addons: '["kasten"]'
       Refusing to continue: without fencing the snapshot would be taken
       from a running instance and is not a cold backup.
```

---

## What this pattern costs you

| | |
|---|---|
| Impact on the primary | none — the primary keeps serving reads and writes throughout |
| Impact on the elected replica | it is **down** for the length of the snapshot (seconds with CSI). It serves no read-only traffic during that time, and needs a short catch-up afterwards |
| Single-instance cluster | there is no replica, so the **primary** is the one fenced, which means write downtime. The operator refuses this unless you also set `k8s.enterprisedb.io/snapshotAllowColdBackupOnPrimary: enabled`. Acceptable in development, rarely in production |
| Recovery granularity | you restore to a restore point. **There is no point-in-time recovery**: the add-on captures no WAL archive. RPO is your policy frequency |
| Incrementality | good — Kasten snapshots the same two volumes every time, and only changed blocks move on export |

If you need point-in-time recovery, this is the wrong pattern. Use a barman-based design instead;
[cnpg-barman-cloud-example](../cnpg-barman-cloud-example/) shows that shape on CNPG and ports to
EDB the same way (rename the API group and the labels).

---

## Prerequisites

### Installing the EDB operator

You need an EDB operator that watches your application namespace. Pick whichever of these matches
your situation.

**Option A — OpenShift OperatorHub (what we used).** The operator is a Red Hat certified operator
named `cloud-native-postgresql`:

```bash
kubectl create ns edb-operator
kubectl create ns edb-test

cat <<EOF | kubectl apply -f -
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: edb-operator-group
  namespace: edb-operator
spec:
  targetNamespaces:
    - edb-test
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: cloud-native-postgresql
  namespace: edb-operator
spec:
  channel: stable-v1.25
  name: cloud-native-postgresql
  source: certified-operators
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
EOF
```

**Option B — plain manifest or Helm, outside OLM.** This is EDB's own documented path and the one
to use on a cluster that is not OpenShift. **We did not run it on this cluster** (see the warning
below), so treat the exact version numbers as illustrative:

```bash
# Manifest install, cluster-wide
kubectl apply -f https://get.enterprisedb.io/cnp/postgresql-operator-1.25.9.yaml

# or Helm
helm repo add edb https://enterprisedb.github.io/edb-postgres-for-kubernetes-charts/
helm upgrade --install edb-pg4k edb/edb-postgres-for-kubernetes \
  --namespace edb-operator --create-namespace
```

Both of these create their own copies of the EDB CRDs.

> ⚠️ **Do not use Option B on a cluster where something else already installed the EDB operator.**
> The CRDs (`clusters.postgresql.k8s.enterprisedb.io` and the rest) are cluster-wide singletons.
> A manifest or Helm install overwrites the ones the other operator is using, and if the versions
> differ it can break the clusters that operator manages. On our cluster CP4D owned them, so we
> used Option A.

**Option C — the operator is already installed (Cloud Pak for Data).** Check before installing
anything:

```bash
kubectl get csv -A | grep cloud-native-postgresql
kubectl get crd | grep enterprisedb
```

A CP4D install watches only the CP4D namespaces. To have it also manage your namespace you must
add that namespace to IBM's `NamespaceScope`, which restarts every CP4D operator pod — so in most
cases installing a second, separately scoped operator (Option A) is the smaller change.

### Two EDB operators on the same cluster

This works, and we verified it. The certified operator was installed into `edb-operator` with an
`OperatorGroup` targeting `edb-test` only, alongside CP4D's `cloud-native-postgresql.v1.25.3` in
`cpd-operators`. OLM records both owners on the shared CRDs:

```
operatorframework.io/installed-alongside-...: cpd-operators/cloud-native-postgresql.v1.25.3
operatorframework.io/installed-alongside-...: edb-operator/cloud-native-postgresql.v1.25.9
```

and it gives each install its own admission webhooks, selected by the `OperatorGroup` UID, so the
two never see each other's namespaces. CP4D's four clusters stayed healthy throughout.

Two things to be careful about:

- **Choose a channel close to the version already installed.** OLM applies the new CSV's CRDs on
  top of the existing ones. We picked `stable-v1.25` against CP4D's `1.25.3`, so the change was a
  patch-level one. Subscribing to `stable-v1.30` would have replaced the CRDs with a schema three
  minor versions newer, underneath an operator still running 1.25.
- **Check `WATCH_NAMESPACE` after install.** It is populated from the pod annotation
  `olm.targetNamespaces`. If that annotation is empty the operator watches the **whole cluster** and
  will try to reconcile the other operator's clusters:

  ```bash
  kubectl get pods -n edb-operator \
    -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.annotations.olm\.targetNamespaces}{"\n"}{end}'
  # expected: postgresql-operator-controller-manager-xxxxx edb-test
  ```

### Operator image on a cluster without an EDB subscription

The certified CSV pulls its operator image from `docker.enterprisedb.com`, which needs an EDB
subscription token in a secret named `postgresql-operator-pull-secret`. With a subscription you
create that secret and everything works; nothing below is needed.

We had no token, so we pointed the CSV at EDB's public evaluation image on quay.io instead. Those
images stop at `1.25.4` in the 1.25 series, which is why the running operator is a few patches
behind its CSV:

```bash
CSV=cloud-native-postgresql.v1.25.9
IMG=quay.io/enterprisedb/cloud-native-postgresql:1.25.4
kubectl patch csv -n edb-operator $CSV --type=json -p="[
 {\"op\":\"replace\",\"path\":\"/spec/install/spec/deployments/0/spec/template/spec/containers/0/image\",\"value\":\"$IMG\"},
 {\"op\":\"replace\",\"path\":\"/spec/install/spec/deployments/0/spec/template/spec/containers/0/env/2/value\",\"value\":\"$IMG\"},
 {\"op\":\"replace\",\"path\":\"/spec/install/spec/deployments/0/spec/template/spec/containers/0/env/3/value\",\"value\":\"$IMG\"}]"
```

Env entries 2 and 3 are `RELATED_IMAGE_CNP` and `OPERATOR_IMAGE_NAME`. They matter as much as the
first patch: the operator injects **itself** as the init container of every instance pod, so if
they still point at the subscriber registry the database pods fail to start.

> `registry.connect.redhat.com/enterprisedb/cloud-native-postgresql:1.25.9` pulls with a normal
> OpenShift entitlement, but the tag there is a ~1.4 MB placeholder, not the operator. It fails
> with `executable file /manager not found`. Do not use it.

### Tool image

The blueprint runs `ghcr.io/kastenhq/blueprint-ai/kasten-tools:9.0.5`, which is Kasten's
`kanister-tools` image plus `kubectl` and `jq`. Dockerfile:
[../images/kasten-tools/Dockerfile](../images/kasten-tools/Dockerfile). Build it yourself with:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  --build-arg KASTEN_VERSION=9.0.5 \
  --build-arg KUBECTL_VERSION=1.31.6 \
  -t <your-registry>/kasten-tools:9.0.5 --push images/kasten-tools/
```

The tag must match the Kasten version on your cluster (`kubectl get csv -n kasten-io`, or
`helm ls -n kasten-io` for a Helm install).

---

## Deploy the test workload

```bash
kubectl apply -f edb-cluster.yaml
```

[edb-cluster.yaml](edb-cluster.yaml):

```yaml
apiVersion: postgresql.k8s.enterprisedb.io/v1
kind: Cluster
metadata:
  name: edb-cluster
  namespace: edb-test
  annotations:
    k8s.enterprisedb.io/addons: '["kasten"]'
spec:
  instances: 2
  imageName: quay.io/enterprisedb/postgresql:16.10
  storage:
    size: 1Gi
    storageClass: managed-csi
  walStorage:
    size: 1Gi
    storageClass: managed-csi
```

> **Storage class**: `managed-csi` is the Azure Disk CSI storage class used in our test
> environment. Replace it with a storage class that supports CSI snapshots on your cluster
> (for example, `ebs-sc` on AWS, `standard-rwo` on GKE, your own class on bare metal, and so on).
> The class must have a matching `VolumeSnapshotClass` registered with Kasten.
> Do **not** use legacy in-tree classes (for example, `gp2` on AWS) — they do not support CSI
> snapshots.

`walStorage` is optional. It is included here on purpose: it gives the elected instance two PVCs
instead of one, which is the layout EDB recommends and the case a blueprint is most likely to get
wrong.

Wait for the cluster, then confirm the add-on elected an instance:

```bash
kubectl wait cluster.postgresql.k8s.enterprisedb.io edb-cluster -n edb-test \
  --for=jsonpath='{.status.readyInstances}'=2 --timeout=8m

kubectl get cluster.postgresql.k8s.enterprisedb.io -n edb-test edb-cluster \
  -o jsonpath='{.metadata.annotations.k8s\.enterprisedb\.io/backupInstance}{"\n"}'
# expected: edb-cluster-2

kubectl get pvc -n edb-test -l kasten-enterprisedb.io/elected=true
# expected: edb-cluster-2 and edb-cluster-2-wal
```

If `backupInstance` is empty, the add-on did not run. Check that the annotation is spelled exactly
`k8s.enterprisedb.io/addons` and that the cluster reached a healthy state at least once.

## Create test data

```bash
kubectl exec -n edb-test edb-cluster-1 -c postgres -- \
  psql -U postgres -c "CREATE DATABASE kasten_test;"

kubectl exec -n edb-test edb-cluster-1 -c postgres -- \
  psql -U postgres -d kasten_test -c "
    CREATE TABLE employees (
      id SERIAL PRIMARY KEY,
      name VARCHAR(50),
      department VARCHAR(50),
      salary INTEGER
    );
    INSERT INTO employees (name, department, salary) VALUES
      ('Alice Martin',   'Engineering', 95000),
      ('Bob Chen',       'Engineering', 88000),
      ('Carol Smith',    'Marketing',   72000),
      ('David Johnson',  'Sales',       68000),
      ('Eve Williams',   'Engineering', 102000);
  "

kubectl exec -n edb-test edb-cluster-1 -c postgres -- \
  psql -U postgres -d kasten_test -c "SELECT * FROM employees ORDER BY id;"
```

> On an **EPAS** cluster the superuser may be `enterprisedb` rather than `postgres`. The blueprint
> itself never runs `psql`, so this only affects these test commands.

## Deploy the blueprint and the binding

```bash
kubectl apply -f blueprint.yaml
kubectl apply -f blueprintbinding.yaml
```

## Create the backup policy

The policy must snapshot **only the elected instance's volumes**. Rather than naming them, exclude
everything the operator marked as excluded — that way the filter follows the operator's election
if it ever changes:

```yaml
# policy.yaml, with <LOCATION_PROFILE> replaced
        filters:
          excludeResources:
            - matchLabels:
                kasten-enterprisedb.io/excluded: "true"
```

```bash
sed 's/<LOCATION_PROFILE>/my-s3-bucket/' policy.yaml | kubectl apply -f -
```

This is not a theoretical concern. After the restore test below, the operator built a new replica
and elected **that** one (`edb-cluster-3`) as the backup instance. The very next backup fenced and
snapshotted the new instance with no change to the policy or the blueprint, because both read the
operator's labels rather than a PVC name.

The profile must be a **Location** profile, not an Infra profile, otherwise the restore point
carries no Kanister-compatible profile and `restorePosthook` has nothing to run with. Check with:

```bash
kubectl get profile my-s3-bucket -n kasten-io -o jsonpath='{.spec.type}'
```

## Run a backup

```bash
kubectl create -f - --validate=false <<'EOF'
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata:
  generateName: run-edb-
  namespace: edb-test
spec:
  subject:
    kind: Policy
    name: edb-backup-policy
    namespace: edb-test
EOF
```

While it runs you can watch the fence appear and disappear:

```bash
watch -n2 'kubectl get cluster.postgresql.k8s.enterprisedb.io -n edb-test edb-cluster \
  -o jsonpath="{.metadata.annotations.k8s\.enterprisedb\.io/fencedInstances}"'
```

Check the blueprint output in the Kanister log:

```bash
kubectl logs -n kasten-io -l app=kanister-svc --tail=2000 \
  | grep -o '"Pod_Out":"[^"]*"'
```

Expected:

```
Elected backup instance: edb-cluster-2
Running pre-backup hook in edb-cluster-2/postgres: /controller/manager snapshot hook pre backup
Database cluster state:               shut down in recovery
Flushing filesystems on edb-cluster-2
Instance edb-cluster-2 fenced and flushed — ready for snapshot
Running post-backup hook in edb-cluster-2/postgres: /controller/manager snapshot hook post backup
Instance edb-cluster-2 unfenced
```

## Restore

### Step 1 — break the data, so the restore proves something

```bash
PRIMARY=$(kubectl get pods -n edb-test \
  -l "k8s.enterprisedb.io/cluster=edb-cluster,k8s.enterprisedb.io/instanceRole=primary" \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n edb-test "$PRIMARY" -c postgres -- \
  psql -U postgres -d kasten_test -c "DELETE FROM employees WHERE department = 'Engineering';"
# DELETE 3 — two rows left
```

### Step 2 — delete the Cluster (manual, required)

The operator owns the PVC lifecycle, so deleting the `Cluster` stops every pod and removes every
PVC, leaving the namespace clean for the restore:

```bash
kubectl delete cluster.postgresql.k8s.enterprisedb.io edb-cluster -n edb-test

# wait until they are really gone
kubectl get pods,pvc -n edb-test
```

### Step 3 — trigger the restore

```bash
RESTORE_POINT=$(kubectl get restorepoint -n edb-test \
  --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1].metadata.name}')

kubectl create -f - --validate=false <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-
  namespace: edb-test
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: ${RESTORE_POINT}
    namespace: edb-test
  targetNamespace: edb-test
EOF
```

On Kasten 9.x no `profile` field is needed — Kasten reads it from the restore point content. On
Kasten 8.x you must add `profile: {name: <LOCATION_PROFILE>, namespace: kasten-io}`, otherwise the
restore fails **after** restoring the volumes and `restorePosthook` never runs.

### Step 4 — what happens next, without you doing anything

1. Kasten restores the two elected PVCs and recreates the `Cluster`.
2. The operator sees an instance PVC that already holds data and **promotes it to primary** — no
   `initdb` runs. In our test `edb-cluster-2`, the fenced replica, came back as the primary.
3. The operator builds a fresh replica (`edb-cluster-3`) by streaming replication to get back to
   `instances: 2`.
4. `restorePosthook` clears any fencing annotation and waits until an instance is ready.

### Step 5 — verify

```bash
PRIMARY=$(kubectl get pods -n edb-test \
  -l "k8s.enterprisedb.io/cluster=edb-cluster,k8s.enterprisedb.io/instanceRole=primary" \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n edb-test "$PRIMARY" -c postgres -- \
  psql -U postgres -d kasten_test -c "SELECT * FROM employees ORDER BY id;"
# expected: 5 rows — Alice, Bob, Carol, David, Eve
```

---

## When something goes wrong

### The cluster is stuck with a fenced instance

If a backup fails between the pre-hook and the post-hook, the instance stays fenced and stays
down. `backupPosthook` is not guaranteed to run after a failure. Check and clear it by hand:

```bash
kubectl get cluster.postgresql.k8s.enterprisedb.io -n edb-test edb-cluster \
  -o jsonpath='{.metadata.annotations.k8s\.enterprisedb\.io/fencedInstances}{"\n"}'

# clear it
kubectl annotate cluster.postgresql.k8s.enterprisedb.io -n edb-test edb-cluster \
  k8s.enterprisedb.io/fencedInstances-

# or, with the EDB plugin
kubectl cnp fencing off edb-cluster -n edb-test
```

### A restored cluster never starts

Look for the fencing annotation first — a backup tool that captures the `Cluster` manifest while
an instance is fenced restores it fenced. `restorePosthook` handles this, but if you restored
without the blueprint, clear it with the command above.

> On Kasten 9.0.5 the captured manifest did **not** contain the fencing annotation, so this did not
> happen in our tests. The step is there because EDB documents the failure and because manifest
> capture timing is not something a blueprint should depend on.

### The backup succeeds but nothing is fenced

Two likely causes, in order:

1. **No `BlueprintBinding`.** See [the note above](#the-one-thing-the-operator-does-not-do) — the
   operator's pod annotation is not enough. Check with
   `kubectl get blueprintbinding -n kasten-io`.
2. **The add-on is not enabled** on that cluster. Then the prehook fails loudly rather than
   backing up hot, so you would see a failed action rather than a green one.

### Reading a failed action

```bash
kubectl get backupaction <name> -n edb-test -o jsonpath='{.status.error}'
kubectl logs -n kasten-io -l app=kanister-svc --tail=2000 | grep -o '"Pod_Out":"[^"]*"'
```

---

## Remove everything

```bash
# workload
kubectl delete cluster.postgresql.k8s.enterprisedb.io edb-cluster -n edb-test
kubectl delete policy edb-backup-policy -n edb-test
kubectl delete namespace edb-test

# Kasten restore point contents
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=edb-test

# blueprint
kubectl delete blueprintbinding edb-hooks-binding -n kasten-io
kubectl delete blueprint edb-hooks -n kasten-io

# operator (Option A install)
kubectl delete subscription cloud-native-postgresql -n edb-operator
kubectl delete csv -n edb-operator -l operators.coreos.com/cloud-native-postgresql.edb-operator
kubectl delete namespace edb-operator
```

> **Do not delete the EDB CRDs** if another operator (CP4D, for example) still uses them. Deleting
> `clusters.postgresql.k8s.enterprisedb.io` deletes every EDB cluster on the cluster, including
> theirs.

---

## Files

| File | Purpose |
|---|---|
| [blueprint.yaml](blueprint.yaml) | the blueprint (`backupPrehook`, `backupPosthook`, `restorePosthook`) |
| [blueprintbinding.yaml](blueprintbinding.yaml) | binds it to every EDB `Cluster` not carrying its own blueprint annotation |
| [edb-cluster.yaml](edb-cluster.yaml) | the test cluster, with the `kasten` add-on enabled |
| [policy.yaml](policy.yaml) | backup policy with the exclusion filter |

---

## Initial prompt

> can you make a blueprint for edb; it's close in my opinion to cnpg in term of pattern but need
> to be checked. also in the readme can you explain the difference between edb and cnpg
