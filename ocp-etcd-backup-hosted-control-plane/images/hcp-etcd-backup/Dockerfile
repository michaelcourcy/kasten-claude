# Keeper image for the Hosted Control Plane (HCP) etcd backup blueprint.
#
# Unlike the standalone OpenShift etcd backup (which runs the node's
# cluster-backup.sh over a hostPath), a hosted cluster's etcd is an ordinary
# StatefulSet (pods etcd-0/1/2) with PVCs on the management ("hub") cluster.
# The keeper does NOT touch the host: it `kubectl exec`s into a running etcd pod
# and runs the documented `etcdctl snapshot save` there (localhost:2379 + the
# etcd pod's own client certs under /etc/etcd/tls), then streams the .db file out
# onto the keeper's permanent PVC, which Kasten snapshots.
#
# So the keeper image only needs:
#   - kubectl   : exec into the hosted etcd pod and stream the snapshot out.
#   - etcdutl    : verify the snapshot on the keeper PVC (`snapshot status`).
#   - bash, coreutils/util-linux : streaming, `sync`, ls, etc.
#
# etcdctl is NOT required in this image — it runs inside the etcd pod, which
# already provides it. We still copy it in (harmless, matches the sibling
# ocp-etcd-backup image) so the keeper can also drive etcdctl directly if ever
# needed for ad-hoc inspection.

ARG ETCD_SOURCE_IMAGE=quay.io/coreos/etcd:v3.5.18
# kubectl is matched to the management-cluster Kubernetes version (OCP 4.18 = k8s 1.31).
ARG KUBECTL_VERSION=v1.31.6

FROM ${ETCD_SOURCE_IMAGE} AS etcd-src

FROM registry.access.redhat.com/ubi9/ubi-minimal:latest
ARG KUBECTL_VERSION

RUN microdnf update -y && \
    microdnf install -y bash ca-certificates tar gzip coreutils-single util-linux curl-minimal && \
    microdnf clean all

RUN curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
      -o /usr/local/bin/kubectl && \
    chmod 0755 /usr/local/bin/kubectl

COPY --from=etcd-src /usr/local/bin/etcdctl /usr/local/bin/etcdctl
COPY --from=etcd-src /usr/local/bin/etcdutl /usr/local/bin/etcdutl

RUN /usr/local/bin/etcdctl version && /usr/local/bin/etcdutl version && \
    /usr/local/bin/kubectl version --client

# The keeper just stays alive so the blueprint can KubeExec into it and so an
# admin can inspect the backup artifacts on the PVC.
ENTRYPOINT ["/bin/bash", "-c"]
CMD ["echo 'hcp-etcd-backup keeper ready'; sleep infinity"]
