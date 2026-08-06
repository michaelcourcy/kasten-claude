# Keeper / orchestrator image for the CP4D artifact-backup blueprint.
# Provides: cpdctl (CP4D logical export/import), kubectl (read cross-ns cred secret,
# manage export pods), plus jq / unzip / bash for the orchestration + atomic unzip.
#
# Base: debian slim. Runs as an arbitrary (OpenShift-assigned) UID — work dirs are
# group-writable so a random UID in group 0 can write.
FROM --platform=linux/amd64 debian:bookworm-slim

ARG CPDCTL_VERSION=v1.8.244
ARG KUBECTL_VERSION=v1.31.6

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl jq unzip bash tar gzip \
    && rm -rf /var/lib/apt/lists/*

# cpdctl (linux amd64) — the CP4D runtime CLI for project export/import
RUN curl -fsSL "https://github.com/IBM/cpdctl/releases/download/${CPDCTL_VERSION}/cpdctl_linux_amd64.tar.gz" \
      | tar -xz -C /usr/local/bin cpdctl \
    && chmod 0755 /usr/local/bin/cpdctl

# kubectl — read the cross-namespace credential secret; managed by the keeper SA RBAC
RUN curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
      -o /usr/local/bin/kubectl \
    && chmod 0755 /usr/local/bin/kubectl

# orchestration scripts (baked in so the blueprint stays tiny and the image is self-contained)
COPY cpdctl-login.sh export-project.sh import-project.sh orchestrate.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/cpdctl-login.sh /usr/local/bin/export-project.sh \
               /usr/local/bin/import-project.sh /usr/local/bin/orchestrate.sh

# cpdctl config is rendered here at runtime (world-writable /tmp => any OpenShift UID); never persisted
ENV CPDCONFIG=/tmp/.cpdctl-config.json

ENTRYPOINT ["/bin/bash"]
