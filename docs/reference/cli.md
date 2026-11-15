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
| `--version` | Print the version, e.g. `aegc 0.1.0` |

Global options go **before** the command: `aegc --region us-west-2 status e2e`.

Commands that delete are dry runs unless given `--yes`. Commands that create accept `--dry-run`.
Each prints a report, one line per resource, with an outcome: `created`, `exists`, `updated`,
`deleted`, `would-create`, `would-update`, `would-delete`, `kept` or `remaining`.

| Command | What it does |
|---|---|
| [`bootstrap`](#aegc-bootstrap) | Create the region-wide resources (once per region) |
| [`unbootstrap`](#aegc-unbootstrap) | Remove the region-wide resources (dry run by default) |
| [`image build`](#aegc-image-build) | Build, push and record a worker image for an environment |
| [`deploy`](#aegc-deploy) | Create or update a deployment and its worker pools |
| [`stop`](#aegc-stop) | Stop a deployment's spending now (kill switch) |
| [`resume`](#aegc-resume) | Resume a stopped, capped or expired deployment |
| [`limits`](#aegc-limits) | Show or change a deployment's spend limits |
| [`cost`](#aegc-cost) | Estimated spend from the cost ledger |
| [`destroy`](#aegc-destroy) | Delete a deployment (dry run by default) |
| [`footprint`](#aegc-footprint) | List what exists for a deployment: owned resources and untagged look-alikes |
| [`status`](#aegc-status) | State, spend, limits, lease, pools, queues, workers and waiting results |
| [`reap`](#aegc-reap) | Delete idle result queues of finished endpoint processes |
| [`iam-policy`](#aegc-iam-policy) | Print an IAM policy document for one of aegc's roles |
| [`endpoint`](#aegc-endpoint) | Run a Globus Compute endpoint with `AWSEngine` in Docker |

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
aegc deploy DEPLOYMENT --pool POOL [--pool POOL ...]
            [--max-usd-day N|none] [--max-usd-month N|none] [--max-instance-hours-day N|none]
            [--on-cap {fail,kill}] [--lease DURATION] [--ttl DURATION]
            [--prune] [--yes] [--force] [--allow-old-image] [--dry-run]
```

Create or update a deployment and its worker pools. On a redeploy, pools and options you don't give
keep their last values. See [Configure pools](../how-to/configuration.md).

| Argument / option | Meaning |
|---|---|
| `DEPLOYMENT` | Deployment name: 1–20 characters of `[a-z0-9-]`, starting with a letter |
| `--pool POOL` | `env[:option=value,...]` (below); repeatable, at least one |
| `--max-usd-day N|none` | Cap on estimated $ per UTC day |
| `--max-usd-month N|none` | Cap on estimated $ per UTC month |
| `--max-instance-hours-day N|none` | Cap on instance-hours per UTC day |
| `--on-cap {fail,kill}` | When a cap trips, let running tasks finish (`fail`, the default) or fail them (`kill`) |
| `--lease DURATION` | Freeze the deployment if no endpoint renews it for this long (default `48h`, at least `3h`; `none` disables); running endpoints renew hourly |
| `--ttl DURATION` | Freeze the deployment this long from now, renewed or not (e.g. `7d`; `none` clears) |
| `--prune` | Remove pools not listed (a dry run of that part unless `--yes`) |
| `--yes` | With `--prune`: actually remove them |
| `--force` | `--prune` even with tasks queued |
| `--allow-old-image` | Deploy images built before aegc 0.2 (their workers ignore limits and stop) |
| `--dry-run` | Report what would change; change nothing |

Durations are `<N>m`, `<N>h` or `<N>d`. Limits, lease and TTL are kept across redeploys unless
given again. Without any limit, `deploy` prints a note. While the deployment isn't running, a
redeploy keeps group minimums at 0. `deploy` refuses an image built with aegc before 0.2 unless
`--allow-old-image` is given.

Pool options:

| Option | Default | Meaning |
|---|---|---|
| `max_vcpus=N` | 8 | Maximum vCPUs in the pool |
| `min_vcpus=N` | 0 | Minimum vCPUs in the pool |
| `vcpu=LO-HI` or `vcpu=N` | 2-8 | vCPU range per instance; HI at most `max_vcpus` (the default HI is lowered to fit) |
| `mem=MiB` | 4096 | Minimum memory per instance |
| `slots=N` or `slots=auto` | `auto` | Concurrent tasks per worker; `auto` is one per vCPU |
| `idle=S` | 300 | Idle seconds before a worker leaves the group |
| `disk=GiB` | 30 | Root gp3 volume size (30–16384) |
| `throughput=MiBps` | 125 | Root volume throughput (125–1000) |
| `walltime=S` | 39,600 | Default walltime for the pool's tasks |
| `max_walltime=S` | 41,400 | Most walltime a task may request |
| `lifetime=S` | 86,400 | Seconds (3,600–259,200) after which a worker stops claiming |
| `on-demand` | Spot | Use on-demand instances |
| `spot` | Spot | Use Spot instances (undoes `on-demand`) |
| `max=N`, `min=N` | | v0.1 spelling, in workers: converted to `max_vcpus`/`min_vcpus` (× `vcpu` LO) with a warning |

The env must have an image recorded by `aegc image build`. The pool is named
`<env>-py<NNN>` from that image's Python version.

## aegc stop

```
aegc stop DEPLOYMENT [--hard] [--yes]
```

Stop a deployment's spending now: set its state to `stopped` and every group's minimum to 0.
Queued tasks fail with `AEGC_STOPPED`, running tasks finish, and workers leave their groups. See
[Operations](../how-to/operations.md#stop-and-resume).

| Option | Meaning |
|---|---|
| `--hard` | Also fail running tasks and terminate workers after 2 minutes (needs `--yes`) |
| `--yes` | Confirm `--hard` |

## aegc resume

```
aegc resume DEPLOYMENT
```

Resume a stopped, capped or expired deployment: set its state to `running`, restore each group's
minimum, and push the lease forward.

## aegc limits

```
aegc limits DEPLOYMENT [--max-usd-day N|none] [--max-usd-month N|none]
            [--max-instance-hours-day N|none] [--on-cap {fail,kill}] [--resume]
```

Show or change a deployment's spend limits. With no options it prints them. Running endpoints pick
up a change within a minute. `--resume` refuses while spend is still over a limit, and needs read
access to the cost ledger. See [Spend limits](../concepts/cost.md#spend-limits).

| Option | Meaning |
|---|---|
| `--max-usd-day N|none` | Cap on estimated $ per UTC day |
| `--max-usd-month N|none` | Cap on estimated $ per UTC month |
| `--max-instance-hours-day N|none` | Cap on instance-hours per UTC day |
| `--on-cap {fail,kill}` | When a cap trips, let running tasks finish (`fail`, the default) or fail them (`kill`) |
| `--resume` | Also end a capped state now |

## aegc cost

```
aegc cost DEPLOYMENT [--since SINCE] [--by {pool,type}] [--actual]
```

Estimated spend from the cost ledger. See [Cost ledger](../concepts/cost.md#cost-ledger).

| Option | Default | Meaning |
|---|---|---|
| `--since` | `today` | `today`, `month`, or a duration such as `24h`, `7d` or `90m` |
| `--by` | `pool` | Break the total down by `pool` or by instance `type` |
| `--actual` | off | Also ask Cost Explorer (costs $0.01; needs the `aegc:deployment` tag active) |

## aegc destroy

```
aegc destroy DEPLOYMENT [--yes] [--force] [--verify] [--no-verify]
```

Delete everything aegc created for a deployment, then check that nothing remains. See
[Operations](../how-to/operations.md#destroy-a-deployment).

| Option | Meaning |
|---|---|
| `--yes` | Actually delete (default: dry run) |
| `--force` | Proceed even with tasks queued or running |
| `--verify` | Without `--yes`: only check that nothing remains (exit 1 if something does) |
| `--no-verify` | With `--yes`: skip the check after deleting |

It exits 1 if anything is reported as `remaining`.

## aegc footprint

```
aegc footprint DEPLOYMENT
```

List what exists for a deployment: resources tagged with it (Resource Groups Tagging API), and
queues, groups, launch templates and parameters with its names, marked `owned` or `look-alike`.
Changes nothing.

## aegc status

```
aegc status DEPLOYMENT [--json]
```

The deployment's state (and why, if it isn't running); estimated spend now, today and this month,
and the limits; the lease and TTL. Per pool: tasks queued, running and dead-lettered; workers in
service, desired and maximum (in vCPUs); workers by type, their $/h, the oldest waiting task and the
worst-case cost of running tasks; the last scaling activity. Then the vCPU quotas, and each result
queue with the results waiting in it. See [Operations](../how-to/operations.md#check-status).

| Option | Meaning |
|---|---|
| `--json` | Machine-readable output |

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
aegc iam-policy {worker,operator,deployer,bootstrap,builder} [--deployment DEPLOYMENT] [--account ACCOUNT]
```

Print an IAM policy document as JSON. See [IAM policies](iam.md).

| Argument / option | Default | Meaning |
|---|---|---|
| `worker` | | On worker instances |
| `operator` | | The endpoint's credentials |
| `deployer` | | `aegc deploy`, `destroy`, `stop`, `resume`, `reap` and `footprint` |
| `bootstrap` | | `aegc bootstrap` and `unbootstrap` |
| `builder` | | `aegc image build` |
| `--deployment` | `default` | Deployment name (worker, operator) |
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
