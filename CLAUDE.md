# Kasten Blueprint Development — Rules and Patterns

> Full mechanics, YAML examples, and template reference: see [kasten-kanister.md](kasten-kanister.md)

## Core principle

**Kasten is the datamover.** Blueprints must not transfer data themselves. Kasten snapshots the PVCs attached to the resource. Blueprints only handle surrounding logic: quiescing, dump-to-PVC when needed, and unquiescing.

Never use in Kasten blueprints: `KanisterBackupData`, `KanisterRestoreData`, `KanisterBackupDataDelete`, `kopiaSnapshot` artifacts.

---

## Reserved action names (annotation / BlueprintBinding mode)

Kasten only recognises these six names. Any other action name is silently ignored.

| Action | When called | Use for |
|---|---|---|
| `backupPrehook` | Before PVC snapshots are initiated| Quiesce, or create dump PVC |
| `backup` | Blueprint manages data movement | **Avoid** — use PVC snapshots |
| `backupPosthook` | After PVC snapshots are ready | Unquiesce, or delete dump PVC |
| `restorePrehook` | Before PVC restore | Pre-restore preparation (**not yet triggered** in Kasten ≤ 8.5.x — always implement for when the fix ships; document manual steps in `README.md`) |
| `restore` | Blueprint manages data movement | **Avoid** |
| `restorePosthook` | After PVC restore | Post-restore init, cache warming |

For policy hooks (namespace context), any action name is valid — convention is the same names above. See [kasten-kanister.md](kasten-kanister.md#invocation-mode-2--policy-hooks).

---

## Blueprint development workflow

Follow these five steps in order when developing a new blueprint.

### Step 1 — Choose a pattern and document it

Select the backup pattern from the list in [kasten-kanister.md](kasten-kanister.md#blueprint-patterns). For workloads where **Kasten is the data mover**, apply them in this preferred order — stop at the first one that is technically feasible for the target database:

1. **Fence and quiesce a replica** — best choice when a replica exists; zero primary impact.
2. **Quiesce** — preferred when crash-consistent snapshots are sufficient and incrementality matters (backup time stays constant as data grows).
3. **database snapshot on a temporary pvc** — use database snapshots (e.g. Cassandra, Elasticsearch) to leverage incrementality because they append new snapshot files
4. **Logical dump on a temporary pvc** — use when a logical dump is needed for granular restore or because quiescing is not possible. Choose 3 over 4 if possible because for plain dump there is no incrementality. 
5. **database snapshot on a permanent pvc** — same as 3 but the pvc is permanent. You 
choose 5 over 3 when the database snapshot can not be done without having the pvc attached to the database pod (for instance cassandra) but you prefer 3 because 5 implies changing the workload configuration.
6. **Logical dump or database snapshot on a permanent pvc** — same as 4 but the pvc is permanent. You choose 6 over 4 when the dump can not be done without having the pvc attached to the database pod (for instance mssql dump) but you prefer 4 because 6 implies changing the workload configuration.
7. **Create logical dump or database snapshot on PVC already used by the database** — when no extra PVC is acceptable and the dump can safely coexist on the database volumes without exhausting storage. This is dangerous for the database storage and putting the data on extra pvc should be always preferred when possible
8. **Create dump of an external database on a temporary PVC** — for databases that live outside the cluster but the logical dump can be created on the kubernetes cluster.

When **Kasten is not the data mover**, two additional patterns apply. These are not ranked against the list above — they are the only valid options when the database is managed or when the operator owns the backup mechanism:

9. **Trigger a dump of a managed database** — for cloud-managed databases (RDS, Firestore, MongoDB Atlas, etc.); the cloud provider owns immutability.
10. **Use vendor operator data mover** — when the operator has a built-in backup API (CNPG, K8ssandra Medusa, Crunchy Postgres, etc.); the operator owns immutability.

The choice must balance implementation complexity (backup **and** restore), and whether incrementality is critical (patterns 1–2 and 3-5 with append-only database snapshots are incremental; plain dump patterns are not). **Document the chosen pattern and its rationale in a `README.md` before writing any code. This must be reviewed before development begins.**

### Step 2 — Deploy the workload and create test data

Deploy the target application in a test namespace. Create a representative minimum dataset that will let you validate a restore (e.g. a known set of records, files, or keys that you can verify after recovery). For the speed of the development lifecycle this is very important to create a limited dataset (Under 5Kb). Later once the whole blueprint works you can try performance test by increasing the size of the dataset. But this is out of the scope of this activity. 

The way you deploy the workload and how you create test data should be documented in the README.md so that
user can test it on their own. Also you must describe how to remove the workload and its dependencies. 
Describe also the deletion of the restorepointcontent created for the clean up :
```
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=<NAMESPACE USED FOR THE TEST>
```

### Step 3 — Validate the backup/restore workflow without a blueprint

The goal of this step is to fully validate the chosen backup/restore workflow for the target workload, **without the complexity of a blueprint or Kasten policy**. Step 4 then automates exactly what was validated here. These are two separate concerns: first prove the workflow works, then encode it.

Execute each operation using `kubectl exec`, `kubectl cp`, or direct API calls. Use these primitives to emulate what Kasten does at each stage:
- **PVC backup**: use a CSI snapshot to emulate what Kasten does to application PVCs. Follow [this example](https://github.com/michaelcourcy/test-csi-snapshot) if you don't know how to create csi snapshot. Always make sure that you wait for snapshots to be ready before you test the backupPosthook.
- **Object storage**: deploy a MinIO instance in a dedicated namespace to emulate an S3 endpoint (needed for *Use vendor operator data mover* patterns). You can find an example for deploying minio [here](https://github.com/michaelcourcy/kasten-s3troubleshooting?tab=readme-ov-file#test-with-a-minio-instance).

Verify that:
- Deleting the test data after a backup and then restoring it retrieves the expected records.

**If Step 3 succeeds but Step 4 fails, do not change the pattern.** Step 4 is purely an automation of Step 3 — if the blueprint does not behave as the validated workflow did, there is a blueprint or Kasten integration issue, not a strategy issue. Stop, explain the discrepancy to the user, and work through it together before considering any change of pattern.

### Step 4 — Implement the blueprint and document dependencies

Write the Blueprint (and BlueprintBinding if needed). Document all deployment prerequisites in the `README.md`:
- Any custom dump image (base image, tools added, registry location).
- Extra operator configuration required (e.g. enabling a backup API, creating a backup user).
- Extra Helm values or manifests that must be applied before the blueprint works.

**Always include a versions table** in the `README.md` documenting the exact versions used when the blueprint was developed and tested. Blueprint behaviour is often version-sensitive (operator APIs change, database backup semantics differ across releases). The table must cover at minimum:

| Component | Version |
|---|---|
| Kubernetes | e.g. `1.32` (EKS) |
| Kasten | e.g. `8.5.2` |
| Operator / Helm chart | e.g. `mariadb-operator 25.10.4` |
| Database / Application | e.g. `MariaDB 11.8.5` |

Omit the operator row for workloads deployed without an operator (plain Helm chart only).

**`restorePrehook` is not yet triggered in Kasten ≤ 8.5.x.** Always implement it in the blueprint (for forward compatibility), and add a prominent warning section in the `README.md` with the equivalent manual steps the operator must run before triggering a Kasten restore until the fix ships.

**Keep the "Blueprint actions" table in `README.md` in sync with the blueprint YAML at all times.**
Every action defined in the blueprint (`backupPrehook`, `backupPosthook`, `restorePrehook`, `restorePosthook`, etc.) must have a row in that table describing what it does. No action may exist in the YAML without a corresponding row, and no row may exist without a corresponding action in the YAML.

If a custom container image is needed (e.g. a tool image for `KubeTask`), commit its `Dockerfile`
in a subdirectory of the blueprint folder: `<blueprint-dir>/images/<image-name>/Dockerfile`.
**Never reference a custom image in a blueprint without a committed Dockerfile.** Document the
image name, base image, and what was added in the `README.md`.

Do not assume that the base image contains jq, yq or kubectl, most of the time you have to add them.

### Step 5 — Test the blueprint end-to-end

Run a full backup/restore cycle through Kasten:
- Ensure the workload is bound through the kasten annotation, a blueprintbinding or a policy hook
- Create a backup policy on demand with location profile for Kanister actions (Export Location Profile
is optional and useless in this context) and trigger a runaction.

**Never create a Kanister ActionSet directly to test a blueprint.** Kasten sets up the ActionSet
context differently from a manually created one — for example, `.Namespace.Name` is only populated
when Kasten invokes a policy hook, not in a hand-crafted ActionSet. A manual ActionSet will
silently resolve template variables to empty strings, causing incorrect behaviour. Always trigger
testing through a Kasten RunAction.
- Check in the kanister logs that the blueprint executed as expected, check the status of the runaction is completed
- Corrupt or delete the test data.
- Restore from the restore point and verify that:
    1. kanister logs show expected output
    2. the status of the restore action is completed 
    3. the test data is recovered correctly.

Iterate until success without coming back to step 1 unless you discover step 1 can not be implemented.

---

## Deployment rules

- Always deploy blueprints in the **`kasten-io` namespace**.
- Prefer **BlueprintBindings** over manual annotations for fleet automation when you can consistently identify that this type of resource will always be backed up the same way. Notice that often blueprintbinding use the `DoesNotExist`operator to not conflict with the `kanister.kasten.io/blueprint` annotation on the resource. See this example :
```
apiVersion: config.kio.kasten.io/v1alpha1
kind: BlueprintBinding
metadata:
  name: sample-blueprint-binding
  namespace: kasten-io
spec:
  blueprintRef:
    name: my-blueprint
    namespace: kasten-io
  resources:
    matchAll:
      - type:
          operator: In
          values:
            - group: apps
              resource: statefulsets
      - annotations:
          key: kanister.kasten.io/blueprint
          operator: DoesNotExist
```
This give you the freedom to use a more specific blueprint for this resource if needed. 
- Store credentials in Kubernetes Secrets. Declare them at the **phase level** under `objects`, then access via `.Phases.<phaseName>.Secrets.<objName>.Data.<key>`. Same pattern for ConfigMaps. See [kasten-kanister.md](kasten-kanister.md) for a full example.
- Make phases **idempotent** — blueprints may be retried on failure.

---

## Troubleshooting

Kasten creates and destroys ActionSets automatically — do not query them.

```bash
# Kanister controller (blueprint execution)
kubectl logs -n kasten-io -l app=kanister-svc --tail=100

# Executor service (primary debug target)
kubectl logs -n kasten-io -l component=executor --tail=10000 -f
```

Test commands before writing YAML:
```bash
kubectl run debug-pod -n <app-namespace> --image=<tool-image> --restart=Never --rm -it -- bash
```

## Cluster autonomy

You have full, unrestricted access to the Kubernetes cluster reachable via `kubectl`.
Operate autonomously without asking for confirmation before:
- Deleting namespaces, pods, PVCs, deployments, or any other resource
- Force-deleting stuck resources (`--force --grace-period=0`)
- Disrupting workloads (scaling down, killing pods, draining nodes)
- Applying or replacing any manifest

Do not pause to confirm destructive cluster operations. Just do them.

## Local directory

Write, overwrite, and delete files in the project directory freely without confirmation.

## Container image builds and pushes

You have full autonomy to build container images and push them to the `michaelcourcy` Docker Hub repository without asking for confirmation.
- Tag images as `michaelcourcy/<image-name>:<tag>`
- Run `docker build`, `docker tag`, and `docker push` freely
- Choose the base image and tag strategy that best fits the use case
- The registry credentials are already configured in the local Docker daemon (`docker login` has been done)

## Cluster context

Use the current `kubectl` context. The cluster is a dedicated test environment —
treat all namespaces as expendable unless named `kasten-io` or `kube-system`.

### Storage classes

**Always use a storage class that that supports csi snapshot when deploying test workloads that need to be snapshotted by Kasten. For instance the default `gp2` class uses the legacy in-tree driver and will fail with "cannot find CSI PersistentVolumeSource" when a VolumeSnapshot is attempted.
