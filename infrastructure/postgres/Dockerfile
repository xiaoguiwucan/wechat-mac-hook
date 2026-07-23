FROM groonga/pgroonga:latest-debian-17

ARG PGVECTOR_VERSION=v0.8.2

# Docker Desktop on Apple Silicon may expose build-time CPU features that are
# unavailable to the runtime VM. Disable pgvector's -march=native.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates git postgresql-server-dev-17 \
    && git clone --depth 1 --branch "${PGVECTOR_VERSION}" \
       https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && make -C /tmp/pgvector OPTFLAGS="" -j"$(nproc)" \
    && make -C /tmp/pgvector OPTFLAGS="" install \
    && rm -rf /tmp/pgvector /var/lib/apt/lists/* \
    && apt-get purge -y --auto-remove build-essential git postgresql-server-dev-17
