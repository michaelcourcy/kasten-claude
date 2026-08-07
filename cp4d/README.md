# CP4D artifact-level backup with Kasten

Back up **Cloud Pak for Data analytics projects** (notebooks, jobs, data assets, connections)
at the **logical / artifact level** using `cpdctl` project export/import, with **Kasten as the
data mover**. Each CP4D project is exported into its **own PVC**; Kasten snapshots those PVCs.
Restore is **granular and multi-tenant**: pick the project(s) you want, restore only those PVCs
into a namespace, and recreate the projects with a script.

> This is a **complement** to CP4D's platform-level backup (`cpdbr`), not a replacement. `cpdbr`
> covers whole-tenant infrastructure DR; this blueprint covers the lane it does not enter —
> per-project restore, portability between clusters, and self-service recovery. It needs the CP4D
> platform to be up; it does not protect the control plane.
>
> The reasoning behind this design — why the API is the only path to a notebook, what the export
> bundle actually contains, and the credential/data boundaries — is in **[DESIGN.md](DESIGN.md)**.
> Read that for the "why"; this README is the "how".

---

## Architecture

```
                          cp4d namespace                      cp4d-projects-backup namespace
                     ┌──────────────────────-┐            ┌──────────────────────────────────────┐
   Kasten policy     │  CP4D / Watson Studio │            │  1 PVC per project (permanent, GUID- │
   (backup ns) ─────▶│  projects + cpdctl API│            │  keyed): cp4d-<name>-<guid>          │
        │            │                       │            │   ├─ cp4d-my-new-project-21a0ff72    │
        │ preHook    │  cred secret          │            │   ├─ cp4d-sales-3f9c1a2b             │
        ▼            │  cp4d-backup-cpdctl-  │            │   └─ ...                             │
  backupPrehook      │  creds (OUTSIDE the   │            └──────────────────────────────────────┘
  (action hook)      │  backed-up namespace) │                         ▲   Kasten snapshots these PVCs
        │            └──────────┬────────────┘                         │
        │ runs orchestrate.sh              reads cred secret           │ one export pod per project
        │ (KubeTask in kasten-io,          cross-namespace (RBAC)      │ (keeper SA), writes the
        │  cluster-wide Kasten SA)  ───────────────────────────────────┘ UNZIPPED cpdctl export to /backup/current
        ▼
  enumerate projects → ensure PVC per project → launch export pods → wait → prune → cleanup pods
```

**Pattern:** hybrid of *Pattern 4 (permanent keeper PVC)* + the *action-hook mechanism*. There is
no `Project`/`Notebook` CR to bind to, so this is an **action blueprint** (a BackupAction preHook),
not a workload blueprint. The preHook runs **before Kasten's PVC discovery**, so every per-project
PVC it creates is included in the restore point. One PVC per project is the whole point: **PVC =
unit of granularity = unit of multi-tenant restore**.

**Key decisions** (see [DESIGN.md § 3](DESIGN.md#3-how-it-maps-onto-this-projects-blueprint-patterns) for the reasoning):
- **Permanent, GUID-keyed PVCs** (`cp4d-<sanitized-name>-<short-guid>`), created-if-missing and
  refreshed every run, pruned when a project is deleted. Permanence lets CSI block-level
  incremental dedup work across backups.
- **Store the export UNZIPPED** (not the zip) so a one-cell change only rewrites one file and the
  ~70% constant schema mass dedups across snapshots.
- **Restore is decoupled from Kasten restore hooks** — a standalone [restore.sh](restore.sh) does
  the `cpdctl import`. Robust, and it fits the multi-tenant / mass-migration story.
- **Credentials never fan out**: the CP4D API key lives in a Secret in the `cpd` namespace (NOT
  the backed-up namespace), read cross-namespace by the keeper SA, and rendered into an ephemeral
  `emptyDir`/tmpfs cpdctl config — so it is never captured in any restore point.

---

## Versions

Detected on the cluster where this blueprint was developed and tested.

| Component | Version |
|---|---|
| Kubernetes | 1.31.6 |
| OpenShift | 4.18.6 |
| Kasten | 8.5.12 (Helm `k10-8.5.12`) |
| Cloud Pak for Data / IBM Software Hub | 5.2.x (Watson Studio `ws` 11.2.0) |
| cpdctl | 1.8.244 |
| Keeper image | `docker.io/michaelcourcy/cp4d-backup:1.8.244-2` |

Detect your Kasten version:

```bash
helm ls -n kasten-io                 # Helm
# or (OpenShift OLM): kubectl get csv -n kasten-io -o jsonpath='{.items[?(@.spec.displayName=="Kasten K10")].spec.version}'
```

---

## Repository layout

| Path | What |
|---|---|
| [blueprint.yaml](blueprint.yaml) | The action blueprint (`backupPrehook`) |
| [restore.sh](restore.sh) | Host-run restore: recreate a project from a restored PVC |
| [images/cp4d-backup/Dockerfile](images/cp4d-backup/Dockerfile) | Keeper image (cpdctl + kubectl + jq + unzip) |
| [images/cp4d-backup/orchestrate.sh](images/cp4d-backup/orchestrate.sh) | Backup orchestrator (enumerate → export pods → prune) |
| [images/cp4d-backup/export-project.sh](images/cp4d-backup/export-project.sh) | Per-project export → unzip atomically to PVC |
| [images/cp4d-backup/import-project.sh](images/cp4d-backup/import-project.sh) | Import a bundle from a restored PVC |
| [images/cp4d-backup/cpdctl-login.sh](images/cp4d-backup/cpdctl-login.sh) | Headless cpdctl auth (env creds or cross-ns secret) |
| [DESIGN.md](DESIGN.md) | The rationale: why the API path, pattern mapping, what export captures |
| [DESIGN.pptx](DESIGN.pptx) | The same rationale as a deck — one slide per DESIGN.md section, full section text in the speaker notes (open with PowerPoint; Keynote refuses the import) |
| [tools/build-design-deck.py](tools/build-design-deck.py) | Regenerates `DESIGN.pptx` from `DESIGN.md` (`pip install python-pptx`) |

---

## Prerequisites

### 1. Build the keeper image

The blueprint and restore both use one self-contained image (Dockerfile committed at
[images/cp4d-backup/](images/cp4d-backup/)): Debian slim + `cpdctl` + `kubectl` + `jq` + `unzip`,
with the four orchestration scripts baked in. Runs as an arbitrary (OpenShift-assigned) UID; the
cpdctl config is rendered to a tmpfs path at runtime and never persisted.

```bash
cd images/cp4d-backup
docker buildx build --platform linux/amd64 -t <your-registry>/cp4d-backup:1.8.244-2 --push .
```

> Use an **immutable tag** (or `imagePullPolicy: Always`, which the pod specs already set). A reused
> mutable tag can leave stale layers cached on some nodes → `executable ... not found` errors.
> Update the image reference in [blueprint.yaml](blueprint.yaml) and [restore.sh](restore.sh).

### 2. CP4D credentials — a service-user API key in the `cpd` namespace

Use a **dedicated CP4D service user** (least privilege — access only to the projects to back up),
not the admin. Generate its **platform API key** and store it in a Secret **in `cpd`** (deliberately
*outside* the backed-up namespace, so it is never captured in a restore point):

```bash
# Get the user's platform API key from CP4D (run as that user's bearer token):
#   TOKEN via POST /icp4d-api/v1/authorize {username,password}
#   GET /usermgmt/v1/user/apiKey  ->  {"apiKey":"..."}

kubectl create secret generic cp4d-backup-cpdctl-creds -n cpd \
  --from-literal=url=https://<cpd-route-host> \
  --from-literal=username=<service-user> \
  --from-literal=apikey=<api-key>
```

Find the CP4D route host: `oc get route cpd -n cpd -o jsonpath='https://{.spec.host}{"\n"}'`.

### 3. Namespace, keeper ServiceAccount, and scoped RBAC

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: cp4d-projects-backup
  labels:
    pod-security.kubernetes.io/enforce: privileged   # export pods run as arbitrary UID; tighten in prod
---
apiVersion: v1
kind: ServiceAccount
metadata: { name: cp4d-backup-keeper, namespace: cp4d-projects-backup }
---
# cpd: keeper SA may GET only the one credential secret (read cross-namespace, never mounted)
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: cp4d-backup-read-creds, namespace: cpd }
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["cp4d-backup-cpdctl-creds"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: cp4d-backup-read-creds, namespace: cpd }
subjects: [ { kind: ServiceAccount, name: cp4d-backup-keeper, namespace: cp4d-projects-backup } ]
roleRef: { kind: Role, name: cp4d-backup-read-creds, apiGroup: rbac.authorization.k8s.io }
---
# backup ns: keeper SA manages its own export pods + PVCs
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: cp4d-backup-keeper, namespace: cp4d-projects-backup }
rules:
  - apiGroups: [""]
    resources: ["pods","pods/log","persistentvolumeclaims"]
    verbs: ["get","list","watch","create","delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: cp4d-backup-keeper, namespace: cp4d-projects-backup }
subjects: [ { kind: ServiceAccount, name: cp4d-backup-keeper, namespace: cp4d-projects-backup } ]
roleRef: { kind: Role, name: cp4d-backup-keeper, apiGroup: rbac.authorization.k8s.io }
EOF
```

> **Storage class**: the blueprint defaults to `managed-csi` (the Azure OpenShift CSI class used
> here). Replace it (in [blueprint.yaml](blueprint.yaml), `STORAGE_CLASS`) with a class that
> supports **CSI snapshots** on your cluster (e.g. `ebs-sc` on AWS, `standard-rwo` on GKE) and has
> a matching `VolumeSnapshotClass` registered with Kasten. Do **not** use legacy in-tree classes.

### 4. A Location profile

The Kanister action hook needs a **Location** profile (S3/GCS/Azure Blob — not Infra):

```bash
kubectl get profile <name> -n kasten-io -o jsonpath='{.spec.type}'   # must print: Location
```

---

## Deploy

```bash
# 1. blueprint (kasten-io)
kubectl apply -f blueprint.yaml

# 2. policy targeting the cp4d-projects-backup namespace, wired to the action hook
kubectl apply -f - <<'EOF'
apiVersion: config.kio.kasten.io/v1alpha1
kind: Policy
metadata:
  name: cp4d-projects-backup-policy
  namespace: kasten-io
spec:
  comment: "CP4D per-project artifact backup"
  frequency: "@daily"                 # or @onDemand
  actions:
    - action: backup
      backupParameters:
        profile: { name: <your-location-profile>, namespace: kasten-io }
        hooks:
          preHook:
            blueprint: cp4d-projects-backup-bp
            actionName: backupPrehook
  selector:
    matchExpressions:
      - key: k10.kasten.io/appNamespace
        operator: In
        values: [ cp4d-projects-backup ]
EOF
```

The hook attaches at `spec.actions[].backupParameters.hooks.preHook` = `{blueprint, actionName}`.

---

## Blueprint actions

| Action | Trigger | What it does |
|---|---|---|
| `backupPrehook` | Before Kasten's PVC discovery/snapshot | Runs `orchestrate.sh`: authenticates cpdctl (cross-ns secret), enumerates all CP4D analytics projects, ensures one permanent GUID-keyed PVC per project, launches one export pod per project (bounded concurrency, one retry, atomic unzip to `/backup/current`), prunes PVCs of deleted projects, deletes export pods so nothing sensitive is captured. Exits non-zero (fails the whole backup) if any project fails to export. |

> There is **no restore action in the blueprint**. Restore is deliberately handled by the
> standalone [restore.sh](restore.sh), not a Kasten restore hook — more robust and a better fit for
> the multi-tenant / mass-migration story. (This also sidesteps the `restorePrehook`-not-triggered
> limitation of resource-bound blueprints in Kasten ≤ 8.5.x.)

---

## Restore

Restore is two steps: (1) Kasten restores the **selected** PVC(s) into a namespace of your choice;
(2) [restore.sh](restore.sh) recreates the CP4D project from each restored PVC. Different users can
restore different projects into different namespaces — the multi-tenant story.

### Step 1 — restore only the PVC(s) you want (Kasten, cross-namespace)

```bash
# list restore points
kubectl get restorepoint -n cp4d-projects-backup
RPC=cp4d-projects-backup-<restorepoint-name>     # the cluster-scoped RestorePointContent name

# create the target namespace (each tenant gets their own)
kubectl create ns john-cp4d-restore
kubectl label ns john-cp4d-restore pod-security.kubernetes.io/enforce=privileged --overwrite

# restore ONLY the chosen project's PVC into that namespace
kubectl create -f - <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RestoreAction
metadata:
  generateName: restore-cp4d-
  namespace: john-cp4d-restore          # action ns MUST equal targetNamespace
spec:
  subject:
    apiVersion: apps.kio.kasten.io/v1alpha1
    kind: RestorePoint
    name: <restorepoint-name>
    namespace: cp4d-projects-backup      # the source restore point
  targetNamespace: john-cp4d-restore
  filters:
    includeResources:
      - resource: persistentvolumeclaims
        name: cp4d-<project>-<guid>      # select exactly the project(s) you want
EOF
```

### Step 2 — recreate the CP4D project from the restored PVC

```bash
./restore.sh <namespace> <pvc> [project-name]

# example:
./restore.sh john-cp4d-restore cp4d-my-new-project-21a0ff72 "my project (restored)"
```

- If `[project-name]` is omitted, the original name stored in the bundle is used.
- If a project with the target name **already exists**, the matching project(s) are listed and you
  must **type the exact name to confirm** delete-and-replace (never silent).
- Requirements on the machine running it: `kubectl` + `cpdctl` (default `./bin/cpdctl` — see
  [Get `cpdctl` on your workstation](#1-get-cpdctl-on-your-workstation); or point `CPDCTL` at your
  own binary). It reads the CP4D credentials straight from the `cpd` Secret, so no profile setup is
  needed.

### Guard rail (recommended)

Forbid restoring INTO the backup namespace (which would clobber the live backup PVCs). Example with
Kyverno — deny any `RestoreAction`/PVC creation with `targetNamespace: cp4d-projects-backup` from
non-admins. (V1 is single-admin; add this before multi-tenant.)

---

## Create the test fixture (test data)

The CP4D platform itself is assumed already installed. This section creates a **minimum
representative dataset** — one analytics project containing a notebook, a job, a **file** data asset
and a **connected** data asset through a real connection. Those last two are the interesting pair:
they behave **oppositely** on export (bytes travel vs reference only, see
[DESIGN.md § 6](DESIGN.md#6-the-data-and-credential-boundary-validated-2026-07-10)), so a restore
that recovers both proves the boundary is understood. Everything here is well under 5 KB.

### 1. Get `cpdctl` on your workstation

`cpdctl` is **not vendored in this repo** (`cp4d/bin/` is git-ignored). Download the release for your
OS/arch from [github.com/IBM/cpdctl/releases](https://github.com/IBM/cpdctl/releases) — use the
**latest stable** version, which is backward compatible with all supported CP4D releases (see
[DESIGN.md § 7](DESIGN.md#7-why-latest-cpdctl-is-the-right-version-policy)):

```bash
mkdir -p bin && cd bin
# with the GitHub CLI — asks for the latest stable tag rather than hardcoding one:
TAG=$(gh release view --repo IBM/cpdctl --json tagName -q .tagName)     # e.g. v1.8.244
gh release download "$TAG" --repo IBM/cpdctl --pattern 'cpdctl_darwin_arm64.tar.gz'
tar xzf cpdctl_darwin_arm64.tar.gz && rm cpdctl_darwin_arm64.tar.gz
xattr -d com.apple.quarantine cpdctl 2>/dev/null                        # macOS Gatekeeper
./cpdctl version
cd ..
```

> Pick the asset matching your platform (`uname -m`); the example is macOS Apple Silicon. Without
> `gh`, grab the same asset from the releases page by hand.

### 2. Point `cpdctl` at the cluster

[restore.sh](restore.sh) configures its own throwaway profile from the credential Secret, so this
step is only needed for the fixture and verification commands below.

```bash
# CP4D web URL and the bootstrap admin credentials
oc get route cpd -n cpd -o jsonpath='https://{.spec.host}{"\n"}'
oc get secret platform-auth-idp-credentials -n cpd -o jsonpath='{.data.admin_username}' | base64 -d; echo
oc get secret platform-auth-idp-credentials -n cpd -o jsonpath='{.data.admin_password}' | base64 -d; echo

# keep the config file next to the binary and OUT of git (cp4d/bin/ is ignored)
export CPDCONFIG=$PWD/bin/.cpdctl.config.json
./bin/cpdctl config user set admin --username <admin-username> --password '<ADMIN_PASSWORD>'
./bin/cpdctl config profile set cp4d --url https://<cpd-route-host> --user admin
./bin/cpdctl config profile use cp4d
./bin/cpdctl project list                       # verify connectivity
```

> The config file holds the CP4D password — **never commit it**. `cp4d/.gitignore` covers both
> `bin/` and `.cpdctl.config.json` anywhere in the tree. Admin credentials are fine for creating a
> fixture; the *blueprint* authenticates with a service-user API key instead (Prerequisite 2).

### 3. Create the project and its assets

```bash
# the analytics project (COS-backed 'assetfiles' storage — no external object store needed)
./bin/cpdctl project create --name cp4d-fixture --type cpd --storage-type assetfiles
PID=<project id printed in the Location header>
```

Add a notebook and a job through the CP4D web UI (Watson Studio): a two-cell Python notebook, then
a job that runs it. Then the two data assets:

```bash
# (a) a 3-row CSV on the in-cluster MinIO, used as the external datasource
kubectl run mc-fixture -n minio --image=minio/mc:latest --restart=Never --rm -i --command -- sh -c '
  mc alias set local http://minio.minio.svc:9000 minio minio123
  mc mb -p local/cp4d-fixture
  printf "id,name,value\n1,alpha,100\n2,beta,200\n3,gamma,300\n" > /tmp/sample.csv
  mc cp /tmp/sample.csv local/cp4d-fixture/sample.csv'

# (b) a FILE data asset — bytes uploaded INTO the project, so they WILL be in the bundle
printf 'id,name,value\n1,alpha,100\n2,beta,200\n3,gamma,300\n' > sample.csv
./bin/cpdctl asset data-asset upload --project-id "$PID" \
  --file sample.csv --name sample.csv --mime text/csv

# (c) a connection to MinIO — this is what carries credentials
./bin/cpdctl connection create --project-id "$PID" \
  --name minio-fixture-conn --datasource-type generics3 --test=false \
  --properties '{"bucket":"cp4d-fixture","url":"http://minio.minio.svc.cluster.local:9000","access_key":"minio","secret_key":"minio123"}'

# (d) a CONNECTED data asset — a reference through that connection, so the bytes stay on MinIO
CONN=<connection id from (c)>
./bin/cpdctl asset data-asset create --project-id "$PID" \
  --metadata-name sample-connected.csv --metadata-asset-type data_asset \
  --metadata-asset-category USER --metadata-origin-country us \
  --entity '{"data_asset":{"mime_type":"text/csv","dataset":false}}' \
  --attachments '[{"asset_type":"data_asset","connection_id":"'"$CONN"'","connection_path":"cp4d-fixture/sample.csv","name":"minio sample.csv","mime":"text/csv"}]'
```

> **Three gotchas, all of which cost time to find:**
> - `--assets` expects a **JSON** value, not the keyword `all`. To export everything use the
>   decomposed boolean **`--assets-all-assets`**.
> - `connection create` **runs a live connect-test by default** and refuses to save if it fails.
>   `--test=false` saves the connection anyway. Needed here: the CP4D S3 connector uses
>   **virtual-hosted** addressing (`bucket.host`), which MinIO's path-style endpoint does not
>   satisfy → a tested connect fails with `UnknownHostException`. Irrelevant for backup-fidelity
>   testing, where what matters is what export captures, not runtime queries.
> - `data-asset create` requires both `--metadata-asset-category` and `--metadata-origin-country`.
>
> The MinIO instance is the throwaway one from
> [kasten-s3troubleshooting](https://github.com/michaelcourcy/kasten-s3troubleshooting?tab=readme-ov-file#test-with-a-minio-instance)
> (`minio`/`minio123` — test fixture credentials, not secrets).

---

## Test it end-to-end

```bash
# 1. run an on-demand backup
kubectl create -f - <<'EOF'
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata: { generateName: run-cp4d-backup-, namespace: kasten-io }
spec:
  subject: { apiVersion: config.kio.kasten.io/v1alpha1, kind: Policy, name: cp4d-projects-backup-policy, namespace: kasten-io }
EOF

# 2. confirm per-project PVCs + snapshots + restore point
kubectl get pvc -n cp4d-projects-backup -L cp4d.io/project-id
kubectl get volumesnapshot -n cp4d-projects-backup
kubectl get restorepoint -n cp4d-projects-backup

# 3. restore (Step 1 + Step 2 above), then verify the project came back:
./bin/cpdctl asset search --project-id <new-pid> --type-name notebook   --query '*:*'
./bin/cpdctl asset search --project-id <new-pid> --type-name data_asset --query '*:*'
```

**Validated end-to-end on 2026-07-10** (Kasten 8.5.12 / CP4D 5.2.x): backup produced one PVC per
project, all CSI-snapshotted into one restore point; a granular cross-namespace restore of a single
PVC + `restore.sh` recreated the project with its notebook, job, file data asset, connected data
asset, and connection.

---

## Security — credentials in the bundle (read this)

`cpdctl` export does **not** redact connection credentials — by default they are written in
**cleartext** into the bundle (evidence in [DESIGN.md § 6](DESIGN.md#6-the-data-and-credential-boundary-validated-2026-07-10)).
Consequences:

- **V1 (this blueprint): plaintext bundle, protected by RBAC.** `ENCRYPTION_KEY=""`. Acceptable
  under a **single backup admin** with the backup namespace locked down. The bundle PVCs and the
  namespace **must** be tightly RBAC-restricted. The CP4D API key is *not* in the bundle (it lives
  in `cpd`, read cross-namespace, rendered to tmpfs) — only the *connection* credentials inside
  exported projects are.
- **Hardening / multi-tenant: encrypt.** Set `ENCRYPTION_KEY` (a managed secret) in the blueprint
  and pass the same key to `restore.sh` (`ENCRYPTION_KEY=… ./restore.sh …`). cpdctl encrypts the
  masked properties on export and decrypts on import. When tenants self-serve restores into their
  own namespaces, encryption is **not** optional — plaintext would fan credentials out into tenant
  namespaces. At that point store the key in a **KMS** (Azure Key Vault + Secrets Store CSI), never
  a k8s Secret — which also keeps it structurally out of any backup.

## The external-datasource boundary

`cpdctl` export captures the **project** (notebooks, jobs, file data assets, connection
*definitions*, the dependency graph) but **not the external data** behind a connection (the S3
objects, DB rows, on-prem files). A **connected** data asset travels as a *reference only*. This is
a deliberate boundary — the same one cpdbr has. Per connection, the team decides whether the
external datasource also needs protection:

- **No action** if it is read-only/versioned or a system-of-record with its own backups — the
  project just needs it to still exist at restore time.
- **A separate, independent process** otherwise — e.g. a separate Kasten policy for the namespace
  hosting that database. The CP4D project backup and the datasource backup are then two
  coordinated-but-independent restore points.

Document this per project so a restore is never silently incomplete. Bundle-level evidence in
[DESIGN.md § 6](DESIGN.md#6-the-data-and-credential-boundary-validated-2026-07-10).

> **Git-based projects fall under the same rule.** If a project is git-based, the git remote *is* an
> external datasource and needs its own independent protection — and you should still back up the
> CP4D metadata with this blueprint. Git is tamper-*evident*, not tamper-*proof*, and in a real
> disaster the whole CI/CD chain is usually inside the blast radius. See
> [DESIGN.md § 4](DESIGN.md#git-based-does-not-remove-the-need-to-back-up-the-metadata).

---

## Remove everything

The CP4D platform itself is out of scope here (installed and torn down independently). To create the
test fixture project, see [Create the test fixture](#create-the-test-fixture-test-data) above; to
remove it: `./bin/cpdctl project delete --project-id <PID>`, and
`kubectl -n minio delete pod mc-fixture --ignore-not-found` if the fixture pod is still around.

Teardown of this blueprint's resources:

```bash
# policy + blueprint
kubectl delete policy cp4d-projects-backup-policy -n kasten-io
kubectl delete blueprint cp4d-projects-backup-bp -n kasten-io

# backup namespace (per-project PVCs + snapshots) and restore namespaces
kubectl delete ns cp4d-projects-backup
kubectl delete ns john-cp4d-restore

# credential secret + RBAC in cpd
kubectl delete secret cp4d-backup-cpdctl-creds -n cpd
kubectl delete role,rolebinding cp4d-backup-read-creds -n cpd

# Kasten restore-point content for this app
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=cp4d-projects-backup
```
