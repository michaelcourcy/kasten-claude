# kasten-tools — kanister-tools + kubectl
#
# Base: gcr.io/kasten-images/kanister-tools (Kasten's standard tool image)
# Added: kubectl (not included in the base image)
#
# Used by KubeTask phases in blueprints that need to run kubectl commands
# against the cluster. KubeTask pods using this image MUST run in the
# kasten-io namespace so they inherit the Kasten service account, which
# has the RBAC permissions to manage resources across all namespaces.
#
# Published as: michaelcourcy/kasten-tools:<kasten-version>
# Build and push:
#   docker build --build-arg KASTEN_VERSION=8.5.2 -t michaelcourcy/kasten-tools:8.5.2 .
#   docker push michaelcourcy/kasten-tools:8.5.2

ARG KASTEN_VERSION=8.5.2
FROM gcr.io/kasten-images/kanister-tools:${KASTEN_VERSION}

ARG KUBECTL_VERSION=1.32.0

USER root
RUN microdnf install -y tar gzip && microdnf clean all \
    && curl -LO "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl
USER 1001

ENTRYPOINT ["/bin/sh"]
