# Kasten + Kanister — Reference Documentation

## Overview

Kasten K10 uses [Kanister](https://kanister.io) Blueprints — Kubernetes Custom Resources — to execute application-specific logic around backup and restore operations. In the Kasten context, blueprints serve two purposes:

1. **Application hooks** — attached to a resource (StatefulSet, Deployment, custom resource, etc.) via annotation or BlueprintBinding. Kasten calls reserved action names at fixed points in its backup/restore lifecycle.
2. **Policy hooks** — invoked directly by a Kasten policy (preHook, postHook, postHook on error). The context is the namespace, not a specific resource.

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

### Reserved action names — Backup

| Action | When Kasten calls it | Typical use |
|---|---|---|
| `backupPrehook` | **Before** the attached PVCs snapshots are initiated | Quiesce the application, or create a dump PVC |
| `backup` | When the blueprint fully manages data movement (no PVC snapshot) | Avoid in Kasten context — use PVC snapshots instead |
| `backupPosthook` | **After** the PVC snapshots are ready | Unquiesce the application, or delete the temporary dump PVC |

### Reserved action names — Restore

| Action | When Kasten calls it | Typical use |
|---|---|---|
| `restorePrehook` | **Before** the attached PVCs are restored | Pre-restore preparation |
| `restore` | When the blueprint fully manages data movement | Avoid in Kasten context |
| `restorePosthook` | **After** the PVCs are restored and after restore of CR metadata and before the hook.onSuccess or hook.onFailure actions associated with the restore action/policy | Post-restore initialization, cache warming |

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

- BackupAction.Prehook 
- Resource.backupPrehook 
- Owned PVCs snapshot start OR Resource.backupHook starts if defined 
- Owned PVCs snapshot ready OR Resource.backupHook finish if defined
- Resource.backupPosthook 
- BackupAction.postHook or BackupAction.postHookError

### Restore 

- RestoreAction.prehook
- Resource.restorePrehook
- Deletion of Owned PVC 
- Restore of Owned PVC OR recreation of empty PVC (manifest only) if Resource.restoreHook defined 
- Wait for all resource restored (including CR) and all pods ready in the restorepoint
- Resource.restoreHook executed if defined 
- Resource.restorePostHook
- RestoreAction.postHook or RestoreAction.postHookError


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

In the Kasten context, Secrets and ConfigMaps are declared under an `objects` field **at the phase level**, not at the action level. Kasten resolves the object at runtime using the template expression for `name` and `namespace`:

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

> **This mechanism is only available for resource-bound blueprints (annotation / BlueprintBinding). It is not available for policy hooks**, which are not associated with a specific restore point.

---

## Invocation Mode 2 — Policy Hooks

A Kasten policy can directly reference a blueprint as a hook (preHook, onSuccess, onFailure). The blueprint is then bound to the namespace itself — it is called at the policy level. See [Kasten policy hooks documentation](https://docs.kasten.io/latest/kanister/hooks).

Any action name is valid for policy hooks. Recommended convention:

| Action | Trigger |
|---|---|
| `backupPrehook` | Before the backup policy runs, snapshots are not initiated |
| `backupPosthook` | After a successful backup, snapshots are completed |
| `backupPosthookOnFailure` | After a failed backup |

### Template context for policy hooks

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

## Blueprint YAML Examples

### Pattern 1 — Quiesce / Unquiesce

```yaml
apiVersion: cr.kanister.io/v1alpha1
kind: Blueprint
metadata:
  name: <app>-blueprint
  namespace: kasten-io
actions:
  backupPrehook:
    
    phases:
      - func: KubeExec
        name: quiesceApp
        args:
          namespace: "{{ .StatefulSet.Namespace }}"
          pod: "{{ index .StatefulSet.Pods 0 }}"
          container: <container>
          command: [sh, -c, "<quiesce command>"]
    # NOTE: do NOT use deferPhase here for unquiescing.
    # deferPhase runs at the end of backupPrehook, BEFORE Kasten takes PVC snapshots.
    # Unquiescing in deferPhase would defeat the purpose of quiescing entirely.
    # Unquiesce belongs exclusively in backupPosthook, which Kasten calls after snapshots.
    # If backupPrehook fails and the app is partially quiesced, Kasten aborts the backup.
    # Design quiesce commands to be atomic or safe to retry.

  backupPosthook:
    # Called by Kasten after all PVC snapshots are ready.
    
    phases:
      - func: KubeExec
        name: unquiesceApp
        args:
          namespace: "{{ .StatefulSet.Namespace }}"
          pod: "{{ index .StatefulSet.Pods 0 }}"
          container: <container>
          command: [sh, -c, "<unquiesce command>"]

  restorePosthook:
    
    phases:
      - func: KubeExec
        name: postRestoreInit
        args:
          namespace: "{{ .StatefulSet.Namespace }}"
          pod: "{{ index .StatefulSet.Pods 0 }}"
          container: <container>
          command: [sh, -c, "<post-restore initialization>"]
```

### Pattern 2 — Dump to PVC

```yaml
apiVersion: cr.kanister.io/v1alpha1
kind: Blueprint
metadata:
  name: <app>-dump-blueprint
  namespace: kasten-io
actions:
  backupPrehook:
    
    phases:
      # Idempotent cleanup: remove any pod/PVC left over from a previous failed
      # run so that the create steps below don't fail on already-existing resources.
      - func: KubeTask
        name: cleanupPreviousRun
        args:
          namespace: "{{ .StatefulSet.Namespace }}"
          image: <kubectl-image>
          command:
            - bash
            - -c
            - |
              kubectl delete pod {{ .StatefulSet.Name }}-dump-pod \
                -n {{ .StatefulSet.Namespace }} --ignore-not-found=true
              kubectl delete pvc {{ .StatefulSet.Name }}-dump \
                -n {{ .StatefulSet.Namespace }} --ignore-not-found=true
      - func: KubeOps
        name: createDumpPVC
        args:
          namespace: "{{ .StatefulSet.Namespace }}"
          resource:
            apiVersion: v1
            resource: persistentvolumeclaims
          spec:
            apiVersion: v1
            kind: PersistentVolumeClaim
            metadata:
              name: "{{ .StatefulSet.Name }}-dump"
              namespace: "{{ .StatefulSet.Namespace }}"
            spec:
              accessModes: [ReadWriteOnce]
              resources:
                requests:
                  storage: 10Gi
      # KubeTask cannot mount an existing PVC — use KubeOps to create a pod
      # with the PVC pre-mounted, WaitV2 to confirm readiness, then KubeExec.
      - func: KubeOps
        name: createDumpPod
        args:
          namespace: "{{ .StatefulSet.Namespace }}"
          resource:
            apiVersion: v1
            resource: pods
          spec:
            apiVersion: v1
            kind: Pod
            metadata:
              name: "{{ .StatefulSet.Name }}-dump-pod"
              namespace: "{{ .StatefulSet.Namespace }}"
            spec:
              restartPolicy: Never
              containers:
                - name: dump
                  image: <dump-tool-image>
                  command: ["sleep", "infinity"]
                  volumeMounts:
                    - name: dump-pvc
                      mountPath: /dump
              volumes:
                - name: dump-pvc
                  persistentVolumeClaim:
                    claimName: "{{ .StatefulSet.Name }}-dump"
      - func: WaitV2
        name: waitForDumpPod
        args:
          timeout: 5m
          conditions:
            anyOf:
              - condition: '{{ $available := false }}{{ range .Conditions }}{{ if and (eq .Type "Ready") (eq .Status "True") }}{{ $available = true }}{{ end }}{{ end }}{{ $available }}'
                objectReference:
                  apiVersion: v1
                  resource: pods
                  name: "{{ .StatefulSet.Name }}-dump-pod"
                  namespace: "{{ .StatefulSet.Namespace }}"
      - func: KubeExec
        name: dumpData
        args:
          namespace: "{{ .StatefulSet.Namespace }}"
          pod: "{{ .StatefulSet.Name }}-dump-pod"
          container: dump
          command:
            - bash
            - -c
            - |
              <run dump command into /dump>
              kando output dumpCompleted true

    deferPhase:
      # Runs after backupPrehook regardless of success or failure.
      # Only clean up the ephemeral pod here (--ignore-not-found is safe whether
      # the pod was created or not, or was already deleted by deleteDumpPod).
      # Do NOT delete the dump PVC here: deferPhase runs before Kasten takes
      # any snapshot, so deleting the PVC here would prevent it from being
      # captured in the restore point. If backupPrehook fails, Kasten aborts
      # the backup entirely and takes no snapshots, so an incomplete dump PVC
      # left behind is harmless. backupPosthook deletes the PVC on success.
      func: KubeOps
      name: cleanupDumpPod
      args:
        namespace: "{{ .StatefulSet.Namespace }}"
        op: delete
        resource:
          apiVersion: v1
          resource: pods
          name: "{{ .StatefulSet.Name }}-dump-pod"

  backupPosthook:
    
    phases:
      - func: KubeOps
        name: deleteDumpPVC
        args:
          namespace: "{{ .StatefulSet.Namespace }}"
          op: delete
          resource:
            apiVersion: v1
            resource: persistentvolumeclaims
            name: "{{ .StatefulSet.Name }}-dump"

  restorePosthook:
    
    phases:
      # Same pattern as backupPrehook: KubeOps creates the pod with the
      # restored dump PVC pre-mounted, WaitV2 confirms readiness, KubeExec
      # runs the replay, then KubeOps deletes the pod.
      - func: KubeOps
        name: createRestorePod
        args:
          namespace: "{{ .StatefulSet.Namespace }}"
          resource:
            apiVersion: v1
            resource: pods
          spec:
            apiVersion: v1
            kind: Pod
            metadata:
              name: "{{ .StatefulSet.Name }}-restore-pod"
              namespace: "{{ .StatefulSet.Namespace }}"
            spec:
              restartPolicy: Never
              containers:
                - name: restore
                  image: <dump-tool-image>
                  command: ["sleep", "infinity"]
                  volumeMounts:
                    - name: dump-pvc
                      mountPath: /dump
              volumes:
                - name: dump-pvc
                  persistentVolumeClaim:
                    claimName: "{{ .StatefulSet.Name }}-dump"
      - func: WaitV2
        name: waitForRestorePod
        args:
          timeout: 5m
          conditions:
            anyOf:
              - condition: '{{ $available := false }}{{ range .Conditions }}{{ if and (eq .Type "Ready") (eq .Status "True") }}{{ $available = true }}{{ end }}{{ end }}{{ $available }}'
                objectReference:
                  apiVersion: v1
                  resource: pods
                  name: "{{ .StatefulSet.Name }}-restore-pod"
                  namespace: "{{ .StatefulSet.Namespace }}"
      - func: KubeExec
        name: restoreFromDump
        args:
          namespace: "{{ .StatefulSet.Namespace }}"
          pod: "{{ .StatefulSet.Name }}-restore-pod"
          container: restore
          command:
            - bash
            - -c
            - |
              <replay dump from /dump into the application>
      - func: KubeOps
        name: deleteRestorePod
        args:
          namespace: "{{ .StatefulSet.Namespace }}"
          op: delete
          resource:
            apiVersion: v1
            resource: pods
            name: "{{ .StatefulSet.Name }}-restore-pod"
      - func: KubeOps
        name: deleteRestorePVC
        args:
          namespace: "{{ .StatefulSet.Namespace }}"
          op: delete
          resource:
            apiVersion: v1
            resource: persistentvolumeclaims
            name: "{{ .StatefulSet.Name }}-dump"
```

---

## Key Kanister Functions

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

### 1. Fence and quiesce a replica

- **Principle**: Fence (halt) replication synchronization and quiesce a replica to take a backup of the application volumes.
- **Example**: [EDB](https://github.com/kastendevhub/enterprise-blueprint/tree/main/edb) or [Kafka with Mirror Maker](https://github.com/michaelcourcy/kafka-kasten)
- **Pro**: No impact on the primary, because only a replica is quiesced.
- **Cons**: At restore time, the operator must be able to promote the replica to primary, or you have to manually reconfigure your workload.
- **Filter PVC**: Yes — you can take only the replica volumes.

### 2. Quiesce

- **Principle**: Ensure that data is flushed to disk using a database primitive and that all transactions are persisted on the volumes before backing them up.
- **Example**: [MongoDB](https://docs.kasten.io/latest/kanister/mongodb/install_app_cons) or [PostgreSQL](https://docs.kasten.io/latest/kanister/postgresql/install_app_cons)
- **Pro**: Backup is incremental and backup time remains constant as the database grows (unlike a dump strategy).
- **Cons**: When restoring database volumes you cannot be granular (e.g. restore only one database or specific tables/collections).
- **Filter PVC**: No — you must take all application volumes.

### 3. database snapshot on a temporary pvc

- **Principle**: Create a temporary PVC and write the database snapshot on it before it is protected by Kasten.
- **Example**: no example
- **Pro**: Database snapshot allows efficient incrementality by appending files at 
each snapshot
- **Cons**: only feasible if the database snapshot can run remotely. You also need to manage the creation and deletion of the temporary PVC and dump pod.
- **Filter PVC**: Yes — you can take only the temporary PVC.

### 4. database dump on a temporary pvc

- **Principle**: Create a temporary PVC and a dump pod that writes the dump or database snapshot to the PVC before it is protected by Kasten.
- **Example**: [Mongoce](https://github.com/kastendevhub/enterprise-blueprint/tree/main/maximo/mongoce)
- **Pro**: A dump enables granular restore
- **Cons**: No incrementality — as the dump grows over time, backup time grows too. You also need to manage the creation and deletion of the temporary PVC and dump pod.
- **Filter PVC**: Yes — you can take only the temporary PVC.


### 5. database snapshot on a permanent pvc

- **Principle**: Same as 3 but the pvc is permanent. You
choose 5 over 3 when the snapshot can not be done without having the pvc attached
to the database pod (for instance cassandra) but you prefer 3 because 5 implies changing the workload configuration.
- **Example**: https://github.com/kastendevhub/enterprise-blueprint/tree/main/k8ssandra-non-incremental
- **Pro**: Database snapshot allows efficient incrementality by appending files at 
each snapshot
- **Cons**: You have to change the workload configuration to introduce the permanent 
PVC
- **Filter PVC**: Yes — you can take only the permanent PVC used for the snapshot.


### 6. database dump on a permanent pvc

- **Principle**: Same as 4 but the pvc is permanent. You choose 6 over 4 when the dump can not be done without having the pvc attached to the database pod (for instance mssql dump) but you prefer 4 because 6 implies changing the workload configuration.
- **Example**: https://github.com/kastendevhub/enterprise-blueprint/tree/main/dh2i
- **Pro**: A dump enables granular restore
- **Cons**: No incrementality — as the dump grows over time, backup time grows too. 
You have to change the workload configuration to introduce the permanent PVC
- **Filter PVC**: Yes — you can take only the permanent PVC used for the dump.

### 7. Create logical dump or database snapshot on PVC already used by the database

- **Principle**: When no extra PVC is acceptable and the dump can safely coexist on the database volumes without exhausting storage.
- **Example**: no example
- **Pro**: No change of the workload configuration, no creation of the database storage
- **Cons**: This is dangerous for the database storage and putting the data on extra pvc should be always preferred when possible
- **Filter PVC**: No — You have to take all the database PVCs

### 8. Create dump of an external database on a temporary PVC

- **Principle**: For databases that live outside the cluster but the logical dump can be created on the kubernetes cluster.
- **Example**: https://github.com/prcerda/K10-SampleApps/blob/main/PostgreSQL-External/postgresql-ext-blueprint-alldbs.yaml
- **Pro**: Include external database in the kasten backup
- **Cons**: No incrementality — as the dump grows over time, backup time grows too. 
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
- [Kasten Policy Hooks](https://docs.kasten.io/latest/kanister/hooks)
- [Blueprint Bindings API](https://docs.kasten.io/latest/usage/blueprint_bindings.html)
- [Kanister Functions Reference](https://docs.kanister.io/functions.html)
- [Kanister Template Parameters](https://docs.kanister.io/templates.html)
- [Kanister GitHub (community blueprints)](https://github.com/kanisterio/kanister/tree/master/examples)
