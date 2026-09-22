# CNPG (CloudNativePG) — Kasten Blueprint

Backup and restore for [CloudNativePG](https://cloudnative-pg.io/) PostgreSQL clusters using
**Pattern 1: Fence and quiesce a replica**.

## Versions

| Component | Version |
|---|---|
| Kubernetes | `1.31` |
| OpenShift | `4.18.6` |
| Kasten | `9.0.5` |
| CNPG Helm chart | `cloudnative-pg 0.28.3` |
| CNPG operator | `1.29.1` |
| PostgreSQL | `18.3` |
| Storage | Azure Disk CSI (`managed-csi`) |
| Tool image | `ghcr.io/kastenhq/blueprint-ai/kasten-tools:9.0.5` ([Dockerfile](../images/kasten-tools/Dockerfile)) |

The blueprint was first developed on EKS `1.32` with Kasten `8.5.4` and CNPG `1.29.0`, then
re-validated end-to-end on the OpenShift cluster above. Nothing in `blueprint.yaml` is
platform-specific — only the **operator installation** needs an OpenShift adjustment, described
in [Prerequisites](#install-the-cnpg-operator).

## Pattern

**Pattern 1 — Fence and quiesce a replica.**
A standby replica's WAL replay is paused immediately before Kasten snapshots the PVCs.
This creates a guaranteed-consistent checkpoint on the replica with zero primary impact.
The quiesced replica's PVC snapshot is the sole source of truth for restore.

## How it works

### Backup

1. `backupPrehook` pauses WAL replay on a replica pod (`pg_wal_replay_pause()`).
2. Kasten snapshots **only the replica PVC** (configured via policy resource filter — see below).
3. `backupPosthook` resumes WAL replay (`pg_wal_replay_resume()`).

### Restore

1. Manually delete the CNPG Cluster CR before restore. The operator stops all pods and
   garbage-collects PVCs (CNPG owns PVC lifecycle).
2. Kasten restores the replica PVC from the quiesced snapshot, with its original CNPG labels.
3. Kasten recreates the Cluster CR from the backup.
4. The CNPG operator detects the existing replica PVC with data (`latestGeneratedNode > 0`),
   **promotes it to primary automatically** — no `initdb` is run.
5. A fresh replica is created via streaming replication.
6. `restorePosthook` waits for the cluster condition `Ready`.

## Blueprint actions

| Action | What it does |
|---|---|
| `backupPrehook` | Finds a replica pod, pauses WAL replay (`pg_wal_replay_pause()`) |
| `backupPosthook` | Resumes WAL replay on the same replica (`pg_wal_replay_resume()`) |
| `restorePosthook` | Waits for the cluster condition `Ready` (operator handles primary promotion automatically) |

## Manual pre-restore preparation

Perform these steps manually before triggering a Kasten restore:

```bash
# Delete the Cluster CR — operator will terminate all pods and remove PVCs
kubectl delete cluster.postgresql.cnpg.io pg-cluster -n cnpg-test

# Confirm all pods and PVCs are gone
kubectl get pods -n cnpg-test
kubectl get pvc -n cnpg-test
```

Once the restore completes, `restorePosthook` waits for the cluster to be ready automatically.

## Kasten policy — PVC resource filter (required)

The backup policy **must** exclude the primary PVC so only the quiesced replica PVC is snapshotted.
Use `excludeResources` in `spec.actions[].backupParameters.filters`:

```yaml
apiVersion: config.kio.kasten.io/v1alpha1
kind: Policy
metadata:
  name: cnpg-backup-policy
  namespace: kasten-io
spec:
  comment: "CNPG backup - quiesced replica PVC only, primary excluded"
  frequency: "@onDemand"
  actions:
    - action: backup
      backupParameters:
        profile:
          name: <LOCATION_PROFILE>
          namespace: kasten-io
        filters:
          excludeResources:
            - matchLabels:
                cnpg.io/instanceRole: primary
  selector:
    matchExpressions:
      - key: k10.kasten.io/appNamespace
        operator: In
        values:
          - <APP_NAMESPACE>
```

This ensures Kasten snapshots only the quiesced replica PVC.
When triggering a RestoreAction, Kasten will restore the replica PVC only — the CNPG operator
then auto-promotes it to primary.

Check the filter did what you expect: after a backup there must be exactly **one** VolumeSnapshot,
and its source must be the replica PVC.

```bash
kubectl get volumesnapshot -n cnpg-test
# NAME                            READYTOUSE   SOURCEPVC      ...
# k10-csi-snap-4bc2xvswcwlbjqr4   true         pg-cluster-2   ...
```

## Prerequisites

### Install the CNPG operator

```bash
helm repo add cnpg https://cloudnative-pg.github.io/charts
helm repo update cnpg
helm install cnpg cnpg/cloudnative-pg \
  --namespace cnpg-system \
  --create-namespace \
  --version 0.28.3 \
  --wait --timeout 3m
```

#### On OpenShift: the operator pod needs one extra value

The command above **fails on OpenShift**. The Helm chart sets a fixed UID and GID on the operator
container:

```yaml
containerSecurityContext:
  runAsUser: 10001
  runAsGroup: 10001
```

OpenShift gives every namespace its own range of allowed UIDs (written in the
`openshift.io/sa.scc.uid-range` annotation on the namespace) and the `restricted-v2` Security
Context Constraint only admits pods whose UID falls inside that range. `10001` is not in the range,
so the pod is never created and `helm install --wait` ends on a timeout:

```
Error: INSTALLATION FAILED: context deadline exceeded
```

The real reason is only visible in the ReplicaSet events, not in the Helm output:

```bash
kubectl get events -n cnpg-system --sort-by=.lastTimestamp
```

```
Error creating: pods "cnpg-cloudnative-pg-..." is forbidden: unable to validate against any
security context constraint: [... provider restricted-v2: .containers[0].runAsUser:
Invalid value: 10001: must be in the ranges: [1001120000, 1001129999] ...]
```

The fix is to remove the two fields and let OpenShift assign the UID and GID itself. The operator
is a Go binary that does not care which UID it runs under:

```bash
cat > cnpg-openshift-values.yaml <<'EOF'
# OpenShift: let the restricted-v2 SCC assign the UID and GID from the namespace range.
# The chart defaults (runAsUser/runAsGroup 10001) are outside that range and the pod is rejected.
containerSecurityContext:
  runAsUser: null
  runAsGroup: null
EOF

helm install cnpg cnpg/cloudnative-pg \
  --namespace cnpg-system \
  --create-namespace \
  --version 0.28.3 \
  -f cnpg-openshift-values.yaml \
  --wait --timeout 5m
```

Do **not** work around this by granting the `anyuid` SCC to the operator service account. That
raises the privileges of the operator for no benefit, while the values override keeps it under
`restricted-v2`.

> **The PostgreSQL cluster pods need nothing.** Only the operator Deployment is affected. The CNPG
> operator detects that it is running on OpenShift (the `SecurityContextConstraints` API is
> present) and leaves `runAsUser` and `runAsGroup` unset on the instance pods it creates, so
> `restricted-v2` assigns them a valid UID. The `Cluster` manifest below is the same on OpenShift
> as anywhere else.

### Deploy the blueprint and binding

```bash
kubectl apply -f blueprint.yaml
kubectl apply -f blueprintbinding.yaml
```

Both hooks run in the shared `kasten-tools` image (`kanister-tools` plus `kubectl` and `jq`),
built from [../images/kasten-tools/Dockerfile](../images/kasten-tools/Dockerfile). The tag must
match the Kasten version installed on the cluster — here `9.0.5`. Detect yours with
`helm ls -n kasten-io`, and if it differs, update the two `image:` lines in `blueprint.yaml`.

## Deploy the test workload

```bash
kubectl create namespace cnpg-test

cat <<EOF | kubectl apply -f -
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-cluster
  namespace: cnpg-test
spec:
  instances: 2
  storage:
    size: 1Gi
    storageClass: managed-csi
EOF
```

> **Storage class**: `managed-csi` is the Azure Disk CSI storage class used in our test
> environment. Replace it with a storage class that supports CSI snapshots on your cluster
> (for example, `ebs-sc` on AWS, `standard-rwo` on GKE, your custom class on bare-metal, and so on).
> The class must have a matching `VolumeSnapshotClass` registered with Kasten.
> Do **not** use legacy in-tree classes (for example, `gp2` on AWS) — they do not support CSI snapshots.

Wait for the cluster to be ready:

```bash
kubectl wait cluster.postgresql.cnpg.io pg-cluster -n cnpg-test \
  --for=condition=Ready --timeout=8m
kubectl get pods -n cnpg-test -l cnpg.io/cluster=pg-cluster
```

## Create test data

```bash
kubectl exec -n cnpg-test pg-cluster-1 -- \
  psql -U postgres -c "CREATE DATABASE kasten_test;"

kubectl exec -n cnpg-test pg-cluster-1 -- \
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

kubectl exec -n cnpg-test pg-cluster-1 -- \
  psql -U postgres -d kasten_test -c "SELECT * FROM employees ORDER BY id;"
```

## Run a backup

Trigger the on-demand policy with a `RunAction` and wait for it to complete:

```bash
kubectl create -f - <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata:
  generateName: run-cnpg-
  namespace: kasten-io
spec:
  subject:
    kind: Policy
    name: cnpg-backup-policy
    namespace: kasten-io
EOF

kubectl get backupaction -n cnpg-test
```

Confirm in the Kanister log that both hooks ran:

```bash
kubectl logs -n kasten-io -l component=kanister --tail=2000 | grep -E 'WAL replay'
# Pausing WAL replay on replica pg-cluster-2
# WAL replay paused: t
# Resuming WAL replay on replica pg-cluster-2
# WAL replay paused after resume: f
```

## Corrupt the data

```bash

kubectl exec -n cnpg-test pg-cluster-1 -- \
  psql -U postgres -d kasten_test -c "DELETE FROM employees WHERE department = 'Engineering';"

kubectl exec -n cnpg-test pg-cluster-1 -- \
  psql -U postgres -d kasten_test -c "SELECT * FROM employees ORDER BY id;"

```

## Restore

Delete the `Cluster` CR first — the operator then terminates the pods and garbage-collects the
PVCs, so Kasten restores into an empty namespace:

```bash
# delete the cnpg cluster
kubectl delete clusters.postgresql.cnpg.io pg-cluster -n cnpg-test

# both pods and both PVCs must be gone before restoring
kubectl get pods,pvc -n cnpg-test

RESTORE_POINT=$(kubectl get restorepoint -n cnpg-test \
  -o jsonpath='{.items[-1].metadata.name}')

kubectl create -f - <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-
  namespace: cnpg-test
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: ${RESTORE_POINT}
    namespace: cnpg-test
  targetNamespace: cnpg-test
EOF
```

> **`profile` on Kasten 9.x and on 8.x.** On Kasten `9.0.5` no `profile` is needed in the
> `RestoreAction`: Kasten extracts the location profile from the `RestorePointContent` on its own,
> which is how the restore above was validated. On Kasten `8.x` that extraction is unreliable and
> the restore fails **after the volumes have been restored**, with the `restorePosthook` never
> running. On `8.x`, name the profile explicitly:
>
> ```yaml
>   profile:
>     name: <LOCATION_PROFILE>
>     namespace: kasten-io
> ```

Wait for the restore to finish, then confirm the posthook ran:

```bash
kubectl get restoreaction -n cnpg-test
kubectl logs -n kasten-io -l component=kanister --tail=2000 | grep -E 'restorePosthook|waitForClusterReady'
```

The CNPG operator promotes the restored replica PVC to primary, so the primary is now
`pg-cluster-2` and a fresh replica `pg-cluster-3` is built by streaming replication:

```bash
kubectl get clusters.postgresql.cnpg.io -n cnpg-test
# NAME         INSTANCES   READY   STATUS                      PRIMARY
# pg-cluster   2           2       Cluster in healthy state    pg-cluster-2
```

## Validate data after restore


```bash
# The primary pod name changes after restore (CNPG promotes the replica)
PRIMARY=$(kubectl get pods -n cnpg-test \
  -l "cnpg.io/cluster=pg-cluster,cnpg.io/instanceRole=primary" \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n cnpg-test "$PRIMARY" -- \
  psql -U postgres -d kasten_test -c "SELECT * FROM employees ORDER BY id;"
# Expected: 5 rows — Alice, Bob, Carol, David, Eve
```

## Destroy the test workload

```bash
kubectl delete namespace cnpg-test
kubectl delete policy cnpg-backup-policy -n kasten-io

# Clean up restore point contents created by Kasten
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=cnpg-test
```

## Uninstall the operator and blueprint

```bash
helm uninstall cnpg -n cnpg-system
kubectl delete namespace cnpg-system
kubectl delete blueprint cnpg-blueprint -n kasten-io
kubectl delete blueprintbinding cnpg-blueprint-binding -n kasten-io
```

---

## Initial prompt

> Create a blueprint to back up a CNPG instance.
