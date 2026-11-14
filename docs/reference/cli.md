# CLI

`aegc` manages AWS resources, worker images and the local endpoint. `aegc-worker` runs inside worker
images. Both are installed with the package (`uv pip install -e '.[cli]'` from the source tree).

```
aegc [-h] [--version] [--profile PROFILE] [--region REGION] COMMAND ...
```

| Global option | Meaning |
|---|---|
| `--profile PROFILE` | AWS profile (default: the environment's) |
| `--region REGION` | AWS region (default: the profile's) |
| `--version` | Print `aegc 0.1.0` |

Global options go **before** the command: `aegc --region us-west-2 status e2e`.

Commands that delete are dry runs unless given `--yes`. Commands that create accept `--dry-run`.
Each prints a report, one line per resource, with an outcome: `created`, `exists`, `updated`,
`deleted`, `would-create`, `would-delete` or `kept`.

## aegc bootstrap

```
aegc bootstrap [--dry-run]
```

Create or reconcile the region-wide resources: the payload bucket, the `aegc-quay` pull-through
cache rule with its creation template and role, and `/aegc/bootstrap`. Run once per region; safe to
repeat. Refuses to use look-alike resources that don't carry aegc's tags.

| Option | Meaning |
|---|---|
| `--dry-run` | Report what would be created; change nothing |

## aegc unbootstrap

```
aegc unbootstrap [--yes]
```

Remove the region-wide resources. Refuses while any deployment exists in the region. Doesn't remove
worker image repositories or image records.

| Option | Meaning |
|---|---|
| `--yes` | Actually delete (default: dry run) |

## aegc image build

```
aegc image build --env ENV --base-tag BASE_TAG [--source {aarchsci,aarchbio}]
                 [--gce-version GCE_VERSION] [--no-push] [--direct] [--project-dir PROJECT_DIR]
```

Build, test, push and record a worker image for an environment. See
[Worker images](../how-to/images.md).

| Option | Default | Meaning |
|---|---|---|
| `--env ENV` | required | aarchsci env or aarchbio tool, e.g. `geospatial` |
| `--base-tag BASE_TAG` | required | Immutable base tag, e.g. `2026.11.13.154112` |
| `--source` | `aarchsci` | `aarchsci` or `aarchbio` |
| `--gce-version` | `4.21.0` | `globus-compute-endpoint` version |
| `--no-push` | off | Build and test locally only |
| `--direct` | off | Pull the base from quay.io, not the ECR cache |
| `--project-dir` | `.` | aegc source tree to build the wheel from |

Prints the pinned image (`<registry>/aegc/<env>@sha256:…`), or the local tag with `--no-push`.

## aegc deploy

```
aegc deploy DEPLOYMENT --pool POOL [--pool POOL ...] [--dry-run]
```

Create or update a deployment and its worker pools. Pass every pool on each run.

| Argument / option | Meaning |
|---|---|
| `DEPLOYMENT` | Deployment name: 1–20 characters of `[a-z0-9-]`, starting with a letter |
| `--pool POOL` | `env[:max=N,min=N,vcpu=LO-HI,mem=MiB,slots=N|auto,idle=S,on-demand]`; repeatable, at least one |
| `--dry-run` | Report what would change; change nothing |

Pool options:

| Option | Default | Meaning |
|---|---|---|
| `max=N` | 4 | Maximum workers |
| `min=N` | 0 | Minimum workers |
| `vcpu=LO-HI` or `vcpu=N` | 2-16 | vCPU range per instance |
| `mem=MiB` | 4096 | Minimum memory per instance |
| `slots=N` or `slots=auto` | `auto` | Concurrent tasks per worker; `auto` is one per vCPU |
| `idle=S` | 300 | Idle seconds before a worker leaves the group |
| `on-demand` | Spot | Use on-demand instances |

The env must have an image recorded by `aegc image build`. The pool is named
`<env>-py<NNN>` from that image's Python version.

## aegc destroy

```
aegc destroy DEPLOYMENT [--yes] [--force]
```

Delete everything aegc created for a deployment. See
[Operations](../how-to/operations.md#destroy-a-deployment).

| Option | Meaning |
|---|---|
| `--yes` | Actually delete (default: dry run) |
| `--force` | Proceed even with tasks queued or running |

## aegc status

```
aegc status DEPLOYMENT
```

Per pool: tasks queued, running and dead-lettered; workers in service, desired and maximum; the last
scaling activity. Then each result queue and the results waiting in it.

## aegc reap

```
aegc reap DEPLOYMENT [--yes]
```

Delete the deployment's result queues that are empty and haven't been modified for 14 days.

| Option | Meaning |
|---|---|
| `--yes` | Actually delete (default: dry run) |

## aegc iam-policy

```
aegc iam-policy {worker,operator,deployer} [--deployment DEPLOYMENT] [--account ACCOUNT]
```

Print an IAM policy document as JSON. See [IAM policies](iam.md).

| Argument / option | Default | Meaning |
|---|---|---|
| `worker` | | For worker instances |
| `operator` | | For the endpoint's credentials |
| `deployer` | | For `aegc bootstrap`, `deploy` and `destroy` |
| `--deployment` | `default` | Deployment the policy is scoped to (worker, operator) |
| `--account` | the caller's | AWS account id |

With both `--region` and `--account` given, it makes no AWS calls.

## aegc endpoint

```
aegc endpoint {build,login,configure,start,stop,logs,test,id}
              [--name NAME] [--deployment DEPLOYMENT] [--env ENV] [-n N]
              [--project-dir PROJECT_DIR] [--python PYTHON]
```

Run a Globus Compute endpoint with `AWSEngine` in Docker.

| Subcommand | What it does |
|---|---|
| `build` | Build the local image `aegc-endpoint` from `python:<PYTHON>-slim`, `globus-compute-endpoint` 4.21.0 and the aegc wheel |
| `login` | Run `globus-compute-endpoint login` interactively; the login is stored in the volume `aegc-endpoint-home` |
| `configure` | Create the endpoint if needed and write its template, schema, environment and `amqp_port: 5671`; needs `--env` and a region |
| `start` | Start the container `aegc-endpoint-<name>` (`--restart unless-stopped`) and print the endpoint id, waiting up to 90 s for a first registration |
| `stop` | Stop the endpoint inside the container, then stop and remove the container |
| `logs` | Follow the container's log |
| `test` | Submit a probe task and `-n` squares from inside the container |
| `id` | Print the endpoint id |

| Option | Default | Meaning |
|---|---|---|
| `--name` | `aegc` | Globus Compute endpoint name |
| `--deployment` | `default` | aegc deployment the engine uses (configure) |
| `--env` | | Default science environment (configure); env to test (test) |
| `-n` | 10 | Tasks to submit (test) |
| `--project-dir` | `.` | aegc source tree (build) |
| `--python` | `3.14` | Python in the endpoint image; `test` submits with it (build) |

The container mounts `~/.aws` read-only and gets `AWS_PROFILE` (from `--profile` or the
environment), `AWS_REGION` and `AWS_DEFAULT_REGION`.

## aegc-worker

Runs inside worker images; you don't normally call it yourself.

| Command | What it does |
|---|---|
| `aegc-worker run` | Claim and run tasks until drained. Reads its configuration from `AEGC_WORKER_CONFIG_B64` (set by the user data). SIGTERM releases in-flight tasks and exits; SIGUSR1 simulates a Spot interruption |
| `aegc-worker selftest` | Run a probe task through `execute_task` in this image's Python; used by `aegc image build` |
