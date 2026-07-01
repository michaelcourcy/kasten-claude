# Throwaway helper image for NON-DESTRUCTIVELY validating an HCP etcd backup.
#
# The stock quay.io/coreos/etcd image has no shell, so it can't be used to
# orchestrate a restore-and-read. This image adds bash + coreutils on top of
# ubi9-minimal and copies in the etcd SERVER binary plus etcdctl/etcdutl, so you
# can: restore a snapshot.db into a fresh data-dir (`etcdutl snapshot restore`),
# start a single-node etcd on it, and read keys back (`etcdctl get`) to prove the
# backup is genuinely restorable and contains real guest data.
#
# See README.md -> "Restore" -> "Validate backup quality non-destructively".
# This image is NOT used by the blueprint itself — only for ad-hoc verification.

FROM quay.io/coreos/etcd:v3.5.18 AS etcd-src

FROM registry.access.redhat.com/ubi9/ubi-minimal:latest
RUN microdnf install -y bash coreutils-single && microdnf clean all
COPY --from=etcd-src /usr/local/bin/etcd    /usr/local/bin/etcd
COPY --from=etcd-src /usr/local/bin/etcdctl /usr/local/bin/etcdctl
COPY --from=etcd-src /usr/local/bin/etcdutl /usr/local/bin/etcdutl
ENTRYPOINT ["/bin/bash", "-c"]
CMD ["sleep infinity"]
