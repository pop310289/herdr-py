# RHEL 8 user space (UBI 8.10) + OpenCode 1.18.32 (glibc build) for the P23 runs. python3 is RHEL 8's platform-python 3.6.
# Build with bench/p23/build-image.sh (it copies the OpenCode binary into the build context).
FROM registry.access.redhat.com/ubi8/ubi:latest
COPY opencode /usr/local/bin/opencode
RUN ln -s /usr/libexec/platform-python /usr/local/bin/python3
