# RHEL 8 user space (UBI 8.10) + Python 3.11 from AppStream (open-slide-py needs 3.10+) + OpenCode 1.18.32 (glibc build).
FROM registry.access.redhat.com/ubi8/ubi:latest
RUN dnf -y -q install python3.11 && dnf clean all && ln -sf /usr/bin/python3.11 /usr/local/bin/python3
COPY opencode /usr/local/bin/opencode
