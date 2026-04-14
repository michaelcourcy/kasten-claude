# ConfigMap Echo Blueprint

Minimal demo blueprint that echoes every label key/value pair from a StatefulSet during `backupPrehook`, using a Go template range loop over `.Object.metadata.labels`.

## Deploy the blueprint

```bash
kubectl apply -f blueprint.yaml
```

## Create a test namespace and StatefulSet

```bash
kubectl create namespace sts-echo-test
```

```yaml
cat <<EOF | kubectl replace -f -
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: demo
  namespace: sts-echo-test
  labels:
    app: demo
    env: test
    tier: backend
  annotations:
    kanister.kasten.io/blueprint: sts-echo
spec:
  selector:
    matchLabels:
      app: demo
  serviceName: demo
  replicas: 1
  template:
    metadata:
      labels:
        app: demo
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
        resources:
          requests:
            storage: 1Gi
EOF
```



The blueprint annotation is on the StatefulSet itself (`kanister.kasten.io/blueprint: configmap-echo`). No separate `kubectl annotate` step is needed.

## What the blueprint does

During `backupPrehook` Kasten triggers a `KubeTask` pod that runs a shell script rendered from this Go template:

```
{{- range $key, $value := .Object.metadata.labels }}
echo "Label: {{ $key }}={{ $value }}"
{{- end }}
```

With the labels above the rendered script will be:

```sh
echo "Label: app=demo"
echo "Label: env=test"
echo "Label: tier=backend"
```

## Clean up

```bash
kubectl delete namespace sts-echo-test
kubectl delete blueprint configmap-echo -n kasten-io
kubectl delete restorepointcontent -l k10.kasten.io/appNamespace=sts-echo-test
```
