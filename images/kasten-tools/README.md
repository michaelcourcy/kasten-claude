# kasten-tools — shared tool image

`michaelcourcy/kasten-tools` is Kasten's standard `gcr.io/kasten-images/kanister-tools`
image plus `kubectl` and `jq`. It is used by the `KubeTask` / `KubeExec` phases of many
blueprints in this repo (CNPG, CockroachDB, Couchbase, Elasticsearch, Grafana Mimir,
MariaDB, MySQL, PSMDB, PVC-info, …).

This directory holds the **single canonical Dockerfile** for that image. Individual
blueprint folders reference this file rather than keeping their own copy, so there is
one source of truth and no version drift.

## What it adds

| Tool | Why |
|---|---|
| `kubectl` | cross-namespace cluster operations (the base image has no `kubectl`) |
| `jq` | parse JSON from database / HTTP API responses |
| `tar`, `gzip` | present in the base already; installed explicitly for clarity |
| `curl` | already in the base image |

`KubeTask` pods using this image **must run in the `kasten-io` namespace** so they
inherit the Kasten service account and its cross-namespace RBAC.

## Build and push

The image **tag must match the Kasten version** the blueprint was tested against
(detect with `helm ls -n kasten-io`). Build one tag per version:

```bash
cd images/kasten-tools
docker buildx build --platform linux/amd64 \
  --build-arg KASTEN_VERSION=8.5.13 \
  --build-arg KUBECTL_VERSION=1.32.0 \
  -t michaelcourcy/kasten-tools:8.5.13 --push .
```

`KUBECTL_VERSION` should match the Kubernetes version in the consuming blueprint's
versions table.

## Published tags

Tags currently referenced by blueprints in this repo:

| Tag | Used by |
|---|---|
| `8.5.2` | cnpg, cnpg-barman, cnpg-barman-cloud, cockroachdb, couchbase-operator, elasticsearch-eck, elasticsearch-eck-minio, mariadb, postgres-windows, psmdb-*, pvc-info |
| `8.5.13` | grafana-mimir |
