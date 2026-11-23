# syntax=docker/dockerfile:1
# aegc worker image: an aarchsci/aarchbio science environment plus globus-compute-endpoint and
# aegc-worker in the SAME Python environment. Rendered and built by `aegc image build`.
ARG BASE
FROM ${BASE}
ARG GCE_VERSION
ARG AEGC_ALLOW_CHANGE=click
USER root
COPY aegc_env_guard.py /opt/aegc/aegc_env_guard.py
COPY dist/ /opt/aegc/dist/
# 1. globus-compute-endpoint from conda-forge, so conda keeps the environment consistent (spike #2).
# 2. The guard fails the build if any package that was already there changed, except the allowlist
#    (globus-compute-endpoint pins click<8.4; the images ship 8.5).
# 3. aegc itself from its wheel, constrained to what's now installed.
RUN micromamba list -n base --json > /opt/aegc/base.json \
 && micromamba install -y -n base -c conda-forge "globus-compute-endpoint=${GCE_VERSION}" \
 && micromamba list -n base --json > /opt/aegc/worker.json \
 && /opt/conda/bin/python /opt/aegc/aegc_env_guard.py /opt/aegc/base.json /opt/aegc/worker.json "${AEGC_ALLOW_CHANGE}" \
 && /opt/conda/bin/python -m pip list --format=freeze \
      | grep -viE "^($(echo "${AEGC_ALLOW_CHANGE}" | tr ',' '|'))==" > /opt/aegc/constraints.txt \
 && /opt/conda/bin/python -m pip install --no-cache-dir -c /opt/aegc/constraints.txt /opt/aegc/dist/*.whl \
 && /opt/conda/bin/python -m pip check \
 && micromamba clean --all --yes && rm -rf /opt/conda/pkgs /opt/aegc/dist
USER mambauser
RUN /usr/local/bin/_entrypoint.sh python -c "from globus_compute_endpoint.engines.helper import execute_task; import aegc.worker.agent" \
 && if [ -f /opt/aarchsci/smoke.py ]; then /usr/local/bin/_entrypoint.sh python /opt/aarchsci/smoke.py; fi \
 && /usr/local/bin/_entrypoint.sh aegc-worker selftest
CMD ["aegc-worker", "run"]
