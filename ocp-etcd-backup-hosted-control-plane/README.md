# Hosted Control Plane (HCP) fleet etcd backup with Kasten — one keeper, all guests

Back up the **etcd of every hosted (guest) OpenShift cluster** — HyperShift / MCE
*Hosted Control Planes* — with Kasten, from a **single keeper** on the management
("hub") cluster. The keeper writes one etcd snapshot per guest to a permanent PVC that
Kasten then snapshots. No object storage client, no cloud identity — Kasten is the data
mover.

> **Zero-touch onboarding.** One keeper Deployment (+ one PVC) in the `clusters`
> namespace backs up **all** HostedClusters in that namespace. Add a new guest → it is
> protected on the next policy run, with **no extra setup** — no per-guest namespace,
> RBAC, or config.

> **Sibling blueprint.** This is the hosted-control-plane counterpart of
> [`../ocp-etcd-backup`](../ocp-etcd-backup) (standalone OpenShift etcd). The *pattern*
> is identical (Pattern 4 — permanent keeper PVC), but the etcd is reached differently:
>
> | | Standalone OCP etcd | **Hosted control plane (this blueprint)** |
> |---|---|---|
> | Where etcd lives | static pods on the **master nodes**, data under host `/etc/kubernetes` | an ordinary **StatefulSet `etcd`** (pods `etcd-0/1/2`, container `etcd`) with PVCs in each guest's control-plane namespace (`clusters-<name>`) **on the hub** |
> | Backup mechanism | node script `cluster-backup.sh` over a hostPath | documented `etcdctl snapshot save` via `kubectl exec` into an etcd pod (`localhost:2379` + the pod's own client certs) |
> | Scope | one cluster | the **whole fleet** — one keeper loops over all HostedClusters |

Procedure source: Red Hat **"Backing up and restoring etcd on a hosted cluster in an
on-premise environment"**
([OCP 4.18 / OKD docs](https://docs.okd.io/4.18/hosted_control_planes/hcp_high_availability/hcp-backup-restore-on-premise.html)).

---

## ⚠️ Read this first — an etcd backup is *not* an OpenShift backup

This holds for hosted clusters too, with an extra twist:

- **It captures no application data.** Only each *guest cluster's* API objects at a
  point in time. The guests' workloads, their PVC data and object storage are **not**
  in it — protect those separately with Kasten **running on (or pointed at) each guest**.
- **Granular restore is impossible.** etcd restore is all-or-nothing: you roll an
  *entire guest control plane* back to the snapshot.
- **It is disruptive.** The documented restore pauses reconciliation, scales the etcd
  StatefulSet to zero, restores into `data-etcd-0`, and rolls out the HostedCluster.

**Recommended recovery model for a hosted cluster:**

1. **Recreate the `HostedCluster` / `NodePool` declaratively** (GitOps).
2. **Restore the guest's applications with Kasten** — granular, *with* their PVC data.

**So why this blueprint?** Same as the standalone one: **compliance/legacy practices**
that mandate etcd snapshots, and **forensics**. It makes fleet-wide etcd snapshots safe,
scheduled, and snapshot-managed by Kasten — it is **not** a substitute for GitOps +
Kasten application restore.

---

## Pattern & design

**Pattern 4 — dump on a permanent Keeper PVC (PVC mounted by keeper).**

A single **keeper Deployment** runs the backup-tool image, mounts one permanent PVC at
`/backup`, and lives in the HostedCluster namespace (`clusters`). The blueprint binds to
the keeper Deployment; `backupPrehook` `KubeExec`s into the keeper, which:

1. lists all HostedClusters **in its own namespace** (`kubectl get hostedcluster -n $POD_NAMESPACE`),
2. for each guest: checks it is stable, `kubectl exec`s into a running hosted etcd pod,
   runs the documented `etcdctl snapshot save`, and streams the `.db` out to
   `/backup/<hostedcluster-name>/snapshot.db`.

Kasten then snapshots the single keeper PVC — the PVC snapshot **is** the restore point
and holds one `snapshot.db` per guest.

### Why this shape (and how it beats a per-guest keeper)

- **Zero-touch onboarding.** A per-guest keeper means "add a guest → redo all the setup
  (namespace, RBAC, keeper, env)", which is easy to forget. Here the keeper's loop picks
  up any new guest automatically.
- **Binding on the keeper, not the HostedCluster.** A `HostedCluster` CR has **no
  ownership link to the keeper PVC**, so a blueprint bound to the HostedCluster would
  give Kasten nothing to snapshot. Binding to the keeper Deployment (which *owns* its
  PVC) is what makes Kasten capture the backup. The per-guest iteration therefore lives
  in the keeper's `backupPrehook` loop, not in Kasten's binding.
- **`KubeExec`, one pod, sequential writes.** All snapshots are written by the one keeper
  pod, so a single **RWO** PVC is fine — there is no concurrency contention, as long as
  each snapshot path carries the cluster name (`/backup/<name>/`).
- **The keeper lives in `clusters`, and that is safe.** Destroying a *single* guest
  removes its `clusters-<name>` namespace, **not** `clusters` — so the shared keeper PVC
  survives individual guest teardowns. (Only tearing down all of HyperShift removes
  `clusters`; by then you rely on the Kasten restore point.)
- **Bonus DR content.** Because the policy targets `clusters`, the restore point also
  captures the `HostedCluster` / `NodePool` CRs alongside the etcd snapshots.

**Trade-off:** Kasten sees **one** component (the keeper), so per-guest visibility lives
in the keeper logs and the `/backup/<name>/` layout, not the Kasten UI.

### Error policy — best-effort with a failing summary

The `backupPrehook` loop is **best-effort**: it snapshots every reachable guest and logs
a per-guest `OK`/`FAIL`. If **any** guest fails, the action **exits non-zero at the end**,
so:

- the healthy guests' snapshots **still land on the PVC** (you always have something), and
- the failed run **surfaces in the customer's monitoring** (the Kasten policy/action goes
  red), so a broken guest is noticed rather than silently skipped.

A guest is marked `FAIL` (and skipped) if its HostedCluster is not `Available`, is
`Progressing` (rollout in flight), its etcd StatefulSet is not fully ready, no running
etcd pod exists, or `etcdctl`/`etcdutl` errors.

### Stale-directory pruning

Each run prunes `/backup/<name>/` for guests that **no longer exist**, so the PVC mirrors
the live fleet. This is safe: the **previous restore point** still holds a removed guest's
old `snapshot.db`, so you can restore that earlier point anywhere to recover it.

### Architecture

```
management ("hub") cluster
┌──────────────────────────────────────────────────────────────────────────────┐
│  namespace: clusters-guest1        namespace: clusters-guest2   (per-guest CPs)│
│   ┌───────────┐ etcd StatefulSet    ┌───────────┐ etcd StatefulSet             │
│   │ etcd-0/1/2│ (app=etcd)          │ etcd-0/1/2│ (app=etcd)                    │
│   └─────▲─────┘                     └─────▲─────┘                              │
│         │ kubectl exec: etcdctl snapshot save (localhost:2379 + in-pod certs)  │
│         └───────────────┬───────────────────┘                                 │
│  namespace: clusters                                                           │
│   ┌──────────────────────┴───────────────┐  image: michaelcourcy/            │
│   │ ONE keeper pod (Deployment)           │         hcp-etcd-backup           │
│   │  loops `kubectl get hostedcluster -n  │                                    │
│   │        clusters` and snapshots each   │── writes ──┐                       │
│   │  - kubectl (exec) · etcdutl (verify)  │            │                       │
│   └───────────────────────────────────────┘           ▼                       │
│                       permanent PVC  hcp-etcd-backup                           │
│                         /backup/guest1/snapshot.db                             │
│                         /backup/guest2/snapshot.db  ...                        │
└──────────────────────────────────────────────────────────────────────────────┘
                                       │
              Kasten snapshots this PVC ▼  = one restore point, all guests
```

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

> How the guest cluster (`guest1`) was created on the hub is documented in
> `enterprise-blueprint/cp4d/install/openshift/MCE.md` (MCE + OpenShift Virtualization /
> `kubevirt` provider; control plane as pods + worker VMs, all on the hub).

---

## The keeper image

`michaelcourcy/hcp-etcd-backup:8.5.12` (public). Built from
[`images/hcp-etcd-backup/Dockerfile`](images/hcp-etcd-backup/Dockerfile):

- Base: `registry.access.redhat.com/ubi9/ubi-minimal`
- `kubectl` `v1.31.6` (matched to the **hub** Kubernetes version) — list HostedClusters,
  exec into each hosted etcd pod, stream the snapshot out.
- `etcdutl` (and `etcdctl`) from `quay.io/coreos/etcd:v3.5.18` (matched to the guests'
  etcd version) — `etcdutl` verifies each snapshot on the keeper PVC.
- `bash`, `coreutils`, `util-linux` (`sync`).

`etcdctl snapshot save` runs **inside each hosted etcd pod** (which already has `etcdctl`
and the client certs), not in the keeper — the keeper only orchestrates and verifies.

To rebuild:

```bash
cd images/hcp-etcd-backup
docker build --platform linux/amd64 -t michaelcourcy/hcp-etcd-backup:8.5.12 .
docker push michaelcourcy/hcp-etcd-backup:8.5.12
```

---

## Prerequisites

### 1. VolumeSnapshotClass annotated for Kasten

```bash
# Azure disk (this test cluster):
kubectl annotate volumesnapshotclass csi-azuredisk-vsc k10.kasten.io/is-snapshot-class=true --overwrite
```

> **Storage class**: the manifest uses `managed-csi` (Azure disk CSI) with the
> `csi-azuredisk-vsc` VolumeSnapshotClass — the values for our test cluster. Replace them
> with a CSI storage class that supports snapshots on your cluster (`ebs-sc` on AWS,
> `standard-rwo` on GKE, your custom class on bare-metal, etc.) and a matching
> `VolumeSnapshotClass` registered with Kasten. Do **not** use legacy in-tree classes
> (e.g. `gp2`) — they do not support CSI snapshots.

### 2. RBAC — cluster-scoped, because guests are dynamic

The keeper's ServiceAccount (`hcp-etcd-backup-keeper` in `clusters`) needs a **ClusterRole**
(not a namespaced Role), because the control planes it snapshots live in the *dynamic*
per-guest namespaces `clusters-<name>` that come and go with the fleet:

- `get`/`list` on `hostedclusters.hypershift.openshift.io` (list the fleet + stability check),
- `get`/`list` on `pods` and `statefulsets`, and `create` on `pods/exec` (find a running
  etcd pod, check readiness, run `etcdctl snapshot save`).

This is a modest, expected escalation for a fleet-wide backup agent. No host access and no
privileged SCC are needed. It is all in [`keeper.yaml`](keeper.yaml).

---

## Step 1 — Deploy the fleet keeper (once)

[`keeper.yaml`](keeper.yaml) creates the ServiceAccount, ClusterRole/ClusterRoleBinding,
the permanent PVC `hcp-etcd-backup` (20Gi) and the keeper Deployment — all in the
`clusters` namespace. It does **not** create the `clusters` namespace (HyperShift/MCE owns it).

```bash
kubectl apply -f keeper.yaml
kubectl rollout status deployment/hcp-etcd-backup-keeper -n clusters
```

> For a HyperShift install that hosts clusters in a different namespace, deploy the keeper
> into that namespace instead (change the four `namespace:` fields and the
> ClusterRoleBinding subject). One keeper per HostedCluster namespace.

Confirm the keeper can see the fleet:

```bash
KP=$(kubectl get pods -n clusters -l app=hcp-etcd-backup-keeper -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n clusters $KP -- kubectl get hostedcluster -n clusters
```

---

## Step 2 — (Optional) validate the loop by hand

Prove the flow works before involving Kasten — this is what `backupPrehook` automates:

```bash
KP=$(kubectl get pods -n clusters -l app=hcp-etcd-backup-keeper -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n clusters $KP -- bash -c '
  HCNS="${POD_NAMESPACE}"
  for hc in $(kubectl get hostedcluster -n "$HCNS" -o jsonpath="{range .items[*]}{.metadata.name}{\"\n\"}{end}"); do
    cpns="${HCNS}-${hc}"
    pod=$(kubectl get pods -n "$cpns" -l app=etcd --field-selector=status.phase=Running -o jsonpath="{.items[0].metadata.name}")
    mkdir -p /backup/$hc
    kubectl exec -n "$cpns" -c etcd "$pod" -- env ETCDCTL_API=3 /usr/bin/etcdctl \
      --cacert /etc/etcd/tls/etcd-ca/ca.crt --cert /etc/etcd/tls/client/etcd-client.crt \
      --key /etc/etcd/tls/client/etcd-client.key --endpoints=https://localhost:2379 \
      snapshot save /var/lib/snapshot.db
    kubectl exec -n "$cpns" -c etcd "$pod" -- cat /var/lib/snapshot.db > /backup/$hc/snapshot.db
    kubectl exec -n "$cpns" -c etcd "$pod" -- rm -f /var/lib/snapshot.db
    echo "[$hc]:"; etcdutl snapshot status /backup/$hc/snapshot.db -w table
  done
  sync; ls -lhR /backup'
```

---

## Step 3 — Deploy the blueprint

```bash
kubectl apply -f blueprint.yaml          # Blueprint in kasten-io
kubectl apply -f blueprintbinding.yaml   # binds to Deployments labelled hcp-etcd-backup-keeper=true
```

### Blueprint actions

| Action | Function | What it does |
|---|---|---|
| `backupPrehook` | `KubeExec` (keeper) | Loops over all HostedClusters in the keeper's namespace. Per guest: checks `Available`/not `Progressing`/etcd ready (**skips + marks FAIL otherwise**); runs `etcdctl snapshot save` inside a running etcd pod; streams the `.db` to `/backup/<name>/snapshot.db`; verifies with `etcdutl`. Prunes stale dirs; `sync`. **Best-effort: exits non-zero if any guest failed** (healthy guests still saved). |
| `backupPosthook` | `KubeExec` (keeper) | Iterates `/backup/*/snapshot.db` and re-verifies each with `etcdutl snapshot status`; fails if none exist or any is invalid. |

There is intentionally **no `restore*` action** and **no `delete` action** — see below.

---

## Step 4 — Back up the fleet through Kasten

You need a **Location** profile so Kasten wires up the Kanister hooks (the blueprint never
writes to it). Any S3/Azure/GCS Location profile works.

```bash
# Location profile must be type "Location" (not "Infra"), or restore can't extract it:
kubectl get profile <name> -n kasten-io -o jsonpath='{.spec.type}'
```

Create an on-demand policy bound to the `clusters` namespace and run it:

```bash
kubectl apply -f - <<'EOF'
apiVersion: config.kio.kasten.io/v1alpha1
kind: Policy
metadata:
  name: hcp-etcd-backup-policy
  namespace: kasten-io
spec:
  comment: "Hosted control plane fleet etcd backup via keeper blueprint"
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
        values: [clusters]
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

Watch it and confirm the loop fired for every guest:

```bash
kubectl get runaction -n kasten-io -w
kubectl logs -n kasten-io deploy/kanister-svc --tail=400 | grep -E "hcpFleetEtcd|Backup summary|OK:|FAIL:"
kubectl get restorepoint -n clusters
```

---

## Restore

> ### ⚠️ Three different things are called "restore"
>
> 1. **Restoring the *backup artifact* with Kasten.** A Kasten restore of the `clusters`
>    namespace brings back the keeper PVC with every guest's `snapshot.db` on it (plus the
>    `HostedCluster`/`NodePool` CRs). Fully automated.
> 2. **Validating the artifact non-destructively** (recommended sanity check). Restore a
>    guest's `snapshot.db` into a throwaway etcd and read keys out — **does not touch any
>    guest**. This is how backup quality was validated for this blueprint (below).
> 3. **Restoring a guest's control-plane etcd onto the hub** (manual, disruptive). The
>    official HyperShift procedure, per guest. **Never** automated from a Kasten hook.

### 1. Restore the backup artifact with Kasten

```bash
RP=$(kubectl get restorepoint -n clusters -o jsonpath='{.items[0].metadata.name}')
kubectl create -f - <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-hcp-etcd-
  namespace: clusters
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: $RP
    namespace: clusters
  targetNamespace: clusters
EOF
```

> No `profile` is set on the `RestoreAction` — Kasten extracts the location profile from
> the `RestorePointContent` automatically.

### 2. Validate backup quality non-destructively ✅ (done for this blueprint)

This proves a snapshot is genuinely restorable **and** contains real guest data, without
touching the guest. It is the validation run when this blueprint was developed (a marker
ConfigMap `etcd-restore-test/marker` on `guest1` was recovered out of the backup):

```bash
NS=clusters
GUEST=guest1

# 0. get the guest's snapshot.db off the keeper PVC (or a Kasten artifact restore)
KP=$(oc get pods -n $NS -l app=hcp-etcd-backup-keeper -o jsonpath='{.items[0].metadata.name}')
oc cp $NS/$KP:/backup/$GUEST/snapshot.db /tmp/hcp-snapshot.db

# 1. throwaway pod with the etcd SERVER binary + etcdutl/etcdctl
#    (image built from images/etcd-verify/Dockerfile)
oc run etcd-verify -n $NS --image=michaelcourcy/etcd-verify:3.5.18 --restart=Never \
  --overrides='{"spec":{"securityContext":{"runAsNonRoot":true,"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"etcd-verify","image":"michaelcourcy/etcd-verify:3.5.18","command":["/bin/bash","-c","sleep infinity"],"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}}]}}'
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
```

Observed when validating this blueprint: the restored etcd came up healthy with ~3033
keys, the `etcd-restore-test` namespace and its `marker` ConfigMap were present, and the
value `HCP-ETCD-MARKER-20260630` was recovered — confirming the Kasten-produced backup is
restorable and carries live guest data.

> The `michaelcourcy/etcd-verify:3.5.18` image is a tiny throwaway helper (ubi9-minimal +
> `bash` + the `etcd`/`etcdctl`/`etcdutl` binaries from `quay.io/coreos/etcd:v3.5.18`), in
> [`images/etcd-verify/Dockerfile`](images/etcd-verify/Dockerfile). The stock
> `quay.io/coreos/etcd` image has **no shell**, hence the wrapper.

### 3. Restore a guest control-plane etcd onto the hub (manual, disruptive)

> Before you do this, re-read
> [**"an etcd backup is not an OpenShift backup"**](#️-read-this-first--an-etcd-backup-is-not-an-openshift-backup).
> Prefer recreating the `HostedCluster`/`NodePool` via GitOps and restoring guest apps
> with Kasten.

This blueprint deliberately does **not** automate this. Follow the official procedure
([Red Hat / OKD docs](https://docs.okd.io/4.18/hosted_control_planes/hcp_high_availability/hcp-backup-restore-on-premise.html)),
using the guest's `snapshot.db` from `/backup/<name>/` on the keeper PVC:

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
> On the test cluster (MCE 2.8.7), the **control-plane-operator kept reconciling the etcd
> "managed strategy"** and scaled the StatefulSet back to 3 shortly after
> `scale --replicas=0`, even with `pausedUntil: "true"`. If `oc scale … --replicas=0` does
> not *hold*, the offline restore in step 3 will be fought. Confirm the etcd pods actually
> stay at 0 (give the pause a moment to propagate to the `HostedControlPlane`) before
> proceeding, and watch `control-plane-operator` logs.
>
> If you ever end up with `data-etcd-1/2` stuck `Terminating` (a pod re-mounted them
> before they deleted), recover **one member at a time** to preserve quorum: clear the
> stuck PVC's finalizer
> (`oc patch pvc data-etcd-N -n $CPNS -p '{"metadata":{"finalizers":null}}' --type=merge`),
> delete its pod, and let the StatefulSet recreate it with a fresh PVC that re-syncs from
> the remaining quorum. Verify `etcdctl endpoint health --cluster` is all-`true` before
> touching the next member.

### `restorePrehook` — intentionally omitted

There is **no namespace-level quiesce/init that makes sense**: the only meaningful etcd
"restore" is the manual per-guest procedure above, which must not run from a hook.
Restoring the keeper PVC needs no preparation.

---

## No delete action

Deleting the Kasten restore point deletes the underlying PVC snapshot — the only copy of
the backups. There is no external object-store copy to clean up, so the blueprint has
**no `delete` action**.

---

## Inspecting / navigating backups

The keeper pod is permanent, so you can always browse the current fleet artifacts:

```bash
KP=$(kubectl get pods -n clusters -l app=hcp-etcd-backup-keeper -o jsonpath='{.items[0].metadata.name}')
kubectl exec -it -n clusters $KP -- bash
#   ls -lhR /backup
#   etcdutl snapshot status /backup/<name>/snapshot.db -w table
```

---

## Cleanup

```bash
# Kasten objects
kubectl delete policy hcp-etcd-backup-policy -n kasten-io --ignore-not-found
kubectl delete blueprintbinding hcp-etcd-backup-binding -n kasten-io --ignore-not-found
kubectl delete blueprint hcp-etcd-backup -n kasten-io --ignore-not-found

# Restore points and their snapshot content
kubectl delete restorepoint -l k10.kasten.io/appNamespace=clusters -n clusters
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=clusters

# Keeper + permanent PVC (this deletes the backup data!) and its cluster-scoped RBAC
kubectl delete -f keeper.yaml

# (optional, on the guest) the marker used for restore validation
oc --kubeconfig <guest-kubeconfig> delete namespace etcd-restore-test --ignore-not-found
```
