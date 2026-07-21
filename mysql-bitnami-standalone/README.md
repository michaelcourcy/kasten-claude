# Kasten Blueprint — Bitnami MySQL (standalone)

Application-consistent backup/restore for **MySQL deployed with the Bitnami Helm chart** in
**standalone** architecture, using Kasten as the data mover.

## Pattern

**Pattern 2 — Quiesce.** The Bitnami standalone chart is a single-replica `StatefulSet` with one
RWO data PVC (`/bitnami/mysql`, InnoDB). Kasten snapshots that PVC directly; the blueprint's only
job is to make the on-disk state consistent at snapshot time.

- `backupPrehook` opens a **background** `mysql` session holding `FLUSH TABLES WITH READ LOCK`
  (kept alive with `DO SLEEP(3600)`) so the lock is actually held while Kasten snapshots the PVC.
  A plain exec that runs `FTWRL` and returns would release the lock immediately — the lock is
  bound to the session that acquired it.
- Kasten snapshots the data PVC.
- `backupPosthook` kills the background session, releasing the lock.
- **Restore** is native Kasten PVC replacement (scale down → swap PVC → scale up). InnoDB performs
  crash recovery automatically on startup. No `restorePrehook` is needed because there is **no
  operator** reconciling the StatefulSet (unlike operator-managed databases).

**Why not other patterns:** there is no replica in standalone (rules out Pattern 1 fence-a-replica);
InnoDB crash-consistent snapshots make logical dumps (patterns 3–9) unnecessary and non-incremental;
Bitnami is a self-managed StatefulSet with no backup API (rules out patterns 10–11). This pattern is
incremental (constant backup time as data grows, via block-level CSI snapshots) and
BlueprintBinding-compatible, preserving SRP/OCP.

## Versions

| Component | Version |
|---|---|
| Kubernetes | `1.32` (EKS) |
| Kasten | `8.5.13` |
| Helm chart | `bitnami/mysql 14.0.3` |
| Database | `MySQL 9.4.0` |

> Detect your own Kasten version with `helm ls -n kasten-io` (or, on OpenShift OLM,
> `kubectl get csv -n kasten-io -o jsonpath='{.items[?(@.spec.displayName=="Kasten K10")].spec.version}'`).

## Files

| File | Purpose |
|---|---|
| `blueprint.yaml` | The Kasten/Kanister blueprint (quiesce / unquiesce / restore verify). |
| `blueprintbinding.yaml` | Binds the blueprint to Bitnami MySQL primary StatefulSets fleet-wide. |

---

## Prerequisites

- A storage class backed by a **CSI driver that supports snapshots**, with a matching
  `VolumeSnapshotClass` registered with Kasten.

> **Storage class**: `ebs-sc` is the AWS EBS CSI storage class used in our test environment.
> Replace it with a storage class that supports CSI snapshots on your cluster
> (e.g. `managed-csi` on AKS, `standard-rwo` on GKE, your custom class on bare-metal, etc.).
> The class must have a matching `VolumeSnapshotClass` registered with Kasten.
> Do **not** use legacy in-tree classes (e.g. `gp2` on AWS) — they do not support CSI snapshots.

### Bitnami image availability

As of late 2025, Bitnami moved its free container images to the `bitnamilegacy` Docker Hub
repository, so the chart's default image (`docker.io/bitnami/mysql:<tag>`) no longer pulls. The
deployment below redirects the image to `bitnamilegacy` and sets
`global.security.allowInsecureImages=true` (which disables the chart's image-origin verification).
Adjust to your own mirror / Bitnami Secure Images subscription as appropriate.

---

## Step 2 — Deploy the workload and create test data

```bash
kubectl create namespace mysql-test

helm install mysql oci://registry-1.docker.io/bitnamicharts/mysql \
  --version 14.0.3 \
  -n mysql-test \
  --set architecture=standalone \
  --set auth.rootPassword=Root1234! \
  --set auth.database=appdb \
  --set auth.username=appuser \
  --set auth.password=AppPass123! \
  --set primary.persistence.size=2Gi \
  --set global.defaultStorageClass=ebs-sc \
  --set primary.persistence.storageClass=ebs-sc \
  --set global.security.allowInsecureImages=true \
  --set image.registry=docker.io \
  --set image.repository=bitnamilegacy/mysql

kubectl rollout status sts/mysql -n mysql-test --timeout=180s
```

> **Storage class**: see the callout above — replace `ebs-sc` with a CSI snapshot-capable class.

Create a minimal, verifiable dataset (< 5 KB):

```bash
kubectl exec mysql-0 -n mysql-test -c mysql -- bash -c '
RP=$(cat "$MYSQL_ROOT_PASSWORD_FILE")
mysql -uroot -p"$RP" appdb <<SQL
CREATE TABLE IF NOT EXISTS pets (id INT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(50), species VARCHAR(50));
INSERT INTO pets (name, species) VALUES ("Rex","dog"),("Whiskers","cat"),("Tweety","bird"),("Nemo","fish"),("Bugs","rabbit");
SELECT COUNT(*) AS row_count FROM pets;
SQL
'
```

Expected: `row_count = 5`.

> **Note on credentials**: the Bitnami image exposes the root password as a **file**
> (`$MYSQL_ROOT_PASSWORD_FILE` points at the mounted secret), not as a plain env var. Every command
> and blueprint phase reads it with `cat "$MYSQL_ROOT_PASSWORD_FILE"`.

### Removing the workload

```bash
helm uninstall mysql -n mysql-test
kubectl delete pvc -l app.kubernetes.io/instance=mysql -n mysql-test   # Helm leaves the data PVC
kubectl delete namespace mysql-test
# Clean up Kasten restore point contents created during testing:
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=mysql-test
```

---

## Step 4 — Deploy the blueprint

```bash
kubectl apply -f blueprint.yaml         # installs into kasten-io
kubectl apply -f blueprintbinding.yaml  # installs into kasten-io
```

The `BlueprintBinding` matches any `apps/statefulsets` labelled
`app.kubernetes.io/name=mysql` **and** `app.kubernetes.io/component=primary` that does **not**
already carry a `kanister.kasten.io/blueprint` annotation. This binds all Bitnami MySQL primaries
fleet-wide while letting an explicit per-StatefulSet annotation override it.

To bind a single StatefulSet manually instead of using the binding:

```bash
kubectl annotate statefulset mysql -n mysql-test \
  kanister.kasten.io/blueprint=mysql-bitnami-standalone-blueprint
```

### Blueprint actions

| Action | When Kasten calls it | What it does |
|---|---|---|
| `backupPrehook` | Before PVC snapshots are initiated | Opens a background `mysql` session holding `FLUSH TABLES WITH READ LOCK` + `FLUSH LOGS` (kept alive with `DO SLEEP(3600)`); verifies the lock PID is alive. |
| `backupPosthook` | After PVC snapshots are ready | Kills the background lock session (releasing FTWRL); `UNLOCK TABLES` as a fallback. |
| `restorePosthook` | After PVC restore + StatefulSet manifest are back | `WaitV2` for pod `Ready`, then `SELECT 1` to confirm the DB is writable after InnoDB crash recovery. |

> **No `restorePrehook`**: Bitnami MySQL is a plain StatefulSet with no operator, so Kasten replaces
> the data PVC natively (scale down → swap → scale up). There is nothing to quiesce/tear-down before
> restore, so the `restorePrehook` — which is **not triggered by Kasten ≤ 8.5.x** anyway — is
> intentionally omitted here. (For operator-managed databases it would delete the CR; not applicable.)

---

## Step 5 — End-to-end test through Kasten

1. **Create a location profile** (S3/GCS/Azure Blob — an `Infra` profile is not enough, because the
   restore extracts the Kanister-compatible profile from the `RestorePointContent`). Confirm:

   ```bash
   kubectl get profile <name> -n kasten-io -o jsonpath='{.spec.type}'   # must be "Location"
   ```

2. **Create an on-demand backup policy** for `mysql-test` referencing that profile in
   `backupParameters.profile`, and trigger a `RunAction` (via the Kasten UI or API). Do **not** hand-craft
   a Kanister ActionSet — Kasten populates the ActionSet context differently.

3. **Watch the blueprint execute:**

   ```bash
   kubectl logs -n kasten-io -l component=executor --tail=10000 -f
   ```

   Expect `Lock acquired (PID=...)` in `backupPrehook` and `Lock process ... killed` /
   `Unquiesce complete` in `backupPosthook`. Confirm the RunAction status is `Complete`.

4. **Simulate data loss:**

   ```bash
   kubectl exec mysql-0 -n mysql-test -c mysql -- bash -c '
   RP=$(cat "$MYSQL_ROOT_PASSWORD_FILE")
   mysql -uroot -p"$RP" appdb -e "DROP TABLE pets;"'
   ```

5. **Restore** from the restore point (application namespace `mysql-test`):

   ```bash
   kubectl get restorepoint -n mysql-test

   kubectl create -f - --validate=false <<EOF
   apiVersion: actions.kio.kasten.io/v1alpha1
   kind: RestoreAction
   metadata:
     generateName: restore-
     namespace: mysql-test
   spec:
     subject:
       apiVersion: apps.kio.kasten.io/v1alpha1
       kind: RestorePoint
       name: <RESTORE_POINT_NAME>
       namespace: mysql-test
     targetNamespace: mysql-test
   EOF
   ```

6. **Verify recovery** — expect the original 5 rows:

   ```bash
   kubectl exec mysql-0 -n mysql-test -c mysql -- bash -c '
   RP=$(cat "$MYSQL_ROOT_PASSWORD_FILE")
   mysql -uroot -p"$RP" appdb -e "SELECT COUNT(*) AS row_count FROM pets; SELECT * FROM pets;"'
   ```

---

## Troubleshooting

```bash
# Kanister controller
kubectl logs -n kasten-io -l app=kanister-svc --tail=100
# Executor (primary debug target)
kubectl logs -n kasten-io -l component=executor --tail=10000 -f
```

If a `backupPrehook` fails, the lock session may linger; the next `backupPosthook` (or a manual
`UNLOCK TABLES`) clears it. To manually release a stuck lock:

```bash
kubectl exec mysql-0 -n mysql-test -c mysql -- bash -c '
[ -f /tmp/mysql-lock.pid ] && kill "$(cat /tmp/mysql-lock.pid)" 2>/dev/null; rm -f /tmp/mysql-lock.pid'
```
