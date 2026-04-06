# couchbase-backup-tool
#
# Adds kubectl and jq to couchbase/server:7.6.6.
# couchbase/server already contains cbbackupmgr at /opt/couchbase/bin/cbbackupmgr.
# This image is used by KubeTask phases in the Couchbase Kasten blueprint.
#
# Build:
#   docker buildx build --platform linux/amd64 \
#     -t michaelcourcy/couchbase-backup-tool:7.6.6 \
#     --push .

FROM couchbase/server:7.6.6

USER root

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl jq ca-certificates && \
    # Install kubectl
    KUBECTL_VERSION=$(curl -fsSL https://dl.k8s.io/release/stable.txt) && \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
      -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl && \
    # Cleanup
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Add Couchbase binaries to PATH so cbbackupmgr is callable without full path
ENV PATH="/opt/couchbase/bin:${PATH}"

CMD ["/bin/bash"]
