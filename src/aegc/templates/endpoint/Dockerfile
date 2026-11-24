# syntax=docker/dockerfile:1
# Globus Compute endpoint with AWSEngine. Built by `aegc endpoint build`.
# The endpoint process never deserializes user functions (it forwards packed tasks), so its
# Python version needn't match the users'; workers' images must (see `aegc image build`).
# PYTHON matters only for `aegc endpoint test`, which submits from this container, so it defaults
# to the Python of most aarchsci images.
ARG PYTHON=3.14
FROM python:${PYTHON}-slim
ARG GCE_VERSION
COPY dist/ /tmp/aegc-dist/
RUN pip install --no-cache-dir "globus-compute-endpoint==${GCE_VERSION}" /tmp/aegc-dist/*.whl \
 && rm -rf /tmp/aegc-dist \
 && python -c "import globus_compute_endpoint.engines as e; print('registered:', e.AWSEngine.__name__)"
# Single-user by default (uid 1000). A multi-user endpoint (#62) runs this image as root with a
# few capabilities (`aegc endpoint start` passes --user 0); its entrypoint adds the mapped users.
RUN useradd --create-home --uid 1000 gc && install -d -o gc -g gc /home/gc/.globus_compute
USER gc
WORKDIR /home/gc
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
