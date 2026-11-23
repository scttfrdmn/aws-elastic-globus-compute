# CLI

`aegc` manages AWS resources, worker images and the endpoint, on your machine or in the cloud. `aegc-worker` runs inside worker
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
| [`image register`](#aegc-image-register) | Check an image you built (with aegc and `globus-compute-endpoint`) and record it |
| [`image list`](#aegc-image-list) | An environment's recorded images, one per Python minor |
| [`image forget`](#aegc-image-forget) | Drop an environment's image for one Python minor |
| [`deploy`](#aegc-deploy) | Create or update a deployment and its pools (EC2 or Lambda) |
| [`stop`](#aegc-stop) | Stop a deployment's spending now (kill switch) |
| [`resume`](#aegc-resume) | Resume a stopped, capped or expired deployment |
| [`limits`](#aegc-limits) | Show or change a deployment's spend limits |
| [`cost`](#aegc-cost) | Estimated spend from the cost ledger |
| [`destroy`](#aegc-destroy) | Delete a deployment (dry run by default) |
| [`footprint`](#aegc-footprint) | List what exists for a deployment: owned resources and untagged look-alikes |
| [`status`](#aegc-status) | State, spend, limits, grants, lease, pools and their size classes, queues, workers and waiting results |
| [`metrics`](#aegc-metrics) | Per-pool task counts, times, interruptions and capacity from CloudWatch |
| [`provenance`](#aegc-provenance) | Where and how a task ran: image, instance, timings, estimated cost |
| [`reap`](#aegc-reap) | Delete idle result queues of finished endpoint processes |
| [`iam-policy`](#aegc-iam-policy) | Print an IAM policy document for one of aegc's roles |
| [`endpoint`](#aegc-endpoint) | Run a Globus Compute endpoint with `AWSEngine` in Docker, or deploy it to EC2 |
| [`evaluate`](#aegc-evaluate) | Spend limits, lease, liveness and idle result queues, without an engine (the cloud endpoint runs it every 5 minutes) |

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
aegc image build --env ENV (--base-tag BASE_TAG | --base REF) [--source {aarchsci,aarchbio}]
                 [--allow-change PACKAGE ...] [--python PATH] [--gce-version GCE_VERSION]
                 [--no-push] [--direct] [--project-dir PROJECT_DIR]
```

Build, test, push and record a worker image for an environment, from an aarchsci env or aarchbio
tool (`--base-tag`) or from any `linux/arm64` image (`--base`). Give exactly one of the two. See
[Worker images](../how-to/images.md).

| Option | Default | Meaning |
|---|---|---|
| `--env ENV` | required | With `--base-tag`, the aarchsci env or aarchbio tool, e.g. `geospatial`; with `--base`, any name of yours |
| `--base-tag BASE_TAG` | | Immutable aarchsci/aarchbio tag, e.g. `2026.11.13.154112`; `latest` is refused |
| `--base REF` | | Any `linux/arm64` image with Python 3.11 or later, e.g. `python:3.13-slim@sha256:...`; not `latest` or untagged. aegc builds from the image the tag points to now |
| `--source` | `aarchsci` | With `--base-tag`: `aarchsci` or `aarchbio` |
| `--allow-change PACKAGE` | `click` only | Let installing aegc and `globus-compute-endpoint` change this package of the base (repeatable), for example `python-dateutil` when the base's is newer than botocore accepts. `click` may always change |
| `--python PATH` | `python3` | With a non-conda `--base`: the base's Python |
| `--gce-version` | `4.21.0` | `globus-compute-endpoint` version |
| `--no-push` | off | Build and test locally only; record nothing |
| `--direct` | off | Pull an aarchsci/aarchbio base from quay.io, not the ECR cache |
| `--project-dir` | `.` | aegc source tree to build the wheel from |

A `--base` in the `mambaorg/micromamba` layout is built with
[templates/worker/Dockerfile](../../src/aegc/templates/worker/Dockerfile), like aarchsci; any
other with [templates/worker/Dockerfile.pip](../../src/aegc/templates/worker/Dockerfile.pip).
The base must be arm64 and have `sh`. Every package of the base must stay as it is, except
`click` and those named with `--allow-change`: the conda build fails if one changes, and the pip
step installs with the base's other packages as constraints. Builds are identified by the base's
image id, so a moved tag builds anew. The image is recorded under its Python minor, replacing only that minor's
record. Prints the pinned image (`<registry>/aegc/<env>@sha256:…`), or the local tag with
`--no-push`.

## aegc image register

```
aegc image register --env ENV --image IMAGE
```

Check an image you built and record it. `register` pulls it (or uses a local one) and runs it the
way workers do, through its own entrypoint and user: `aegc-worker version` must report aegc 0.3 or
later and uid 0 or 57439, and `aegc-worker selftest` must pass. The image must also be pinned and
`linux/arm64`. It's copied into `aegc/<env>` and recorded as kind `registered`, under the Python
minor `aegc-worker version` reports. See
[Register an image you built](../how-to/images.md#register-an-image-you-built).

| Option | Default | Meaning |
|---|---|---|
| `--env ENV` | required | The environment's name; `<env>-py314` must be a valid pool name |
| `--image IMAGE` | required | The image, pinned: `repo@sha256:...` (or a tag other than `latest`) |

Prints the pinned `aegc/<env>` image.

## aegc image list

```
aegc image list --env ENV
```

Print an environment's recorded images, one line per Python minor: the minor, the kind (`conda`,
`pip` or `registered`), the aegc version in the image, and the pinned image, separated by tabs.
Fails with `aegc: no image recorded for env 'x'` if there's none.

## aegc image forget

```
aegc image forget --env ENV --python 3.NN
```

Drop the environment's record for one Python minor. The image stays in ECR, where the lifecycle
policy expires it unless a deployment uses it, and deployed pools keep running until you redeploy
or prune them. With the last minor gone, the env's records are deleted.

| Option | Meaning |
|---|---|
| `--env ENV` | The environment |
| `--python 3.NN` | The Python minor to forget |

## aegc deploy

```
aegc deploy DEPLOYMENT --pool POOL [--pool POOL ...]
            [--max-usd-day N|none] [--max-usd-month N|none] [--max-instance-hours-day N|none]
            [--on-cap {fail,kill}] [--grant read|write=s3://BUCKET/PREFIX|none ...]
            [--lease DURATION] [--ttl DURATION]
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
| `--grant read|write=s3://BUCKET/PREFIX` | Let every task in the deployment read or write under an S3 prefix of yours; repeatable, at most 32. `s3://BUCKET/` is the whole bucket; write grants only reach buckets in the deployment's account. Replaces the previous grants; see [Read and write your data](../how-to/data.md) |
| `--grant none` | Remove the grants |
| `--lease DURATION` | Freeze the deployment if no endpoint renews it for this long (default `48h`, at least `3h`; `none` disables); running endpoints renew hourly |
| `--ttl DURATION` | Freeze the deployment this long from now, renewed or not (e.g. `7d`; `none` clears) |
| `--prune` | Remove pools not listed (a dry run of that part unless `--yes`) |
| `--yes` | With `--prune`: actually remove them |
| `--force` | `--prune` even with tasks queued |
| `--allow-old-image` | Deploy images built before aegc 0.2 (their workers ignore limits and stop), or, for a Lambda pool, before 0.7 (no `aegc-worker lambda`) |
| `--dry-run` | Report what would change; change nothing |

Durations are `<N>m`, `<N>h` or `<N>d`. Limits, grants, lease and TTL are kept across redeploys
unless given again. Without any limit, `deploy` prints a note. While the deployment isn't running, a
redeploy keeps group minimums at 0. `deploy` refuses an image built with aegc before 0.2 unless
`--allow-old-image` is given.

Pools whose image was built with aegc 0.4 or later [isolate tasks](../concepts/security.md#task-isolation)
from the worker role. For older images `deploy` warns,
`warning: <pools>: images built before aegc 0.4 run tasks with the worker role's credentials (they
can read other tasks' payloads and post results); rebuild them with `aegc image build` to isolate
tasks`, and keeps the grants on the worker role. `deploy` creates the task role
`aegc-<d>-task-<region>` and turns on each group's 1-minute Auto Scaling metrics. Role policies are
compared with what the role has: an unchanged one is reported as `exists`.

Pool options:

| Option | Default | Meaning |
|---|---|---|
| `max_vcpus=N` | 8 | Maximum vCPUs in the pool |
| `min_vcpus=N` | 0 | Minimum vCPUs in the pool |
| `vcpu=LO-HI` or `vcpu=N` | 2-8 | vCPU range per instance; HI at most `max_vcpus` (the default HI is lowered to fit) |
| `mem=MiB` | 4096 | Minimum memory per instance |
| `size=CLASS` | none | `small`, `medium`, `mem`, `large` or `whole`: what one task gets ([size classes](configuration.md#size-classes)); adds `-<size>` to the pool name; not with `slots=N` |
| `slots=N` or `slots=auto` | `auto` | Concurrent tasks per worker; `auto` is one per vCPU, or what the size class fits |
| `idle=S` | 300 | Idle seconds before a worker leaves the group |
| `disk=GiB` | 30 | Root gp3 volume size (30–16384) |
| `throughput=MiBps` | 125 | Root volume throughput (125–1000) |
| `walltime=S` | 39,600 | Default walltime for the pool's tasks |
| `max_walltime=S` | 41,400 | Most walltime a task may request |
| `lifetime=S` | 86,400 | Seconds (3,600–259,200) after which a worker stops claiming |
| `on-demand` | Spot | Use on-demand instances |
| `spot` | Spot | Use Spot instances (undoes `on-demand`) |
| `python=3.NN` | see below | Deploy the env's image for this Python minor (and add its pool on a redeploy) |
| `max=N`, `min=N` | | v0.1 spelling, in workers: converted to `max_vcpus`/`min_vcpus` (× `vcpu` LO) with a warning |

The env must have an image recorded by `aegc image build` or `aegc image register`. Pools are
named `<env>-py<NNN>` from the image's Python version, plus `-<size>` with `size=`: for example
`geospatial-py314` and `geospatial-py314-mem`. The first deploy of an env (and size class) makes
one pool per recorded Python minor, skipping images built before aegc 0.2 when there are newer
ones, and the options apply to each pool (`min_vcpus` and `max_vcpus` too). A redeploy updates
only the minors already deployed, and notes others the env has; `python=` picks or adds one.
`size=` needs an image built with aegc 0.3 or later, can't be combined with `slots=N`, and
`size=whole` needs one worker size (`vcpu=N`).

A **Lambda pool** is `env:lambda[,option=value,...]`, named `<env>-py<NNN>-lambda`, and takes only
these options (see [Run short tasks on Lambda](../how-to/lambda-pools.md)):

| Option | Default | Meaning |
|---|---|---|
| `lambda` | | Make the pool a Lambda function fed by an SQS event source mapping |
| `memory=MiB` | 2048 | Function memory, 1024–10240 (vCPUs in proportion, 1 per 1,769 MB) |
| `timeout=S` | 900 | Function timeout, 60–1800 |
| `concurrency=N` | 10 | The mapping's maximum concurrency, 2–1000 |
| `tmp=MiB` | 512 | Function `/tmp`, 512–10240 |
| `max_walltime=S` | timeout − 20 | Most walltime a task may request; at most timeout − 20 |
| `walltime=S` | `max_walltime` | Default walltime for the pool's tasks |
| `python=3.NN` | every minor | As for EC2 pools |

EC2 options on a Lambda pool, and Lambda options on an EC2 pool, are refused. A Lambda pool needs
an image built with aegc 0.7 or later, at most 10 GiB compressed, and the
[lambda-deployer](iam.md#lambda-deployer) policy next to the deployer policy. Each deploy with
Lambda pools prints `note: <pools>: Lambda pools aren't isolated. Their tasks run with the
function role, which can read this deployment's task payloads and post results for any of its
tasks; run untrusted functions on isolated EC2 pools in a deployment without Lambda pools`. The
vCPU quota check leaves Lambda pools out; the account's Lambda concurrency isn't checked.

## aegc stop

```
aegc stop DEPLOYMENT [--hard] [--yes]
```

Stop a deployment's spending now: set its state to `stopped`, every group's minimum to 0, and
disable every Lambda pool's event source mapping. Queued tasks fail with `AEGC_STOPPED`, running
tasks finish (Lambda invocations always do, even with `--hard`), and workers leave their groups.
Lambda pools need the [lambda-deployer](iam.md#lambda-deployer) policy here. See
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
minimum, enable each Lambda pool's event source mapping, and push the lease forward.

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
| `--resume` | Also end a capped state now (as `aegc resume` does, Lambda pools' mappings included) |

## aegc cost

```
aegc cost DEPLOYMENT [--since SINCE] [--by {pool,type}] [--actual]
```

Estimated spend from the cost ledger, plus Lambda pools from Lambda's `Duration` and
`Invocations` metrics and aegc's `InitSeconds` (`lambda-<memory>MB` with `--by type`). See
[Cost ledger](../concepts/cost.md#cost-ledger) and [Lambda pools](../concepts/cost.md#lambda-pools).

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
queues, groups, launch templates, parameters, Lambda functions and event source mappings with its
names, marked `owned` or `look-alike`. Changes nothing. Without Lambda read access it leaves the
Lambda resources out and prints a note.

## aegc status

```
aegc status DEPLOYMENT [--json]
```

The deployment's state (and why, if it isn't running); estimated spend now, today and this month,
and the limits; the deployment's grants (`grants: none (workers reach only aegc's own bucket)`
without any); the lease and TTL. Per pool: tasks queued, running and dead-lettered; workers in
service, desired and maximum (in vCPUs); its size class and what one task gets (for example
`size mem: 2 vCPU(s), 16 GiB per task`, or `size per-vCPU: ...` for a pool without `size=`);
workers by type, their $/h, the oldest waiting task and the worst-case cost of running tasks; the
last scaling activity. A Lambda pool shows its function's memory and concurrency and its event
source mapping's state (`lambda 2048 MB x 10, event source mapping Enabled`) in place of workers.
Then the vCPU quotas (EC2 pools only), and each result queue with the results waiting in it. See
[Operations](../how-to/operations.md#check-status).

| Option | Meaning |
|---|---|
| `--json` | Machine-readable output; also has `grants` and each pool's `size_class`, `task_vcpus` and `task_memory_gib`, and its `kind` (`ec2` or `lambda`), `lambda_text` and `mapping` |

## aegc metrics

```
aegc metrics DEPLOYMENT [--since SINCE] [--json]
```

Per pool: tasks completed and failed by outcome, average and maximum execution and queue-wait
times, average boot-to-first-claim, interruptions, releases, poison tasks, worker exits, and the
group's largest in-service and desired capacity (for a Lambda pool, the function's largest
concurrent executions and its throttles, from `AWS/Lambda`). It reads the metrics workers emit
(namespace `aegc`) and the Auto Scaling group metrics with GetMetricData, which the deployer and operator
policies both allow. Workers from images built before aegc 0.4 emit none. See
[Watch tasks](../how-to/observability.md#aegc-metrics).

| Option | Default | Meaning |
|---|---|---|
| `--since` | `1h` | A duration such as `90m`, `1h`, `1d` or `7d`, or `today` or `month` |
| `--json` | off | Machine-readable output |

## aegc provenance

```
aegc provenance DEPLOYMENT TASK_UUID [--since SINCE] [--json]
```

Where and how a task ran: for each attempt that finished, its outcome, run and queue-wait times,
instance type, Spot or on-demand, Availability Zone, instance id, pool, slots, vCPUs, image and
estimated cost (on a Lambda pool: the function, its memory and the request id). It finds the task's `task_finished` lines in `/aegc/<d>` with FilterLogEvents,
which needs the [operator policy](iam.md#operator). `TASK_UUID` is the Globus Compute task id.
The same record is in the result's `details["aegc"]`. It exits 1 if it finds none:
`aegc: no task_finished line for <uuid> in /aegc/<d> since 14d (logs are kept 14 days; workers
from images built before aegc 0.4 don't write them)`. See
[Watch tasks](../how-to/observability.md#aegc-provenance).

| Option | Default | Meaning |
|---|---|---|
| `--since` | `14d` | How far back to look: a duration, `today` or `month` (logs are kept 14 days) |
| `--json` | off | The records as JSON, each with its `log_stream` |

## aegc reap

```
aegc reap DEPLOYMENT [--yes]
```

Delete the deployment's result queues that are empty and haven't been modified for 14 days. A
[cloud endpoint](../how-to/cloud-endpoint.md) does this every 5 minutes through `aegc evaluate`.

| Option | Meaning |
|---|---|
| `--yes` | Actually delete (default: dry run) |

## aegc iam-policy

```
aegc iam-policy {worker,operator,deployer,bootstrap,builder,task,endpoint,endpoint-deployer,
                 lambda-deployer}
                [--deployment DEPLOYMENT] [--account ACCOUNT]
```

Print an IAM policy document as JSON. See [IAM policies](iam.md).

| Argument / option | Default | Meaning |
|---|---|---|
| `worker` | | On worker instances |
| `operator` | | The endpoint's credentials |
| `deployer` | | `aegc deploy`, `destroy`, `stop`, `resume`, `reap` and `footprint` |
| `bootstrap` | | `aegc bootstrap` and `unbootstrap` |
| `builder` | | `aegc image build`, `register`, `list` and `forget` |
| `task` | | `aegc-task` on the task role, besides the grants |
| `endpoint` | | `aegc-endpoint` on the cloud endpoint host's role |
| `endpoint-deployer` | | `aegc endpoint deploy`, `undeploy` and `status`, with the deployer policy |
| `lambda-deployer` | | `aegc deploy`, `destroy`, `stop`, `resume`, `status` and `footprint` of Lambda pools, with the deployer policy |
| `--deployment` | `default` | Deployment name (worker, operator, task, endpoint) |
| `--account` | the caller's | AWS account id |

With both `--region` and `--account` given, it makes no AWS calls.

## aegc endpoint

```
aegc endpoint {build,login,configure,start,stop,logs,test,id,deploy,undeploy,status}
              [--name NAME] [--deployment DEPLOYMENT] [--env ENV] [-n N]
              [--allow-function ID ...] [--project-dir PROJECT_DIR] [--python PYTHON]
              [--instance-type TYPE] [--alarm-email ADDRESS] [--image IMAGE]
              [--stop-local] [--session-manager] [--dry-run] [--yes]
```

Run a Globus Compute endpoint with `AWSEngine` in Docker, or deploy it to EC2 (`deploy`,
`undeploy`, `status`; see [Run the endpoint in the cloud](../how-to/cloud-endpoint.md)).

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
| `deploy` | Run the endpoint `--name` on an EC2 instance for `--deployment`, with the same endpoint id, as your Globus login; needs `--env`. Builds and pushes the endpoint image (`aegc/endpoint`, `linux/arm64`) unless `--image`, stores the login in the secret `aegc-<d>-endpoint`, and creates or updates the host's role, launch template, Auto Scaling group of one and offline alarm. Refuses while the local endpoint runs, unless `--stop-local` |
| `undeploy` | Remove the deployment's cloud endpoint: its group, launch template, alarm and topic, the image's `inuse-` tag, role and instance profile, and secret (dry run unless `--yes`) |
| `status` | The cloud endpoint's instance and its state, the endpoint's name and id, and the offline alarm's state |

| Option | Default | Meaning |
|---|---|---|
| `--name` | `aegc` | Globus Compute endpoint name |
| `--deployment` | `default` | aegc deployment the engine uses (configure, deploy, undeploy, status) |
| `--env` | | Default science environment (configure, deploy); env to test (test) |
| `-n` | 10 | Tasks to submit (test) |
| `--allow-function ID` | | Only these registered function ids may run (configure, deploy); repeatable. `none` removes the list, so any function may run; without the option, `configure` leaves the list as it is, and `deploy` uses the local endpoint's list. Globus refuses other functions with `403 FUNCTION_NOT_PERMITTED` once the endpoint restarts. See [Allow only your functions](../how-to/flows.md#allow-only-your-functions) |
| `--project-dir` | `.` | aegc source tree (build, deploy) |
| `--python` | `3.14` | Python in the endpoint image (build, deploy); `test` submits with it |
| `--instance-type` | `t4g.micro` | The cloud endpoint's instance type, Graviton (deploy) |
| `--alarm-email` | | Email this address through SNS when the cloud endpoint goes offline and when it's back (deploy). A redeploy without it keeps the existing subscriptions |
| `--image` | | An endpoint image in `aegc/endpoint`, pinned by digest, instead of building one (deploy) |
| `--stop-local` | off | Stop the local endpoint first if it's running (deploy) |
| `--session-manager` | off | Allow Session Manager shells on the host; whoever may `ssm:StartSession` there gets root, and with it your stored Globus login (deploy) |
| `--dry-run` | off | Report what would change; stop, build and write nothing (deploy) |
| `--yes` | off | Actually remove the cloud endpoint (undeploy; default: dry run) |

The container mounts `~/.aws` read-only and gets `AWS_PROFILE` (from `--profile` or the
environment), `AWS_REGION` and `AWS_DEFAULT_REGION`.

`configure` writes `allowed_functions` into the endpoint's `config.yaml` with `--allow-function`,
and prints `allowed functions: <ids>; restart the endpoint (stop, start) for Globus to enforce it`.
A value that isn't a UUID fails with
`aegc: --allow-function takes function ids (UUIDs), or none to allow any`.

`deploy` needs the deployer and [endpoint-deployer](iam.md#endpoint-deployer) policies, and the
builder policy to push the image; `undeploy` and `status` need the first two. Its errors:

- `aegc: endpoint deploy needs --env (the default science environment)`.
- `aegc: no deployment '<d>' in <region>; `aegc deploy <d> ...` first`.
- `aegc: the local endpoint <name> is running (aegc-endpoint-<name>); one endpoint id can run in
  one place. Stop it, or pass --stop-local`.
- `endpoint <name> (<id>) is already the cloud endpoint of deployment <d2>; one endpoint id can
  run in one place (`aegc endpoint undeploy` it there first)`.
- `endpoint image must be pinned by digest, got '...'`, or `image '...' isn't in aegc's repository
  aegc/endpoint`, for an `--image` that isn't `<registry>/aegc/endpoint@sha256:...`.

## aegc evaluate

```
aegc evaluate DEPLOYMENT [--endpoint-id ENDPOINT_ID] [--dry-run]
```

One pass of the checks that don't need an engine. The [cloud endpoint](../how-to/cloud-endpoint.md#the-evaluator)
runs it every 5 minutes; you can run it yourself, for example as a dry run with operator
credentials:

1. With `--endpoint-id`, ask Globus whether the endpoint is online (with this machine's Globus
   Compute login) and, on the cloud endpoint, publish the `EndpointOnline` metric.
2. If the deployment is running and has spend limits, check spend and trip a cap like an engine
   would (state `capped`, `by: evaluator`, group minimums to 0, Lambda pools' mappings disabled).
   Then make each Lambda pool's event source mapping follow the state: enabled while the
   deployment is running (a cap whose period ended reads as running), disabled otherwise.
3. Renew the lease (hourly), only while Globus reports the endpoint online.
4. Delete result queues that are empty and idle for 14 days, as `aegc reap --yes` does.

| Option | Meaning |
|---|---|
| `--endpoint-id` | The endpoint to check with Globus; without it, no liveness check and no lease renewal |
| `--dry-run` | Change nothing and publish nothing; the report says `would cap it`, `would enable` (or `disable`) `Lambda pool <pool>'s event source mapping`, `would renew the lease` or `would delete idle result queue` |

It prints its report on stderr, for example `evaluate e2e: endpoint online, state running`, with
one line per action and `error:` lines for steps that failed, and exits 1 if any did. The metric
is published only when the environment has `AEGC_METRIC_TOKEN` (the cloud endpoint's token).

## aegc-worker

Runs inside worker images; you don't normally call it yourself.

| Command | What it does |
|---|---|
| `aegc-worker run` | Claim and run tasks until drained. Reads its configuration from `AEGC_WORKER_CONFIG_B64` (set by the user data). Logs JSON lines, and writes EMF metric lines on stdout. SIGTERM releases in-flight tasks and exits (a task that finishes meanwhile posts its result); SIGUSR1 simulates a Spot interruption |
| `aegc-worker lambda` | The command of a [Lambda pool](../how-to/lambda-pools.md)'s function: serve Lambda invocations through the Runtime API, one SQS record each, with the same message path as `run`. Reads `AEGC_WORKER_CONFIG_B64` from the function's environment; logs JSON lines and writes EMF metric lines, including `InitSeconds` once per execution environment |
| `aegc-worker selftest` | Run a probe task through `execute_task` in this image's Python; used by `aegc image build` and `aegc image register` |
| `aegc-worker version` | Print the aegc and `globus-compute-endpoint` versions, the Python minor and interpreter, and the uid, as one line of JSON; used by `aegc image register` |
