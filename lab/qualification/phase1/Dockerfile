# CUDA 12.9.2 devel on Ubuntu 24.04. APT repositories remain live.
FROM nvidia/cuda@sha256:16656a1ef115bca9e1f820c6349876f1486d2b3c9a0e615773799fe402960dc5

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      cmake \
      ninja-build \
      python3 \
      libyaml-cpp-dev \
      pkg-config && \
    rm -rf /var/lib/apt/lists/*
