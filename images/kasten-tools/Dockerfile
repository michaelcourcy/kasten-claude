# kasten-tools — kanister-tools + kubectl + jq (shared tool image)
#
# This is the SINGLE canonical Dockerfile for the michaelcourcy/kasten-tools
# image reused by many blueprints in this repo. Do not copy it into individual
# blueprint folders — reference this file from each blueprint README instead.
#
# Base: gcr.io/kasten-images/kanister-tools (Kasten's standard tool image, RHEL-based)
# Added: kubectl (cross-namespace cluster operations)
#        jq     (JSON parsing of database / HTTP API responses)
#        tar, gzip (already present in base — re-stated for clarity)
# The base already ships curl.
#
# KubeTask pods using this image MUST run in the kasten-io namespace so they
# inherit the Kasten service account, which has cross-namespace RBAC.
#
# Published as: michaelcourcy/kasten-tools:<kasten-version>
# The image tag MUST match the Kasten version the blueprint was tested against
# (detect with `helm ls -n kasten-io`). Build one tag per tested version:
#
#   docker buildx build --platform linux/amd64 \
#     --build-arg KASTEN_VERSION=<kasten-version> \
#     --build-arg KUBECTL_VERSION=<k8s-version> \
#     -t michaelcourcy/kasten-tools:<kasten-version> --push .
#
# The ARG defaults below track the most recent tested version; always pass
# --build-arg explicitly so the tag matches the target cluster.

ARG KASTEN_VERSION=8.5.13
FROM gcr.io/kasten-images/kanister-tools:${KASTEN_VERSION}

ARG KUBECTL_VERSION=1.32.0

USER root
RUN microdnf install -y tar gzip jq && microdnf clean all \
    && curl -LO "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl
USER 1001

ENTRYPOINT ["/bin/sh"]
