# Build worker images

A worker image is an [aarch.science](https://aarch.science/) environment with
`globus-compute-endpoint` and the aegc worker added in the same Python environment. Workers run it
by digest.

!!! note "Only aarchsci and aarchbio bases"
    `aegc image build` builds only from aarchsci environments and aarchbio tool images, and
    `aegc deploy` only uses images recorded by it. Bringing your own arm64 image isn't supported
    yet; [#67](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/67) tracks it.

## Build one

Run from the root of the aegc source tree (or pass `--project-dir`):

```sh
aegc image build --env geospatial --base-tag 2026.11.13.154112
```

The steps:

1. Build the aegc wheel with `uv build`.
2. Log in to ECR and build `linux/arm64` with `docker buildx`, from
   `<account>.dkr.ecr.<region>.amazonaws.com/aegc-quay/aarchsci/<env>:<base-tag>`, the region's
   pull-through cache of `quay.io/aarchsci/<env>:<base-tag>`.
3. Inside the build: install `globus-compute-endpoint` from conda-forge with micromamba, then fail
   if any package that was already in the image changed version or disappeared, except `click`
   (`globus-compute-endpoint` pins an older one). Then install the aegc wheel, constrained to the
   installed packages, and run `pip check`.
4. Still inside the build: import `execute_task` and the worker, run the image's own
   `/opt/aarchsci/smoke.py` if it has one, and run `aegc-worker selftest`, which pushes a real packed
   task through `execute_task`.
5. Read the image's Python minor version.
6. Create the ECR repository `aegc/<env>` if needed (immutable tags, scan on push, and the
   [lifecycle policy](#old-images-and-in-use-tags)), push, and record the image at
   `/aegc/images/<env>` in SSM Parameter Store.

The tag has the form `py<M.m>-gce<v>-aegc<v>-b<base>-w<wheelhash>`, for example
`py3.14-gce4.21.0-aegc0.1.0-b2026.11.13.154112-w1a2b3c4d`. The wheel hash is the first 8 hex
digits of the SHA-256 of the aegc wheel, so a code change gets a new tag. If the tag already exists,
the build reuses the pushed image instead of pushing again.

Options:

- `--base-tag` must be an immutable tag (for aarchsci, `YYYY.MM.DD.HHMMSS`). `latest` is refused.
- `--gce-version` sets the `globus-compute-endpoint` version (default 4.21.0). Keep it compatible
  with the endpoint's.
- `--no-push` builds and tests locally, and records nothing.
- `--direct` pulls the base from quay.io instead of the ECR cache.
- `--source aarchbio` builds from `quay.io/aarchbio/<tool>` ([aarch.bio](https://aarch.bio/)) instead. This isn't validated in v0.1:
  aarchbio images don't pin a Python for worker use yet (gap G4 in
  [#16](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/16)).

## Use a new image

`aegc deploy` reads `/aegc/images/<env>` and puts the digest in the launch template. A deployment
keeps running the image it was deployed with, so after a build, redeploy:

```sh
aegc image build --env geospatial --base-tag <newer tag>
aegc deploy e2e --pool geospatial
```

The pool keeps its other options. New workers run the new image; running workers keep the old one
until they drain. Running endpoints pick up the redeploy within a minute.

If the new image has a different Python minor version, the pool name changes too (for example
`geospatial-py315`), and the redeploy creates a new pool. The old pool is kept, with a warning; pass
`--prune` to remove it.

## Old images and in-use tags

The repository's lifecycle policy keeps the 10 most recent images, removes untagged images after a
day, and never removes an image that a deployment uses. `aegc deploy` tags each image it pins
`inuse-<d>-<first 12 hex digits of the digest>` (adding a tag to the existing image; tags stay
immutable) and removes the deployment's older `inuse-` tags. A priority-1 lifecycle rule keeps every
image with an `inuse-` tag, however old. `aegc destroy` removes the deployment's tags, and the image
then ages out normally. The policy is re-applied to existing repositories on every build and deploy.

Protected images still count toward the 10, so many deployments on old images leave fewer slots for
new builds.

## Things to know

- **One image per env.** `/aegc/images/<env>` holds one record, so one Python version per env at a
  time. Building another overwrites the record.
- **Env names of at most 34 characters.** The pool name `<env>-py<NNN>` must fit in 40 characters.
  Every aarchsci env fits; the longest, `earth-observation`, gives `earth-observation-py314`.
- **Python must match the clients.** The aarchsci images run Python 3.14. Clients on another
  version can't use them.
- **Old images expire, unless a deployment uses them.** See
  [Old images and in-use tags](#old-images-and-in-use-tags).
- **Rebuild images from before aegc 0.2.** `aegc deploy` refuses them unless given
  `--allow-old-image`: their workers ignore spend limits, `aegc stop` and the lease.
- **Only aarchsci and aarchbio bases**, for now
  ([#67](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/67)).
- **Images are big.** Science images are often several GB, which shows in cold-start time and ECR
  storage. The pull-through cache keeps base pulls in-region.
- **Native arm64 builds.** On Apple Silicon the build is native. Elsewhere buildx needs QEMU, and
  the in-image tests run emulated.
- Repositories and image records stay after `aegc destroy` and `aegc unbootstrap`. Remove them with
  the AWS CLI or console if you no longer need them.
