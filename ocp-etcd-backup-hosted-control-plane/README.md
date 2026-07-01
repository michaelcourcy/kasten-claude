# Hosted Control Plane (HCP) etcd backup with Kasten — permanent keeper PVC

Back up the **etcd of a hosted (guest) OpenShift cluster** — a HyperShift / MCE
*Hosted Control Plane* — with Kasten, writing the etcd snapshot to a **permanent
PVC** that Kasten then snapshots. No object storage client, no cloud identity —
Kasten is the data mover.

> **Sibling blueprint.** This is the hosted-control-plane counterpart of
> [`../ocp-etcd-backup`](../ocp-etcd-backup) (standalone OpenShift etcd). The
> *pattern* is identical (Pattern 4 — permanent keeper PVC), but the etcd is
> reached completely differently:
>
> | | Standalone OCP etcd | **Hosted control plane (this blueprint)** |
> |---|---|---|
> | Where etcd lives | static pods on the **master nodes**, data under host `/etc/kubernetes` | an ordinary **StatefulSet `etcd`** (pods `etcd-0/1/2`, container `etcd`) with PVCs in the control-plane namespace (`clusters-<name>`) **on the management ("hub") cluster** |
> | Backup mechanism | the node script `cluster-backup.sh` over a hostPath | the documented `etcdctl snapshot save` via `kubectl exec` into an etcd pod (`localhost:2379` + the pod's own client certs) |
> | Keeper placement | a control-plane node (needs hostPath) | a plain pod in its own namespace — **no host access, no privileged SCC** |

Procedure source: Red Hat **"Backing up and restoring etcd on a hosted cluster
in an on-premise environment"**
([OCP 4.18 / OKD docs](https://docs.okd.io/4.18/hosted_control_planes/hcp_high_availability/hcp-backup-restore-on-premise.html)).

---

## ⚠️ Read this first — an etcd backup is *not* an OpenShift backup

This holds for hosted clusters too, with an extra twist:

- **It captures no application data.** Only the *guest cluster's* API objects at a
  point in time. The guest's workloads, their PVC data and object storage are **not**
  in it — protect those separately with Kasten **running on (or pointed at) the guest**.
- **Granular restore is impossible.** etcd restore is all-or-nothing: you roll the
  *entire guest control plane* back to the snapshot.
- **It is disruptive.** The documented restore pauses reconciliation, scales the etcd
  StatefulSet to zero, restores into `data-etcd-0`, and rolls out the HostedCluster.
  It is a control-plane disaster operation, not routine recovery.

**Recommended recovery model for a hosted cluster:**

1. **Recreate the `HostedCluster` / `NodePool` declaratively** (GitOps) — that is the
   reproducible way to bring a guest control plane back.
2. **Restore the guest's applications with Kasten** — granular, *with* their PVC data.

**So why this blueprint?** Same reasons as the standalone one: **compliance/legacy
practices** that mandate etcd snapshots, and **forensics** (inspecting a guest's
cluster state at a past point in time). It makes those snapshots safe, scheduled, and
snapshot-managed by Kasten — it is **not** a substitute for GitOps + Kasten
application restore.

---

## Pattern

**Pattern 4 — dump on a permanent Keeper PVC (PVC mounted by keeper).**

A dedicated **keeper Deployment** runs the backup-tool image, mounts the permanent
backup PVC at `/backup`, and lives in its **own namespace** (`clusters-<name>-etcd-backup`).
Kasten's PVC discovery sees the permanent keeper PVC *before* `backupPrehook` runs, so
it is correctly included in the restore point. `backupPrehook` `KubeExec`s into the
keeper; the keeper `kubectl exec`s into a running hosted etcd pod and runs the
documented `etcdctl snapshot save`, then streams the `.db` out onto the keeper PVC.
The blueprint binds to the keeper Deployment via a `BlueprintBinding` (preserves
SRP/OCP).

### Why the keeper is in its *own* namespace, not `clusters-guest1`

This is deliberate and important:

1. **The backup must outlive what it protects.** The documented restore *requires
   destroying/recreating the hosted cluster* — which deletes `clusters-<name>` and
   everything in it. If the keeper PVC (the only copy of `snapshot.db`) lived there,
   the disaster you are recovering from would delete your backup. Its own namespace
   survives a guest teardown.
2. **Kasten policy scope.** A policy snapshots *all* PVCs in its target namespace. In
   `clusters-<name>` that is the live `data-etcd-*` volumes **and** the worker-VM disks
   — you would need fragile `excludeResources` filters. Own namespace → Kasten
   snapshots *only* the keeper PVC.
3. **Mapping is explicit anyway** via the keeper's `CLUSTER_NAME` /
   `HOSTED_CLUSTER_NAMESPACE` env, independent of where the keeper runs. The namespace
   is named `clusters-<name>-etcd-backup` so the link to the guest is obvious.

### Architecture

```
management ("hub") cluster
┌───────────────────────────────────────────────────────────────────────┐
│  namespace: clusters-guest1   (guest1's hosted control plane)          │
│   ┌───────────┐  ┌───────────┐  ┌───────────┐                          │
│   │  etcd-0   │  │  etcd-1   │  │  etcd-2   │  StatefulSet etcd (app=etcd)│
│   │ data-etcd-0│ │ data-etcd-1│ │ data-etcd-2│  (container `etcd`)       │
│   └─────▲─────┘  └───────────┘  └───────────┘                          │
│         │ kubectl exec: etcdctl snapshot save (localhost:2379)          │
│         │            + in-pod certs /etc/etcd/tls/...                   │
│  namespace: clusters-guest1-etcd-backup                                │
│   ┌─────┴────────────────────────┐   image: michaelcourcy/             │
│   │ keeper pod (Deployment)      │          hcp-etcd-backup             │
│   │  - kubectl (exec into etcd)  │                                      │
│   │  - etcdutl (verify)          │── streams snapshot.db ──┐            │
│   └──────────────────────────────┘                         │            │
│                                                            ▼            │
│                                permanent PVC  hcp-etcd-backup           │
└───────────────────────────────────────────────────────────────────────┘
                                       │
              Kasten snapshots this PVC ▼  = restore point
```

- **backupPrehook** (`KubeExec` into keeper): check the HostedCluster is `Available`
  and not `Progressing` and the etcd StatefulSet is fully ready; pick a running etcd
  pod; `etcdctl snapshot save` inside it; stream the `.db` out to `/backup`; verify
  with `etcdutl snapshot status`; `sync`.
- Kasten takes a CSI snapshot of the keeper PVC → restore point.
- **backupPosthook** (`KubeExec` into keeper): re-verify the `snapshot.db` on the PVC.
- **No restore action** and **no delete action** (see below).

---

## Versions

| Component | Version |
|---|---|
| Hub OpenShift | `4.18.6` |
| Hub Kubernetes | `1.31.6` |
| MCE (HyperShift provider) | `2.8.7` |
| Hosted cluster (`guest1`) | OpenShift `4.18.6` |
| Hosted etcd | `3.5.18` |
| Kasten | `8.5.12` (detect with `helm ls -n kasten-io`) |
| Keeper image | `michaelcourcy/hcp-etcd-backup:8.5.12` (UBI9-minimal + `kubectl` 1.31.6 + `etcdctl`/`etcdutl` 3.5.18) |


---

## The keeper image

`michaelcourcy/hcp-etcd-backup:8.5.12` (public). Built from
[`images/hcp-etcd-backup/Dockerfile`](images/hcp-etcd-backup/Dockerfile):

- Base: `registry.access.redhat.com/ubi9/ubi-minimal`
- `kubectl` `v1.31.6` (matched to the **hub** Kubernetes version) — exec into the
  hosted etcd pod and stream the snapshot out.
- `etcdutl` (and `etcdctl`) copied from `quay.io/coreos/etcd:v3.5.18` (matched to the
  guest's etcd version) — `etcdutl` verifies the snapshot on the keeper PVC.
- `bash`, `coreutils`, `util-linux` (`sync`).

`etcdctl snapshot save` itself runs **inside the hosted etcd pod** (which already has
`etcdctl` and the client certs), not in the keeper — the keeper only orchestrates and
verifies.

To rebuild:

```bash
cd images/hcp-etcd-backup
docker build --platform linux/amd64 -t michaelcourcy/hcp-etcd-backup:8.5.12 .
docker push michaelcourcy/hcp-etcd-backup:8.5.12
```

Replace `michaelcourcy` by your own repo and change the image field in the blueprint accordingly.

---

## Prerequisites

### 1. VolumeSnapshotClass annotated for Kasten

Kasten only uses a `VolumeSnapshotClass` carrying `k10.kasten.io/is-snapshot-class: true`.

```bash
# Azure disk (this test cluster):
kubectl annotate volumesnapshotclass csi-azuredisk-vsc k10.kasten.io/is-snapshot-class=true --overwrite
```

> **Storage class**: the manifest uses `managed-csi` (Azure disk CSI) with the
> `csi-azuredisk-vsc` VolumeSnapshotClass — the values for our test cluster.
> Replace them with a CSI storage class that supports snapshots on your cluster
> (`ebs-sc` on AWS, `standard-rwo` on GKE, your custom class on bare-metal, etc.)
> and a matching `VolumeSnapshotClass` registered with Kasten. Do **not** use
> legacy in-tree classes (e.g. `gp2`) — they do not support CSI snapshots.

### 2. RBAC — the keeper reaches into two other namespaces

No host access and **no privileged SCC** are needed (unlike the standalone blueprint).
The keeper's ServiceAccount only needs, via namespaced Roles in
[`keeper.yaml`](keeper.yaml):

- in the **HostedCluster namespace** (`clusters`): `get`/`list` on
  `hostedclusters.hypershift.openshift.io` (stability check).
- in the **control-plane namespace** (`clusters-guest1`): `get`/`list` on `pods` and
  `statefulsets`, and `create` on `pods/exec` (find the etcd pod, check readiness, run
  `etcdctl snapshot save`).

### 3. The keeper targets one hosted cluster via env

`CLUSTER_NAME` and `HOSTED_CLUSTER_NAMESPACE` are set on the keeper container. The
control-plane namespace is derived as `${HOSTED_CLUSTER_NAMESPACE}-${CLUSTER_NAME}`
(e.g. `clusters-guest1`). The blueprint reads these env vars when it `KubeExec`s into
the keeper, so it stays generic: **deploy one keeper (and one keeper namespace) per
hosted cluster** you want to protect, and adjust the RBAC namespaces accordingly.

---

## Step 1 — Deploy the keeper (namespace, RBAC, PVC, Deployment)

[`keeper.yaml`](keeper.yaml) creates namespace `clusters-guest1-etcd-backup`,
ServiceAccount `hcp-etcd-backup-keeper`, the cross-namespace Roles/RoleBindings, the
permanent PVC `hcp-etcd-backup` (10Gi), and the keeper Deployment.

```bash
kubectl apply -f keeper.yaml
kubectl rollout status deployment/hcp-etcd-backup-keeper -n clusters-guest1-etcd-backup
```

> The manifest targets `guest1` in namespace `clusters` (control-plane namespace
> `clusters-guest1`). For a different guest, change the `CLUSTER_NAME` /
> `HOSTED_CLUSTER_NAMESPACE` env, the keeper namespace name, and the RBAC namespaces.

Confirm the keeper can see the guest etcd:

```bash
KP=$(kubectl get pods -n clusters-guest1-etcd-backup -l app=hcp-etcd-backup-keeper -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n clusters-guest1-etcd-backup $KP -- \
  kubectl get pods -n clusters-guest1 -l app=etcd
```

---

## Step 2 — (Optional) validate the backup by hand

Prove the flow works before involving Kasten — this is exactly what `backupPrehook`
automates:

```bash
KP=$(kubectl get pods -n clusters-guest1-etcd-backup -l app=hcp-etcd-backup-keeper -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n clusters-guest1-etcd-backup $KP -- bash -c '
  CPNS="${HOSTED_CLUSTER_NAMESPACE}-${CLUSTER_NAME}"
  ETCD_POD=$(kubectl get pods -n "$CPNS" -l app=etcd --field-selector=status.phase=Running -o jsonpath="{.items[0].metadata.name}")
  mkdir -p /backup/hcp-etcd-backup
  kubectl exec -n "$CPNS" -c etcd "$ETCD_POD" -- env ETCDCTL_API=3 /usr/bin/etcdctl \
    --cacert /etc/etcd/tls/etcd-ca/ca.crt --cert /etc/etcd/tls/client/etcd-client.crt \
    --key /etc/etcd/tls/client/etcd-client.key --endpoints=https://localhost:2379 \
    snapshot save /var/lib/snapshot.db
  kubectl exec -n "$CPNS" -c etcd "$ETCD_POD" -- cat /var/lib/snapshot.db > /backup/hcp-etcd-backup/snapshot.db
  kubectl exec -n "$CPNS" -c etcd "$ETCD_POD" -- rm -f /var/lib/snapshot.db
  ls -lh /backup/hcp-etcd-backup/
  etcdutl snapshot status /backup/hcp-etcd-backup/snapshot.db -w table
  sync'
```

You should get a `snapshot.db` (~40 MB) and a healthy `etcdutl snapshot status` table.

### Copy the artifact to your laptop

The keeper PVC is always browsable:

```bash
KP=$(oc get pods -n clusters-guest1-etcd-backup -l app=hcp-etcd-backup-keeper \
  --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
oc cp clusters-guest1-etcd-backup/$KP:/backup/hcp-etcd-backup/snapshot.db ./snapshot.db
```

> `oc cp` (like `kubectl cp`) needs `tar` in the container — the keeper image has it.

---

## Step 3 — Deploy the blueprint

```bash
kubectl apply -f blueprint.yaml          # Blueprint in kasten-io
kubectl apply -f blueprintbinding.yaml   # binds to Deployments labelled hcp-etcd-backup-keeper=true
```

### Blueprint actions

| Action | Function | What it does |
|---|---|---|
| `backupPrehook` | `KubeExec` (keeper) | Verifies the HostedCluster is `Available` and not `Progressing` and the etcd StatefulSet is fully ready (**aborts otherwise**); picks a running etcd pod; runs the documented `etcdctl snapshot save` inside it; streams the `.db` out to `/backup/hcp-etcd-backup/snapshot.db`; verifies with `etcdutl snapshot status`; runs `sync`. |
| `backupPosthook` | `KubeExec` (keeper) | Verifies a non-empty `snapshot.db` exists and re-runs `etcdutl snapshot status`. |

There is intentionally **no `restore*` action** and **no `delete` action** — see the
notes below.

---

## Step 4 — Back up through Kasten

You need a **Location** profile so Kasten wires up the Kanister hooks (the blueprint
itself never writes to it). Any S3/Azure/GCS Location profile works.

```bash
# Location profile must be type "Location" (not "Infra"), or restore can't extract it:
kubectl get profile <name> -n kasten-io -o jsonpath='{.spec.type}'
```

Create an on-demand policy bound to the keeper namespace and run it:

```bash
kubectl apply -f - <<'EOF'
apiVersion: config.kio.kasten.io/v1alpha1
kind: Policy
metadata:
  name: hcp-etcd-backup-policy
  namespace: kasten-io
spec:
  comment: "Hosted control plane (guest1) etcd backup via keeper blueprint"
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
        values: [clusters-guest1-etcd-backup]
EOF

kubectl create -f - <<'EOF'
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata:
  generateName: run-hcp-etcd-
  namespace: kasten-io
spec:
  subject:
    apiVersion: config.kio.kasten.io/v1alpha1
    kind: Policy
    name: hcp-etcd-backup-policy
    namespace: kasten-io
EOF
```

Watch it and confirm the hooks fired:

```bash
kubectl get runaction -n kasten-io -w
kubectl logs -n kasten-io deploy/kanister-svc --tail=200 | grep -E "hcpEtcd|TOTAL KEYS"
kubectl get restorepoint -n clusters-guest1-etcd-backup
```

---

## Restore

> ### ⚠️ Three different things are called "restore"
>
> 1. **Restoring the *backup artifact* with Kasten.** A Kasten restore of the
>    `clusters-guest1-etcd-backup` namespace brings back the keeper PVC (and
>    Deployment) with `snapshot.db` on it. Fully automated.
> 2. **Validating the artifact non-destructively** (recommended sanity check). Restore
>    `snapshot.db` into a throwaway etcd and read keys out — **does not touch the
>    guest**. This is how the backup quality was validated for this blueprint (below).
> 3. **Restoring the guest's control-plane etcd onto the hub** (manual, disruptive).
>    The official HyperShift procedure. **Never** automated from a Kasten hook.

### 1. Restore the backup artifact with Kasten

```bash
RP=$(kubectl get restorepoint -n clusters-guest1-etcd-backup -o jsonpath='{.items[0].metadata.name}')
kubectl create -f - <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-hcp-etcd-
  namespace: clusters-guest1-etcd-backup
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: $RP
    namespace: clusters-guest1-etcd-backup
  targetNamespace: clusters-guest1-etcd-backup
EOF
```

> No `profile` is set on the `RestoreAction` — Kasten extracts the location profile
> from the `RestorePointContent` automatically.

### 2. Validate backup quality non-destructively ✅ (done for this blueprint)

This proves the snapshot is genuinely restorable **and** contains real guest data,
without touching `guest1`. It is the validation that was run when this blueprint was
developed (a marker ConfigMap `etcd-restore-test/marker` was created on the guest, then
recovered out of the backup):

```bash
NS=clusters-guest1-etcd-backup

# 0. get the snapshot.db (off the keeper PVC, or a Kasten artifact restore)
KP=$(oc get pods -n $NS -l app=hcp-etcd-backup-keeper -o jsonpath='{.items[0].metadata.name}')
oc cp $NS/$KP:/backup/hcp-etcd-backup/snapshot.db /tmp/hcp-snapshot.db

# 1. throwaway pod with the etcd SERVER binary + etcdutl/etcdctl
#    (image: ubi9-minimal + bash + etcd/etcdctl/etcdutl from quay.io/coreos/etcd:v3.5.18)
oc label ns $NS pod-security.kubernetes.io/enforce=privileged --overwrite
oc run etcd-verify -n $NS --image=michaelcourcy/etcd-verify:3.5.18 --restart=Never \
  --command -- /bin/bash -c 'sleep infinity'
oc wait --for=condition=Ready pod/etcd-verify -n $NS --timeout=120s

# 2. stream the snapshot in (stdin avoids needing tar), then restore + start + read
oc exec -i -n $NS etcd-verify -- bash -c 'cat > /tmp/snapshot.db' < /tmp/hcp-snapshot.db
oc exec -n $NS etcd-verify -- bash -c '
  etcdutl snapshot restore /tmp/snapshot.db --data-dir /tmp/restored
  etcd --data-dir /tmp/restored --name default \
    --initial-cluster default=http://localhost:2380 \
    --initial-advertise-peer-urls http://localhost:2380 \
    --listen-peer-urls http://localhost:2380 \
    --listen-client-urls http://localhost:2379 \
    --advertise-client-urls http://localhost:2379 >/tmp/etcd.log 2>&1 &
  sleep 6
  export ETCDCTL_API=3
  echo "marker keys:";  etcdctl get "" --prefix --keys-only | grep -a etcd-restore-test
  echo "marker value:"; etcdctl get --prefix /kubernetes.io/configmaps/etcd-restore-test/ | grep -aoE "HCP-ETCD-MARKER-[0-9]+"'

# 3. cleanup
oc delete pod etcd-verify -n $NS --force --grace-period=0
oc label ns $NS pod-security.kubernetes.io/enforce-
```

Observed result when validating this blueprint: the restored etcd came up healthy with
**3033 keys**, the `etcd-restore-test` namespace and its `marker` ConfigMap were
present, and the marker value `HCP-ETCD-MARKER-20260630` was recovered — confirming the
Kasten-produced backup is restorable and carries live guest data.

> The `michaelcourcy/etcd-verify:3.5.18` image is a tiny throwaway helper
> (ubi9-minimal + `bash` + the `etcd`/`etcdctl`/`etcdutl` binaries from
> `quay.io/coreos/etcd:v3.5.18`). Its Dockerfile lives at
> [`images/etcd-verify/Dockerfile`](images/etcd-verify/Dockerfile). The stock
> `quay.io/coreos/etcd` image has **no shell**, which is why a small wrapper is used.

### 3. Restore the guest control-plane etcd onto the hub (manual, disruptive)

> Before you do this, re-read
> [**"an etcd backup is not an OpenShift backup"**](#️-read-this-first--an-etcd-backup-is-not-an-openshift-backup).
> Prefer recreating the `HostedCluster`/`NodePool` via GitOps and restoring guest
> applications with Kasten.

This blueprint deliberately does **not** automate this. Follow the official procedure
([Red Hat / OKD docs](https://docs.okd.io/4.18/hosted_control_planes/hcp_high_availability/hcp-backup-restore-on-premise.html)),
using the `snapshot.db` now on the keeper PVC. High-level (the doc is authoritative):

```bash
CLUSTER_NAME=guest1
HOSTED_CLUSTER_NAMESPACE=clusters
CPNS=${HOSTED_CLUSTER_NAMESPACE}-${CLUSTER_NAME}

# 1. pause reconciliation
oc patch -n $HOSTED_CLUSTER_NAMESPACE hostedclusters/$CLUSTER_NAME \
  -p '{"spec":{"pausedUntil":"true"}}' --type=merge

# 2. scale etcd down, drop the 2nd/3rd member volumes
oc scale -n $CPNS statefulset/etcd --replicas=0
oc delete -n $CPNS pvc/data-etcd-1 pvc/data-etcd-2

# 3. restore snapshot.db into data-etcd-0 via a data-access pod, then
#    `etcdutl snapshot restore ... --name etcd-0 --initial-cluster etcd-0=...,etcd-1=...,etcd-2=...`
#    (see the doc for the exact data-access Deployment + etcdutl flags)

# 4. scale etcd back up, unpause, roll out the HostedCluster
oc scale -n $CPNS statefulset/etcd --replicas=3
oc patch -n $HOSTED_CLUSTER_NAMESPACE hostedclusters/$CLUSTER_NAME \
  -p '{"spec":{"pausedUntil":null}}' --type=merge
oc annotate hostedcluster -n $HOSTED_CLUSTER_NAMESPACE $CLUSTER_NAME \
  hypershift.openshift.io/restart-date=$(date --iso-8601=seconds) --overwrite
```

> ### ⚠️ Field finding — pausing may not stop the etcd reconcile immediately
> On the test cluster (MCE 2.8.7), the **control-plane-operator kept reconciling the
> etcd "managed strategy"** and scaled the StatefulSet back to 3 shortly after
> `scale --replicas=0`, even with `pausedUntil: "true"`. If `oc scale … --replicas=0`
> does not *hold*, the offline restore in step 3 will be fought. Confirm the etcd pods
> actually stay at 0 (give the pause a moment to propagate to the
> `HostedControlPlane`) before proceeding, and watch `control-plane-operator` logs.
>
> If you ever end up with `data-etcd-1/2` stuck `Terminating` (a pod re-mounted them
> before they deleted), recover **one member at a time** to preserve quorum: clear the
> stuck PVC's finalizer
> (`oc patch pvc data-etcd-N -n $CPNS -p '{"metadata":{"finalizers":null}}' --type=merge`),
> delete its pod, and let the StatefulSet recreate it with a fresh PVC that re-syncs
> from the remaining quorum. Verify `etcdctl endpoint health --cluster` is all-`true`
> before touching the next member.

### `restorePrehook` — intentionally omitted

As in the standalone blueprint, there is **no namespace-level quiesce/init that makes
sense**: the only meaningful etcd "restore" is the manual control-plane procedure
above, which must not run from a hook. Restoring the keeper PVC needs no preparation.

---

## No delete action

Deleting the Kasten restore point deletes the underlying PVC snapshot — the only copy
of the backup. There is no external object-store copy to clean up, so the blueprint has
**no `delete` action**.

---

## Inspecting / navigating backups

The keeper pod is permanent, so you can always browse the current artifact:

```bash
KP=$(kubectl get pods -n clusters-guest1-etcd-backup -l app=hcp-etcd-backup-keeper -o jsonpath='{.items[0].metadata.name}')
kubectl exec -it -n clusters-guest1-etcd-backup $KP -- bash
#   ls -lh /backup/hcp-etcd-backup
#   etcdutl snapshot status /backup/hcp-etcd-backup/snapshot.db -w table
```

---

## Cleanup

```bash
# Kasten objects
kubectl delete policy hcp-etcd-backup-policy -n kasten-io --ignore-not-found
kubectl delete blueprintbinding hcp-etcd-backup-binding -n kasten-io --ignore-not-found
kubectl delete blueprint hcp-etcd-backup -n kasten-io --ignore-not-found

# Restore points and their snapshot content
kubectl delete restorepoint -l k10.kasten.io/appNamespace=clusters-guest1-etcd-backup -n clusters-guest1-etcd-backup
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=clusters-guest1-etcd-backup

# Keeper + permanent PVC (this deletes the backup data!) and its cross-namespace RBAC
kubectl delete -f keeper.yaml

# (optional, on the guest) the marker used for restore validation
oc --kubeconfig <guest-kubeconfig> delete namespace etcd-restore-test --ignore-not-found
```
