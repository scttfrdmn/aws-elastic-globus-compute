# Quickstart

This walks through the flow used for the live end-to-end run
([#15](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/15)): bootstrap a region,
build one worker image, deploy a pool, run a Globus Compute endpoint in Docker on your machine,
submit tasks, and tear the deployment down.

It creates AWS resources that cost money while workers run. Workers terminate themselves about
5 minutes after the queue empties; `aegc destroy` removes the rest. See [Cost](concepts/cost.md).

## Prerequisites

- **Docker with buildx.** Worker images are built for `linux/arm64` with `docker buildx build`, and
  the build runs tests inside the image. On Apple Silicon this is a native build. On an x86-64 host
  buildx needs QEMU emulation, which is much slower (and untested).
- **[uv](https://docs.astral.sh/uv/).** `aegc image build` and `aegc endpoint build` build the aegc
  wheel with `uv build`.
- **AWS credentials** for an account and region with a default VPC. aegc places workers in the
  default VPC's default subnets. The examples use `us-west-2`. The credentials need the
  [deployer](reference/iam.md#deployer) and [builder](reference/iam.md#builder) policies; an
  administrator profile also works. Check your Spot vCPU quota for standard instances
  (`max` workers × vCPUs per worker must fit).
- **A Globus account**, for `aegc endpoint login`.
- **A clone of this repository.** aegc isn't on PyPI, and the build commands build the wheel from
  the source tree (`--project-dir`, default `.`). Run the commands below from the repository root.

```sh
git clone https://github.com/scttfrdmn/aws-elastic-globus-compute
cd aws-elastic-globus-compute
uv venv .venv
uv pip install -e '.[cli]'
source .venv/bin/activate

export AWS_PROFILE=<your profile> AWS_DEFAULT_REGION=us-west-2
```

`--profile` and `--region` are global options and go before the command
(`aegc --region us-west-2 status e2e`). Without them aegc uses the environment's profile and the
profile's region (boto3 reads `AWS_DEFAULT_REGION`, not `AWS_REGION`).

## 1. Bootstrap the region

```sh
aegc bootstrap
```

This creates the region-wide resources once: the payload bucket `aegc-<account>-<region>`, an ECR
pull-through cache rule `aegc-quay` for quay.io with its creation template and role, and the
parameter `/aegc/bootstrap`. It's idempotent; run it again at any time. `--dry-run` shows what it
would do.

## 2. Build a worker image

```sh
aegc image build --env geospatial --base-tag 2026.11.13.154112
```

This layers `globus-compute-endpoint` and the aegc worker onto
`quay.io/aarchsci/geospatial:2026.11.13.154112` (pulled through the ECR cache), runs the image's
smoke test and `aegc-worker selftest`, reads the image's Python version, pushes it to ECR as
`aegc/geospatial`, and records the pinned digest at `/aegc/images/geospatial`. That [aarchsci](https://aarch.science/) build
runs Python 3.14.

Use an immutable base tag, never `latest`. See [Worker images](how-to/images.md).

## 3. Deploy a pool

```sh
aegc deploy e2e --pool geospatial:max=3
```

`e2e` is the deployment name. This creates the worker IAM role, a no-ingress security group, the
log group, and for the pool `geospatial-py314` a task queue with a dead-letter queue, a launch
template and a Spot Auto Scaling group with up to 3 workers. The group starts at zero instances.
See [Configuration](how-to/configuration.md) for the other pool options.

## 4. Run the endpoint

The endpoint runs in a container on your machine, with `~/.aws` mounted read-only and the Globus
login kept in the Docker volume `aegc-endpoint-home`.

```sh
# A shell function, so this works in zsh (the macOS default) as well as bash.
ep() { aegc endpoint --name aegc-e2e --deployment e2e --env geospatial "$@"; }
ep build         # builds the local image aegc-endpoint (Python 3.14)
ep login         # interactive Globus login; follow the URL it prints
ep configure     # writes the endpoint config for deployment e2e, env geospatial
ep start         # prints: started aegc-endpoint-aegc-e2e; endpoint id <uuid>
```

Note the endpoint id. `start` runs the container with `--restart unless-stopped`.

## 5. Submit tasks

```sh
ep test
```

`test` submits from inside the endpoint container (Python 3.14): one task that reports the worker's
host and Python, then 10 squares (`-n` changes the count). The first result waits for a cold start;
in the live run that took 39 s from zero workers.

From your own code, use the Globus Compute SDK with the endpoint id. **Your Python minor version
must match a worker image** (3.14 here); a 3.13 client gets an error instead of a result. See
[Troubleshooting](how-to/troubleshooting.md#python-version-mismatch).

```python
from globus_compute_sdk import Executor


def where():
    import platform

    return platform.node(), platform.python_version()


with Executor(
    endpoint_id="<uuid>", amqp_port=5671, user_endpoint_config={"env": "geospatial"}
) as ex:
    print(ex.submit(where).result())
```

`amqp_port=5671` avoids dropped AMQPS connections on port 443
([globus/globus-compute#2232](https://github.com/globus/globus-compute/issues/2232)).

## 6. Watch it

```sh
aegc status e2e
```

This shows, per pool, the queued, running and dead-lettered tasks, the workers in service and
desired, the last scaling activity, and any results waiting in result queues. Workers leave the
group about 5 minutes after they go idle; in the live run the pool was back to zero 5.5 minutes
after the last task.

## 7. Tear down

```sh
ep stop
aegc destroy e2e          # dry run: lists what would be deleted
aegc destroy e2e --yes    # deletes it
```

`destroy` refuses while tasks are queued or running unless you add `--force`. It deletes only
resources tagged as belonging to `e2e`.

The region bootstrap and the `aegc/geospatial` image stay, so the next deployment is quick. To
remove the bootstrap as well, destroy every deployment first, then run `aegc unbootstrap` (a dry run)
and `aegc unbootstrap --yes`. See [Operations](how-to/operations.md#removing-everything).
