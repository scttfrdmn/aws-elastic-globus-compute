# The base for the 1000 Genomes tutorial's worker image: Python 3.14 and bcftools, nothing
# aegc-specific (`aegc image build --base` adds globus-compute-endpoint and the worker).
# The digest is python:3.14-slim on 2026-11-22; `docker buildx imagetools inspect
# python:3.14-slim | grep Digest` gives the current one.
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
RUN apt-get update \
 && apt-get install -y --no-install-recommends bcftools tabix ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && bcftools --version | head -1 \
 && bcftools plugin -l | grep -qx fill-tags
