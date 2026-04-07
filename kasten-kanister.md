# Kasten + Kanister — Reference Documentation

## Overview

Kasten K10 uses [Kanister](https://kanister.io) Blueprints — Kubernetes Custom Resources — to execute application-specific logic around backup and restore operations. In the Kasten context, blueprints serve two purposes:

1. **Application hooks** — attached to a resource (StatefulSet, Deployment, custom resource, etc.) via annotation or BlueprintBinding. Kasten calls reserved action names at fixed points in its backup/restore lifecycle.
2. **Action hooks** — defined in the spec of a Kasten Action (BackupAction, RestoreAction, or ExportAction). ImportAction does not support hooks. The context is the namespace, not a specific resource.

---

## Vocabulary 

 - **Fencing a database** : Isolate the database from the networks (put fences around it) and ensure that all transactions are flushed to the disk for a consistent capture 
 of its storage. The way you fence a database does not belong to Kasten specifically 
 but to the database vendor. In this [example](https://github.com/kastendevhub/enterprise-blueprint/blob/efe6ef727d813ef54c45cde825443833e6e32bd7/edb/edb-hooks.yaml#L26C122-L26C140) we invoke in the blueprint a script in a pod that has been advertised in an annotation by the database vendor. Only database vendor documentation 
 can let you know if fencing is supported and how it is supported.
 - **Database Snapshot**: a filesystem dump, when the next dump is called only files 
 that materialize the changes are appended. Example : Cassandra nodetool snapshot or 
 elasticsearch snapshot.
 - **Fleet automation**: When you want to apply the same workflow for the workload that
 have the same type. For instance we use blueprint binding to make sure that all CNPG 
 postgres cluster are backed up using the same blueprint.

## Invocation Mode 1 — Resource Annotation / BlueprintBinding

### PVC discovery and ownership chain

Kasten supports not only standard workloads (StatefulSet, Deployment) but also operator-managed custom resources such as `postgresql.k8s.enterprisedb.io/Cluster` that directly control their own pods.

Discovery works as follows:

1. Kasten finds PVCs mounted by running pods in the namespace.
2. For each pod, Kasten walks the **owner reference chain** upward (pod → ReplicaSet → Deployment, or pod → StatefulSet, or pod → custom resource, etc.) until it reaches the root owner.
3. If a blueprint is annotated on **any resource in that ownership chain**, Kasten associates all PVCs transitively owned by that resource with the blueprint.
4. `backupPrehook` is called **before** any of those PVCs snapshots are initiated.
5. `backupPosthook` is called **after** all of those PVCs snapshots are ready.

A blueprint attached to a high-level custom resource (e.g., an EDB `Cluster`) will correctly scope hook execution to all PVCs in that ownership subtree, even if pods and PVCs are several levels below.

> **Critical timing constraint — PVC discovery runs before `backupPrehook`.**
> Kasten discovers and registers all PVCs for snapshotting **at backup-action start**, before
> `backupPrehook` executes. Any PVC created during `backupPrehook` is therefore invisible to the
> snapshot phase and will **not** appear in the restore point. This has a direct impact on pattern
> selection:
>
> - **Permanent PVCs** pre-exist at discovery time → they are correctly snapshotted. A
>   BlueprintBinding on the workload that owns (or mounts) the PVC is sufficient.
> - **Temporary PVCs** (created inside `backupPrehook`) are never discovered → they are never
>   snapshotted. The only way to make a temporary PVC visible to Kasten is to create it inside a
>   **BackupAction preHook** (action hook), which runs before PVC discovery. See
>   [Invocation Mode 2 — Action Hooks](#invocation-mode-2--action-hooks) and the warning in the
>   [temporary PVC patterns](#temporary-pvc-patterns--action-hook-required) section.
>
> Note: Kasten also snapshots **orphan PVCs** (PVCs not mounted by any running pod). However,
> since there is no ownership chain to attach a blueprint to, no `backupPrehook` runs for orphan
> PVCs through a BlueprintBinding. If backup data must be written to the PVC before the snapshot,
> you must either use a keeper Deployment (sub-case B below) or an action preHook. An orphan PVC
> with no prehook is only valid when the PVC already holds valid, up-to-date backup data that
> does not need to be refreshed before snapshotting.

### Permanent PVC discovery — two sub-cases

A permanent PVC is always pre-existing when Kasten runs its discovery pass. However, a
BlueprintBinding targets a **workload resource**, not a PVC directly. How Kasten links the PVC
to a BlueprintBinding depends on whether the PVC is mounted by the target workload:

**Sub-case A — PVC mounted by the workload pod (preferred)**

The permanent backup PVC is added to the workload's pod spec as an extra volume mount. Kasten
discovers it transitively through the owner reference chain. The BlueprintBinding targets the
workload CR (StatefulSet, Deployment, or custom resource). No extra deployment is needed.
The `backupPrehook` uses `KubeExec` to run backup commands directly in the workload pod, or
spawns a short-lived pod via `KubeTask` if a separate tool image is needed.
Example: Elasticsearch ECK, where the snapshot repository PVC is declared in the Elasticsearch CR.

**Sub-case B — PVC not mounted by the workload pod (dedicated keeper Deployment)**

The permanent backup PVC exists as a standalone resource, not mounted by the workload's pods at
rest. Create a **keeper Deployment** that runs the backup tool image (e.g. `couchbase/server:7.6.6`
for Couchbase, or a custom image containing the database dump tool) and mounts the backup PVC
permanently. This serves two purposes:

- Kasten discovers the PVC through the Deployment's ownership chain and snapshots it every run.
- The `backupPrehook` uses `KubeExec` into the keeper pod to run backup commands directly —
  no need to create/delete a temporary pod per backup cycle, and no `ReadWriteOnce` conflict
  because the PVC is already mounted by the keeper.

Apply a BlueprintBinding to this keeper Deployment (not to the main workload CR). The keeper
Deployment does not need to do anything at runtime — it simply keeps the PVC mounted so Kasten
can discover it and so that `KubeExec` has a stable target. Use a lightweight long-running command
such as `sleep infinity` if the image does not have a natural entrypoint.

### Reserved action names — Backup

| Action | When Kasten calls it | Typical use |
|---|---|---|
| `backupPrehook` | **Before** the attached PVCs snapshots are initiated and before the preHook associated action| Quiesce the application, or create a dump PVC |
| `backup` | When the blueprint fully manages data movement (no PVC snapshot) | Avoid in Kasten context — use PVC snapshots instead |
| `backupPosthook` | **After** the PVC snapshots are ready and before the onSuccess/onFailure associated action | Unquiesce the application, or delete the temporary dump PVC |

### Reserved action names — Restore

| Action | When Kasten calls it | Typical use |
|---|---|---|
| `restorePrehook` | **Before** the attached PVCs are restored and before the preHook associated action| Pre-restore preparation |
| `restore` | When the blueprint fully manages data movement | Avoid in Kasten context |
| `restorePosthook` | **After** the PVCs are restored and after restore of CR metadata and before the onSuccess/onFailure associated action | Post-restore initialization, cache warming |

> **Any action name other than these six is silently ignored by Kasten.**

> **Known limitation (Kasten ≤ 8.5.x)** — `restorePrehook` is defined in the Kasten
> specification but is **not yet triggered** by the executor in current releases. The action
> is silently skipped during restore. Kasten engineering has confirmed this will be implemented
> in a near-future release. Always implement `restorePrehook` in your blueprints so they are
> ready when the fix ships. In the meantime, document the equivalent manual steps in each
> blueprint's `README.md` so operators know what to run before triggering a Kasten restore.

## The backup and restore workflow managed by kasten 

### Backup

Owned PVCs means the PVCs owned by the resource on which the blueprint apply. 

If the resource blueprint define backup and restore action then only the PVCs manifest 
are captured but not the data on the PVC.

- BackupAction.preHook 
- for each resource in the namespace
  - Resource.backupPrehook 
  - Owned PVCs snapshot start OR Resource.backupHook starts if defined (one exclude the other)
  - Owned PVCs snapshot ready OR Resource.backupHook finish if defined (one exclude the other)
  - Resource.backupPosthook 
- BackupAction.onSuccess or BackupAction.onFailure

### Restore 

- RestoreAction.preHook
- for each resource in the restorePoint
  - Resource.restorePrehook
  - Deletion of Owned PVC 
  - Restore of Owned PVC OR recreation of empty PVC (manifest only) if Resource.restoreHook defined 
- Wait for all resource restored (including CR) to be ready 
- for each resource in the restorePoint
  - Resource.restoreHook
  - Resource.restorePostHook
- RestoreAction.onSuccess or RestoreAction.onFailure


### Assigning a blueprint — Manual annotation

```bash
kubectl -n <app-namespace> annotate <resource-type>/<resource-name> \
  kanister.kasten.io/blueprint=<blueprint-name>
```

### Assigning a blueprint — BlueprintBinding (fleet automation, preferred)

```yaml
apiVersion: config.kio.kasten.io/v1alpha1
kind: BlueprintBinding
metadata:
  name: <app>-binding
  namespace: kasten-io
spec:
  blueprintRef:
    name: <blueprint-name>
    namespace: kasten-io
  resources:
    matchAll:
      - type:
          group: apps
          resource: statefulsets
      - namespace:
          nameSelector:
            matchNames: ["<app-namespace>"]
```

BlueprintBindings take priority over manual annotations. This does not contradict the
use of the `DoesNotExist` operator:
```
      - annotations:
          key: kanister.kasten.io/blueprint
          operator: DoesNotExist
```
Because the BlueprintBinding stops targeting this object anymore, hence no conflict
with the annotation.


### Template context for annotation/fleet blueprints

The context object is the Kubernetes resource the blueprint is bound to:

| Parameter | Description |
|---|---|
| `.StatefulSet.Namespace` | Namespace of the StatefulSet |
| `.StatefulSet.Name` | Name of the StatefulSet |
| `.StatefulSet.Pods` | List of pod names (`index .StatefulSet.Pods 0` for first) |
| `.Deployment.Namespace` | Namespace of the Deployment |
| `.Deployment.Pods` | List of pod names |
| `.Object.metadata.name` | Name of the annotated Kubernetes object |
| `.Phases.<phaseName>.Secrets.<objName>.Data.<key>` | Secret value — declared via `objects` at phase level |
| `.Phases.<phaseName>.ConfigMaps.<objName>.Data.<key>` | ConfigMap value — declared via `objects` at phase level |
| `.Phases.<name>.Output.<key>` | Output from a previous phase **within the same action execution** |
| `.ArtifactsIn.<artifactName>.KeyValue.<key>` | Artifact value passed from a backup action — only available in restore actions and delete actions (`restorePrehook`, `restore`, `restorePosthook`) via `inputArtifactNames` |

In the Kasten context, Secrets and ConfigMaps are declared under an `objects` field **at the phase level**, not at the action level. Kasten resolves the object at runtime using the template expression for `name` and `namespace`, for instance if the name of secret is also the name of the statefulset:

```yaml
phases:
  - func: KubeExec
    name: quiesceApp
    objects:
      pgSecret:
        kind: Secret
        name: '{{ .StatefulSet.Name }}'
        namespace: '{{ .StatefulSet.Namespace }}'
    args:
      command:
        - sh
        - -c
        - |
          password='{{ index .Phases.quiesceApp.Secrets.pgSecret.Data "password" | toString }}'
          <use password in quiesce command>
```

### Passing data from backup actions to restore actions via outputArtifacts

A backup action (`backupPrehook`, `backup`, `backupPosthook`) can declare `outputArtifacts` to store key-value metadata in the Kasten restore point alongside the object manifest. Restore actions (`restorePrehook`, `restore`, `restorePosthook`) can then declare `inputArtifactNames` to consume that metadata via `.ArtifactsIn.<artifactName>.KeyValue.<key>`.

This is useful when a restore action needs information that was only known at backup time (e.g., a PVC name, a database identifier, a configuration value).

```yaml
actions:
  backupPrehook:
    
    outputArtifacts:
      appMeta:
        keyValue:
          dbName: "{{ .Phases.captureInfo.Output.dbName }}"
    phases:
      - func: KubeExec
        name: captureInfo
        args:
          # ... phase that emits dbName via: kando output dbName <value>

  restorePosthook:
    
    inputArtifactNames:
      - appMeta
    phases:
      - func: KubeExec
        name: postRestoreInit
        args:
          command:
            - sh
            - -c
            - |
              db="{{ .ArtifactsIn.appMeta.KeyValue.dbName }}"
              <use db name in post-restore initialization>
```

> **This mechanism is only available for resource-bound blueprints (annotation / BlueprintBinding).** Action hooks cannot create output artifacts or consume input artifacts.

---

## Invocation Mode 2 — Action Hooks

A Kasten Action (BackupAction, RestoreAction, or ExportAction — ImportAction does not support hooks) can define blueprint hooks (`preHook`, `onSuccess`, `onFailure`) in its spec. The blueprint is bound to the namespace, not to a specific resource. See [Kasten action hooks documentation](https://docs.kasten.io/latest/kanister/hooks).

Any action name is valid for action hooks. Recommended convention:

| Action | Trigger |
|---|---|
| `backupPrehook` | Before the backup runs, snapshots are not initiated |
| `backupPosthook` | After a successful backup, snapshots are completed |
| `backupPosthookOnFailure` | After a failed backup |

### Template context for action hooks

The context is the **namespace**, not a specific resource:

| Parameter | Description |
|---|---|
| `.Namespace.Name` | The namespace being backed up |
| `.Phases.<phaseName>.Secrets.<objName>.Data.<key>` | Secret value — declared via `objects` at phase level |
| `.Phases.<phaseName>.ConfigMaps.<objName>.Data.<key>` | ConfigMap value — declared via `objects` at phase level |

---

## Why Kasten Blueprints Must Not Manage Data Movement

In the standalone Kanister framework, blueprints manage the full backup/restore cycle including data movement (`KanisterBackupData`, `KanisterRestoreData`, `KanisterBackupDataDelete`, `kopiaSnapshot` artifacts).

**Do not use these in the Kasten context.** Kasten is the datamover — it snapshots the attached PVCs and handles export to the location profile. Blueprints handle only the surrounding logic.

Do not use in Kasten blueprints:
- `KanisterBackupData` / `KanisterRestoreData` / `KanisterBackupDataDelete`
- `outputArtifacts` with `kopiaSnapshot`
- `inputArtifactNames` referencing kopia snapshots

---

## Key Kanister Functions

This is a list of fuctions that are commonly used in a blueprint, and how to use them in the Kasten context. 

| Function | When to Use |
|---|---|
| `KubeExec` | Run a command in an **existing** running container (like `kubectl exec`) |
| `KubeTask` | Spin up a **new temporary pod** with any image to run a command — **cannot mount existing PVCs**; use `KubeOps` + `WaitV2` + `KubeExec` instead |
| `KubeExecAll` | Run in parallel across **multiple pods/containers** |
| `KubeOps` | Create, patch, or delete Kubernetes resources (PVCs, ConfigMaps, etc.) |
| `WaitV2` | Wait for a condition to be true on a kubernetes resource. The condition is a Go template evaluated against the **full resource object** (not just `.status`). Use `.status.<field>` notation to access status fields. Verify the correct syntax with: `kubectl get <resource> -n <ns> <name> -o go-template='<condition>'` — it should return `true` when the condition is met. Example for Elasticsearch: `{{ if and (or (eq .status.health "green") (eq .status.health "yellow")) (eq .status.phase "Ready") }}true{{ else }}false{{ end }}`. **`objectReference` format**: `apiVersion` is the version ONLY (e.g. `v1`), NOT the combined `group/version`. For CRDs, provide the API group separately in the `group` field. Example for a core resource (`apps/v1 deployments`): `apiVersion: v1, group: apps`. Example for a CRD (`elasticsearch.k8s.elastic.co/v1 elasticsearches`): `apiVersion: v1, group: elasticsearch.k8s.elastic.co`. Core resources (Pods, PVCs, etc.) have no group: `apiVersion: v1` with no `group` field. |

- Use `KubeExec` when you need the application's running environment (credentials, Unix sockets, etc.)
- Use `KubeTask` when you need a specific tool image or isolation from the app container
- **KubeTask namespace choice** — two distinct rules:
  - **`kubectl` operations** (patching CRs, scaling StatefulSets, managing resources across namespaces): run the KubeTask in the **`kasten-io` namespace**. The pod inherits the Kasten service account which has cluster-wide RBAC permissions. The base `gcr.io/kasten-images/kanister-tools` image does NOT include `kubectl` — you must build a custom image that adds it (see [Example Dockerfile](#example-dockerfile)).
  - **Network calls to in-cluster services** (curl to a database HTTP API, gRPC, etc.): run the KubeTask in the **application namespace** (`{{ .Object.metadata.namespace }}`). Network policies may restrict cross-namespace traffic; running in the same namespace as the target service avoids policy violations. No special RBAC is needed for network calls.
- Use `KubeOps` to create/patch/delete Kubernetes resources (PVCs, Pods, ConfigMaps, etc.). To run a command against an existing PVC, use `KubeOps` to create a pod with the PVC pre-mounted, `WaitV2` to wait for it to be ready, and `KubeExec` to execute inside it
---

## The dump-tool image

Wherever a blueprint template shows `<dump-tool-image>`, you need an image that provides:
- **`kando`** — the Kanister CLI, required for `kando output` (emitting phase outputs). It ships in the official Kasten image `gcr.io/kasten-images/kanister-tools:<kasten-version>`.
- **Application-specific dump tools** (e.g. `mongodump`, `pg_dump`, `mysqldump`) added on top.

### Why build on `kanister-tools`

`kando` is not publicly available as a standalone binary — it is only distributed inside `gcr.io/kasten-images/kanister-tools`. Your dump image must therefore use it as the base layer. The `KASTEN_VERSION` build arg should match your deployed Kasten version.

### Example Dockerfile

```dockerfile
ARG KASTEN_VERSION=8.0.8
FROM gcr.io/kasten-images/kanister-tools:${KASTEN_VERSION}

ARG HELM_VERSION=3.7.1
ARG KUBECTL_VERSION=1.32.0
ARG YQ_VERSION=v4.35.2

USER root

RUN microdnf install -y \
    tar \
    jq \
    nc \
    && microdnf clean all \
    && curl -L "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_amd64" \
         -o /usr/local/bin/yq \
    && chmod +x /usr/local/bin/yq \
    && curl -LO "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && curl -LO "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" \
    && tar -zxvf "helm-v${HELM_VERSION}-linux-amd64.tar.gz" \
    && mv linux-amd64/helm /usr/local/bin/helm \
    && rm -rf linux-amd64 "helm-v${HELM_VERSION}-linux-amd64.tar.gz"

USER 1001

ENTRYPOINT ["/bin/sh"]
```

This produces an image with: `kando`, `kubectl`, `helm`, `yq`, `jq`, `tar`, `nc`. Add your application dump tool (e.g. `mongodump`) with an additional `RUN` layer.

### Usage in blueprint phases

**KubeTask** — when you only need to run a command and don't need to mount an existing PVC:

```yaml
- func: KubeTask
  name: runCommand
  args:
    namespace: "{{ .StatefulSet.Namespace }}"
    image: <your-registry>/kasten-tools:<kasten-version>  # built from the Dockerfile above
    command:
      - bash
      - -c
      - |
        <your command here>
```

**KubeOps + WaitV2 + KubeExec** — when you need to mount an existing PVC (KubeTask cannot do this):

```yaml
- func: KubeOps
  name: createDumpPod
  args:
    spec:
      spec:
        containers:
          - name: dump
            image: <your-registry>/kasten-tools:<kasten-version>  # built from the Dockerfile above
```

---

## Blueprint patterns

We can list the different blueprint patterns observed in the field. We use the word `dump` for a logical backup (e.g. `mysqldump` or `mongodump`) and `database snapshot` for an application-aware snapshot (e.g. Cassandra `nodetool snapshot` or the Elasticsearch snapshot API).

The patterns are ordered so that **permanent PVC patterns come before temporary PVC patterns**.
This ordering reflects a deliberate trade-off in favour of the **Single Responsibility Principle
(SRP)** and **Open/Closed Principle (OCP)** of software architecture:

- **Permanent PVC patterns (1–5)** work with BlueprintBinding — each workload type gets its own
  blueprint, and adding a new workload type never requires modifying an existing blueprint.
- **Temporary PVC patterns (6–8)** require a BackupAction preHook (action hook), because PVC
  discovery runs before `backupPrehook` and a PVC created during `backupPrehook` is never
  snapshotted. Action hooks are namespace-scoped: one blueprint handles all workloads in the
  namespace, which violates SRP and OCP. See the warning block in patterns 6–8.

### 1. Fence and quiesce a replica

- **Principle**: Fence (halt) replication synchronization and quiesce a replica to take a backup of the application volumes.
- **Example**: [EDB](https://github.com/kastendevhub/enterprise-blueprint/tree/main/edb) or [Kafka with Mirror Maker](https://github.com/michaelcourcy/kafka-kasten)
- **Pro**: No impact on the primary, because only a replica is quiesced.
- **Cons**: At restore time, the operator must be able to promote the replica to primary, or you have to manually reconfigure your workload.
- **BlueprintBinding**: Yes — attach to the workload CR.
- **Filter PVC**: Yes — you can take only the replica volumes.

### 2. Quiesce

- **Principle**: Ensure that data is flushed to disk using a database primitive and that all transactions are persisted on the volumes before backing them up.
- **Example**: [mariadb-community-operator-standalone](./mariadb-community-operator-standalone/), [MongoDB](https://docs.kasten.io/latest/kanister/mongodb/install_app_cons) or [PostgreSQL](https://docs.kasten.io/latest/kanister/postgresql/install_app_cons)
- **Pro**: Backup is incremental and backup time remains constant as the database grows (unlike a dump strategy).
- **Cons**: When restoring database volumes you cannot be granular (e.g. restore only one database or specific tables/collections).
- **BlueprintBinding**: Yes — attach to the workload CR.
- **Filter PVC**: No — you must take all application volumes.

### 3. Database snapshot on a permanent PVC (sub-case A — PVC mounted by workload)

- **Principle**: The workload is configured to mount a permanent backup PVC. The `backupPrehook` runs the database snapshot tool via `KubeExec` into the workload pod (or a sidecar). The PVC is pre-existing when Kasten runs its discovery pass.
- **Example**: [elasticsearch-eck](./elasticsearch-eck/)
- **Pro**: Database snapshot allows efficient incrementality by appending files at each snapshot. BlueprintBinding works because the PVC is discovered through the workload's ownership chain.
- **Cons**: Requires changing the workload configuration to mount the backup PVC.
- **BlueprintBinding**: Yes — attach to the workload CR.
- **Filter PVC**: Yes — you can take only the permanent PVC used for the snapshot.

### 4. Database dump or snapshot on a permanent PVC (sub-case B — keeper Deployment)

- **Principle**: A dedicated keeper Deployment mounts the backup PVC permanently and runs the backup tool image (e.g. the database server image that includes the backup tool). The `backupPrehook` uses `KubeExec` into the keeper pod to write the dump or snapshot to the PVC. Kasten discovers the PVC through the keeper Deployment's ownership chain.
- **Example**: [couchbase-operator](./couchbase-operator/) (future evolution — current implementation uses a temporary backup pod instead)
- **Pro**: No change to the main workload configuration. PVC is always discovered because the keeper keeps it mounted. `KubeExec` avoids temporary pod lifecycle management. BlueprintBinding attaches to the keeper Deployment.
- **Cons**: Requires deploying an extra Deployment alongside the workload. The keeper image must include the backup tool (e.g. `couchbase/server:<version>` for `cbbackupmgr`).
- **BlueprintBinding**: Yes — attach to the keeper Deployment.
- **Filter PVC**: Yes — you can take only the permanent PVC used for the backup.

### 5. Create logical dump or database snapshot on PVC already used by the database

- **Principle**: When no extra PVC is acceptable and the dump can safely coexist on the database volumes without exhausting storage.
- **Example**: no example
- **Pro**: No change of the workload configuration, no creation of extra storage.
- **Cons**: This is dangerous for the database storage and putting the data on an extra PVC should always be preferred when possible.
- **BlueprintBinding**: Yes — attach to the workload CR.
- **Filter PVC**: No — you must take all the database PVCs.

---

### Temporary PVC patterns — Action Hook required

> ⚠️ **Patterns 6, 7, and 8 create a PVC during `backupPrehook`. Because Kasten's PVC discovery
> runs before `backupPrehook`, this PVC will never be included in the restore point when invoked
> via a BlueprintBinding resource hook.**
>
> The **only** supported approach for these patterns is a **BackupAction preHook** (action hook),
> which runs before PVC discovery. This comes with significant architectural constraints:
>
> - **Context is namespace-only**: `{{ .Namespace.Name }}` is the only available template
>   variable. No `.Object`, `.StatefulSet`, or any resource-level field is available. The blueprint
>   must discover workloads in the namespace by itself (e.g. via `kubectl get`).
> - **No BlueprintBinding**: The hook is configured per-policy in the BackupAction spec, not via a
>   BlueprintBinding.
> - **SRP violation**: A single action hook blueprint handles all workloads in the namespace. Adding
>   a new workload type that also needs a temporary PVC requires modifying the same blueprint —
>   violating the Open/Closed Principle. To limit this impact, **deploy only one type of workload
>   per namespace** when using these patterns.
>
> Only choose patterns 6–8 when patterns 1–5 are technically infeasible.

### 6. Database snapshot on a temporary PVC

- **Principle**: Create a temporary PVC inside a BackupAction preHook (before PVC discovery) and write the database snapshot to it. Kasten discovers and snapshots the PVC, then the `backupPosthook` (or `onSuccess` hook) deletes the temporary PVC.
- **Example**: no example
- **Pro**: Database snapshot allows efficient incrementality by appending files at each snapshot.
- **Cons**: Requires a BackupAction preHook; BlueprintBinding cannot be used. See warning above.
- **BlueprintBinding**: No — must use BackupAction preHook.
- **Filter PVC**: Yes — you can take only the temporary PVC.

### 7. Logical dump on a temporary PVC

- **Principle**: Create a temporary PVC inside a BackupAction preHook and write the logical dump to it. Kasten discovers and snapshots the PVC, then the `onSuccess` hook deletes it.
- **Example**: [Mongoce](https://github.com/kastendevhub/enterprise-blueprint/tree/main/maximo/mongoce)
- **Pro**: A dump enables granular restore.
- **Cons**: No incrementality — as the dump grows over time, backup time grows too. Requires a BackupAction preHook; BlueprintBinding cannot be used. See warning above.
- **BlueprintBinding**: No — must use BackupAction preHook.
- **Filter PVC**: Yes — you can take only the temporary PVC.

### 8. Create dump of an external database on a temporary PVC

- **Principle**: For databases that live outside the cluster but whose logical dump can be created inside the cluster. Create a temporary PVC inside a BackupAction preHook, write the dump to it, let Kasten snapshot it, then delete it.
- **Example**: https://github.com/prcerda/K10-SampleApps/blob/main/PostgreSQL-External/postgresql-ext-blueprint-alldbs.yaml
- **Pro**: Includes an external database in the Kasten backup.
- **Cons**: No incrementality. Requires a BackupAction preHook; BlueprintBinding cannot be used. See warning above.
- **BlueprintBinding**: No — must use BackupAction preHook.
- **Filter PVC**: Yes — you can take only the temporary PVC.


### 9. Trigger a dump of a managed database

- **Principle**: Bind the blueprint to a Secret that provides connection information for a managed database, invoke an external API to create a backup, and store the backup identifier in the restore point alongside the Secret manifest.
- **Example**: [RDS](https://github.com/kanisterio/blueprints/tree/main/aws-rds-postgres), [Firestore](https://github.com/michaelcourcy/kasten-firestore) or [MongoDB Atlas](https://github.com/kanisterio/blueprints/tree/main/mongodb-atlas)
- **Pro**: All backup complexity is handed off to the cloud provider.
- **Cons**: You lose control of the data mover and Kasten is no longer responsible for backup immutability. 
- **Filter PVC**: No PVC is involved.

### 10. Use vendor operator data mover to handle the backup

- **Principle**: Invoke the vendor operator API to trigger a backup.
- **Example**: [CNPG](https://github.com/michaelcourcy/kasten-cnpg), [K8ssandra Medusa](https://docs.kasten.io/latest/kanister/k8ssandra/policy/) or [Crunchy Postgres](https://docs.kasten.io/latest/kanister/pgo/logical)
- **Pro**: All backup complexity is handed off to the operator.
- **Cons**: You lose control of the data mover and Kasten is no longer responsible for backup immutability, unless the vendor stores the backup in a PVC (e.g. the Ansible Automation Platform operator). Often you need to add specific configuration on each resource and if you copy and paste without paying attention you can get backup collision.
- **Filter PVC**: No PVC is involved unless the vendor creates a dedicated backup PVC.

## Use delete action when using pattern 9 and 10

In patterns 9 and 10, Kasten is no longer the data mover. When Kasten deletes a restore point, it
does **not** automatically clean up the external backup that was created by the blueprint (e.g. an
RDS snapshot, an Atlas backup, a CNPG `Backup` CR). To clean up the external backup, implement a
`delete` action in the blueprint. Kasten calls it automatically when the restore point is deleted.

### How the delete action receives the backup identifier

The `backup` (or `backupPrehook` / `backupPosthook`) action must emit the external backup identifier
into the restore point using `outputArtifacts`. The `delete` action then retrieves it via
`inputArtifactNames` and `.ArtifactsIn.<artifactName>.KeyValue.<key>`.

```yaml
actions:
  backup:
    outputArtifacts:
      externalBackup:
        keyValue:
          backupId: "{{ .Phases.triggerBackup.Output.backupId }}"
    phases:
      - func: KubeTask
        name: triggerBackup
        args:
          # ... trigger external backup and emit its ID:
          # kando output backupId <id>

  delete:
    inputArtifactNames:
      - externalBackup
    phases:
      - func: KubeTask
        name: deleteExternalBackup
        args:
          namespace: "{{ .Namespace.Name }}"
          image: <tool-image>
          command:
            - bash
            - -c
            - |
              BACKUP_ID="{{ .ArtifactsIn.externalBackup.KeyValue.backupId }}"
              # call external API to delete the backup identified by BACKUP_ID
```

### Execution context for the delete action

The `delete` action does **not** run in the context of the original application object — that object
may have been deleted long before the restore point is retired. Instead, it runs in the Kasten
namespace context. Use `{{ .Namespace.Name }}` to refer to the Kasten namespace (works whether
Kasten is installed in `kasten-io` or any other namespace), and run KubeTask phases in that same
namespace.
 
## References

- [Kasten K10 Blueprints Documentation](https://docs.kasten.io/latest/usage/blueprints.html)
- [Kasten Action Hooks](https://docs.kasten.io/latest/kanister/hooks)
- [Blueprint Bindings API](https://docs.kasten.io/latest/usage/blueprint_bindings.html)
- [Kanister Functions Reference](https://docs.kanister.io/functions.html)
- [Kanister Template Parameters](https://docs.kanister.io/templates.html)
- [Kanister GitHub (community blueprints)](https://github.com/kanisterio/kanister/tree/master/examples)
