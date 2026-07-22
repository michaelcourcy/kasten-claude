# kasten-tools — kanister-tools + kubectl + jq
#
# Base: gcr.io/kasten-images/kanister-tools (Kasten's standard tool image, RHEL-based)
# Added: kubectl (cross-namespace cluster operations)
#        jq (JSON parsing of Elasticsearch API responses)
#        tar, gzip (already present in base — re-stated for clarity)
#
# Used by KubeTask phases that need kubectl (cross-namespace operations)
# and curl + jq (Elasticsearch HTTP API calls and response parsing).
# KubeTask pods using this image MUST run in the kasten-io namespace so they
# inherit the Kasten service account with cross-namespace RBAC permissions.
#
# Published as: michaelcourcy/kasten-tools:<kasten-version>
# Build and push:
#   docker buildx build --platform linux/amd64 \
#     --build-arg KASTEN_VERSION=8.5.2 \
#     -t michaelcourcy/kasten-tools:8.5.2 --push .

ARG KASTEN_VERSION=8.5.2
FROM gcr.io/kasten-images/kanister-tools:${KASTEN_VERSION}

ARG KUBECTL_VERSION=1.32.0

USER root
RUN microdnf install -y tar gzip jq && microdnf clean all \
    && curl -LO "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl
USER 1001

ENTRYPOINT ["/bin/sh"]
