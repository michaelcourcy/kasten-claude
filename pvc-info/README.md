# PVC Info Blueprint

## Purpose

Some teams configure Kasten backup policies to **exclude PVCs** from namespace backups
(e.g. when they rely on storage-level replication instead of Kasten snapshots). However,
they still want to preserve a record of which PVCs existed and which cloud volumes (PVs)
they were bound to at the time of backup.

This blueprint is invoked as a **Kasten policy preHook** before backup starts. It:
1. Queries all PVCs in the namespace and their bound PVs.
2. Formats the data as a human-readable YAML document.
3. Writes it into a ConfigMap named `pvc-info-snapshot` in the target namespace.

Because the ConfigMap is a namespace resource (not a PVC), it **is** captured in the
restore point even when PVC snapshots are excluded. Teams can inspect it after a restore
to identify the original volume handles and re-provision accordingly.

## Blueprint actions

| Action | Trigger | What it does |
|---|---|---|
| `backupPrehook` | Before backup policy runs (policy preHook) | Lists all PVCs + bound PVs, writes a YAML summary into ConfigMap `pvc-info-snapshot` in the target namespace |

## ConfigMap format

The ConfigMap contains a single key `pvc-info.yaml` whose value is a YAML document:

```yaml
# PVC and PV information snapshot
# Captured by Kasten backupPrehook policy hook.
# Use this to recover volume info when PVCs are excluded from the backup.
namespace: my-app
capturedAt: 2026-03-03T10:15:00Z
pvcs:
  - name: data-myapp-0
    storageClass: ebs-sc
    phase: Bound
    requestedStorage: 10Gi
    capacity: 10Gi
    accessModes: [ReadWriteOnce]
    boundPV: pvc-abc123de-...
    pv:
      name: pvc-abc123de-...
      capacity: 10Gi
      reclaimPolicy: Delete
      csi:
        driver: ebs.csi.aws.com
        volumeHandle: vol-0a1b2c3d4e5f6a7b8
```

## Prerequisites

- Kasten installed in `kasten-io` namespace.
- The image `michaelcourcy/kasten-tools:8.5.2` must be pullable from the cluster.
  It is the standard `gcr.io/kasten-images/kanister-tools:8.5.2` image with `kubectl`
  added. See [Dockerfile](../mariadb-community-operator-standalone/images/kasten-tools/Dockerfile)
  if you need to rebuild it.

## Deployment

Deploy the blueprint in the `kasten-io` namespace:

```bash
kubectl apply -f blueprint.yaml
```

Verify:
```bash
kubectl get blueprint pvc-info-blueprint -n kasten-io
```

## Attaching the blueprint to a Kasten policy

Create a policy that targets the namespace and sets the blueprint as a preHook:

```bash
kubectl apply -f - <<EOF
apiVersion: config.kio.kasten.io/v1alpha1
kind: Policy
metadata:
  name: my-app-backup
  namespace: kasten-io
spec:
  frequency: '@onDemand'
  selector:
    matchExpressions:
      - key: k10.kasten.io/appNamespace
        operator: In
        values:
          - my-app
  actions:
    - action: backup
      backupParameters:
        filters: {}
        hooks:
          preHook:
            blueprint: pvc-info-blueprint
            actionName: backupPrehook
EOF
```

In the Kasten UI: go to **Policies → Create Policy → Advanced Options → Kanister Actions**
and set the **Pre-Hook** blueprint to `pvc-info-blueprint` with action `backupPrehook`.

## Testing

### Deploy a workload with PVCs

```bash
kubectl create namespace pvc-info-test

kubectl apply -n pvc-info-test -f - <<EOF
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: test-app
spec:
  selector:
    matchLabels:
      app: test-app
  serviceName: test-app
  replicas: 1
  template:
    metadata:
      labels:
        app: test-app
    spec:
      containers:
        - name: app
          image: busybox
          command: ["sh", "-c", "while true; do sleep 3600; done"]
          volumeMounts:
            - name: data
              mountPath: /data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        storageClassName: ebs-sc
        resources:
          requests:
            storage: 1Gi
EOF

# Wait for the PVC to be bound
kubectl wait pvc/data-test-app-0 -n pvc-info-test --for=jsonpath='{.status.phase}'=Bound --timeout=120s
```

### Create the policy and trigger a RunAction

`{{ .Namespace.Name }}` is only populated when Kasten invokes the blueprint — a manually
created Kanister ActionSet does not set this variable. Always test through a Kasten RunAction.

```bash
kubectl apply -f - <<EOF
apiVersion: config.kio.kasten.io/v1alpha1
kind: Policy
metadata:
  name: pvc-info-test-backup
  namespace: kasten-io
spec:
  frequency: '@onDemand'
  selector:
    matchExpressions:
      - key: k10.kasten.io/appNamespace
        operator: In
        values:
          - pvc-info-test
  actions:
    - action: backup
      backupParameters:
        filters: {}
        hooks:
          preHook:
            blueprint: pvc-info-blueprint
            actionName: backupPrehook
EOF

kubectl create -f - <<EOF
apiVersion: actions.kio.kasten.io/v1alpha1
kind: RunAction
metadata:
  generateName: pvc-info-test-run-
  namespace: kasten-io
spec:
  subject:
    apiVersion: config.kio.kasten.io/v1alpha1
    kind: Policy
    name: pvc-info-test-backup
    namespace: kasten-io
EOF
```

Watch progress in the kanister-svc logs:
```bash
kubectl logs -n kasten-io -l app=kanister-svc --tail=200 -f
```

### Verify the ConfigMap

```bash
kubectl get configmap pvc-info-snapshot -n pvc-info-test
kubectl get configmap pvc-info-snapshot -n pvc-info-test \
  -o jsonpath='{.data.pvc-info\.yaml}'
```

Expected output: a YAML document listing the `data-test-app-0` PVC and its bound EBS volume.

### Cleanup

```bash
kubectl delete namespace pvc-info-test
kubectl delete policy pvc-info-test-backup -n kasten-io
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=pvc-info-test
```
