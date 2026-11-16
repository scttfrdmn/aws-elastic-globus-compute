# Configuration reference

## Endpoint template

`aegc endpoint configure` writes this user endpoint template, shown here for
`--deployment e2e --env geospatial` in us-west-2 (comments removed). The source is
[src/aegc/templates/endpoint/user_config_template.yaml.j2](../../src/aegc/templates/endpoint/user_config_template.yaml.j2).

```yaml
{%- set py = _GC.user_runtime.python.version_tuple if _GC.user_runtime is defined and _GC.user_runtime.python is defined else [] %}
idle_heartbeats_soft: 0
amqp_port: 5671

engine:
  type: AWSEngine
  deployment: e2e
  region: us-west-2
  env: {{ env | default('"geospatial"') }}
{%- if py %}
  python_minor: "{{ py[:2] | join('.') }}"
{%- elif python is defined %}
  python_minor: {{ python }}
{%- else %}
  python_minor: ""
{%- endif %}
{%- if pool is defined %}
  pool: {{ pool }}
{%- endif %}
{%- if size is defined %}
  size: {{ size }}
{%- endif %}
{%- if cpus is defined %}
  cpus: {{ cpus }}
{%- endif %}
{%- if memory_gib is defined %}
  memory_gib: {{ memory_gib }}
{%- endif %}
{%- if walltime_s is defined %}
  walltime_s: {{ walltime_s }}
{%- endif %}
  max_retries_on_system_failure: 1
```

| Key | Value | Why |
|---|---|---|
| `idle_heartbeats_soft` | `0` | Keeps user endpoint processes running. Globus Compute's idle check counts results replayed from `unacked_results` as forwarded, so after a recovery it stopped the process with tasks still running on AWS, and their results waited for the next submission ([globus/globus-compute#2233](https://github.com/globus/globus-compute/issues/2233)). |
| `amqp_port` | `5671` | AMQPS on 443 had cut connections and a reconnect race ([globus/globus-compute#2232](https://github.com/globus/globus-compute/issues/2232)). `configure` also adds it to the endpoint's `config.yaml`. |
| `engine.type` | `AWSEngine` | Resolvable through the `.pth` hook installed with aegc. |
| `engine.env` | from `user_endpoint_config`, else the `--env` given to `configure` | Default env for this process's tasks. |
| `engine.python_minor` | the submitter's Python, e.g. `"3.14"`; else `python` from `user_endpoint_config`; else empty | Selects the pool for the env's image of that minor. The SDK sends its Python with each submission; Globus Flows doesn't, so `python` can say it. |
| `engine.pool`, `size`, `cpus`, `memory_gib`, `walltime_s` | from `user_endpoint_config`, only if given | Pool choice and walltime for this process's tasks; see [below](#user_endpoint_config). |
| `engine.max_retries_on_system_failure` | `1` | One resubmission after a worker system failure. |

The endpoint's `user_environment.yaml` sets `AWS_REGION`, and `AWS_PROFILE` if one was given.

## AWSEngine parameters

Parameters of `AWSEngine` ([src/aegc/engine.py](../../src/aegc/engine.py)), in addition to those of
Globus Compute's engine base class. The template sets `deployment`, `region`, `env` and
`python_minor`, and the task defaults when `user_endpoint_config` gives them; the others keep their
defaults.

| Parameter | Default | Meaning |
|---|---|---|
| `deployment` | `"default"` | aegc deployment to use; its manifest is `/aegc/<deployment>/manifest` |
| `region` | the session's | AWS region of the deployment |
| `env` | `""` | Default env; empty means any pool, subject to the Python check |
| `python_minor` | `""` | Submitter's Python minor version; empty skips the Python check, but refuses tasks for an env with several minors |
| `pool` | `""` | Run tasks on this exact pool |
| `size` | `""` | Run tasks on a pool of this [size class](#size-classes) |
| `cpus` | `0` | vCPUs each task needs (0: no requirement) |
| `memory_gib` | `0` | GiB each task needs (0: no requirement) |
| `walltime_s` | `0` | Walltime for each task (0: the pool's default) |
| `aws_profile` | `""` | AWS profile for the engine's clients |
| `max_retries_on_system_failure` | `1` | Resubmissions after a system failure |
| `scale_interval_s` | `10.0` | Seconds between scaler passes |
| `result_wait_s` | `20` | Long-poll wait on the result queue |
| `inline_limit_bytes` | `700000` | Task bodies larger than this go to S3 |
| `stall_fail_s` | `3600.0` | Seconds a pool may want capacity with no instance in service before this process's queued tasks for it fail with `AEGC_NO_CAPACITY` |
| `label` | `"AWSEngine"` | Engine label |

## user_endpoint_config

Set by the client: `Executor(user_endpoint_config={...})`. This is where every aegc task option
goes. The schema
([src/aegc/templates/endpoint/user_config_schema.json](../../src/aegc/templates/endpoint/user_config_schema.json))
allows these keys, and the template renders them into the engine section:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `env` | string matching `^[a-z][a-z0-9-]{0,40}$` | the `--env` given to `configure` | Science environment for this executor's tasks |
| `python` | string matching `^3\.[0-9]{1,2}$`, e.g. `"3.14"` | the submitter's Python | Pick the env's image for this Python minor when the submission doesn't carry a Python version, as from [Globus Flows](../how-to/flows.md). Ignored when it does (the SDK's) |
| `pool` | string matching `^[a-z][a-z0-9-]{0,39}$` | | Run on this exact pool (e.g. `geospatial-py314`); overrides `env` and `size`. `cpus` and `memory_gib`, if given, must fit it |
| `size` | `small`, `medium`, `mem`, `large` or `whole` | | Run on a pool of this [size class](#size-classes); a pool without one counts as `small`. `cpus` and `memory_gib`, if given too, must fit it |
| `cpus` | number > 0 | | vCPUs a task needs: run on the cheapest pool whose tasks get at least this many |
| `memory_gib` | number > 0 | | GiB a task needs: run on the cheapest pool whose tasks get at least this much |
| `walltime_s` | integer ≥ 1, at most the pool's `max_walltime` (at most 41,400) | the pool's `walltime`, else 39,600 | Kill the task after this many seconds |

No other keys are accepted; Globus Compute rejects the submission before it reaches aegc. Globus
Compute runs one user endpoint process per distinct config, so each combination you use gets its
own process (and its own result queue). For tasks with different options, use one `Executor` per
combination.

## resource_specification

Don't use it for aegc options. The Globus Compute web service validates
`Executor(resource_specification={...})` as an MPI request: it requires `num_nodes` (a request
without it fails with HTTP 422 `REQUEST_MALFORMED`), and it drops every key except `num_nodes`,
`ranks_per_node`, `num_ranks` and `launcher_options`. So `size`, `cpus`, `memory_gib`, `walltime_s`,
`pool` and `env` never reach `AWSEngine` this way.

If code you share with MPI endpoints passes a `resource_specification`, `AWSEngine` accepts its MPI
keys as long as they ask for one instance and no MPI launch:

| Key | Accepted | Otherwise the task fails with |
|---|---|---|
| `num_nodes` | `1` | `AWSEngine runs each task on one instance; num_nodes must be 1, not N` |
| `ranks_per_node`, `num_ranks` | missing, or `1` | `AWSEngine doesn't launch MPI ranks; use cpus= for a multi-core task` |
| `launcher_options` | missing or empty | the same |

aegc's own keys, if they do reach the engine in a `resource_specification`, override those from
`user_endpoint_config`; any other key fails the task (`unsupported task options [...]; AWSEngine
accepts [...]`, with a hint for `vcpus`, `memory` and similar spellings), and so does a `cpus` or
`memory_gib` that isn't a positive number (`task option cpus must be a positive number`).
Non-numeric MPI keys fail with `num_nodes, ranks_per_node and num_ranks must be numbers`.

## Pool selection

For each task the engine:

1. fails the task with the state's code (`AEGC_BUDGET_EXCEEDED`, `AEGC_STOPPED` or `AEGC_EXPIRED`)
   if the deployment isn't running;
2. fails the task if the deployment couldn't be loaded, or `resource_specification` asks for more
   than one instance, MPI ranks, or keys it doesn't know;
3. takes `pool`, `size`, `cpus`, `memory_gib` and `walltime_s` from the engine (that is, from
   `user_endpoint_config`), overridden by any that arrive in `resource_specification`;
4. with `pool`, uses that pool if it exists and its Python matches the submitter;
5. otherwise takes the pools whose env is the engine's `env` (all pools if it's empty), and keeps
   those whose image's Python minor matches the submitter (or `python`); if neither is known and
   those pools have more than one minor, it fails the task, asking for `python`;
6. from those, with `size`, picks the first by name of that class, and checks that its tasks get
   at least `cpus` and `memory_gib`; otherwise keeps the pools whose tasks get at least `cpus`
   vCPUs and `memory_gib` GiB (both 0 if not given) and picks the cheapest: the fewest vCPUs held
   per task (for `whole`, the worker's vCPUs), then a sized pool over a `whole` one, then the
   least memory per task, then the name. With `pool`, it checks `cpus` and `memory_gib` against
   that pool. What a task gets: its class's vCPUs and GiB; for `whole`, `vcpu=` and `mem=`; for a
   pool without a class and with fixed `slots=N`, the low end of `vcpu=` and `mem=` divided by N;
   otherwise 1 vCPU and 2 GiB;
7. checks `walltime_s` against the chosen pool's `max_walltime`, or applies its default `walltime`;
8. fails the task with an `AEGC_CONFIGURATION` error if any of this fails.

## Size classes

A pool's `size=` option ([src/aegc/sizes.py](../../src/aegc/sizes.py)) sets what one task gets.
How to use them is in [Size classes](../how-to/configuration.md#size-classes).

| Class | vCPUs per task | GiB per task | `MemoryGiBPerVCpu` minimum | Slots with `slots=auto` |
|---|---|---|---|---|
| (none) | 1 | about 2 (not a slot limit) | not set | one per vCPU |
| `small` | 1 | 2 | not set | min(vCPUs, ⌊GiB ÷ 1.8⌋) |
| `medium` | 2 | 8 | 4 | min(⌊vCPUs ÷ 2⌋, ⌊GiB ÷ 7.2⌋) |
| `mem` | 2 | 16 | 8 | min(⌊vCPUs ÷ 2⌋, ⌊GiB ÷ 14.4⌋) |
| `large` | 8 | 32 | 4 | min(⌊vCPUs ÷ 8⌋, ⌊GiB ÷ 28.8⌋) |
| `whole` | the instance's | the instance's | not set | 1 |

`GiB` is the memory the worker sees, a little under the instance's nominal size; the divisor is
the class's memory × 0.9. Slots are always at least 1. A sized pool can't also set `slots=N`.
`size=` needs an image built with aegc 0.3 or later.

For a sized pool, `deploy` sets the pool's `size_class`, `task_cpus`, `task_memory_gib` and
`memory_gib_per_vcpu_min` in the manifest, adds `MemoryGiBPerVCpu` to the group's instance
requirements (when the class sets one), and raises the low end of `vcpu=` to the class's vCPUs if
`vcpu=` isn't given. A `whole` pool has one worker size (`vcpu=N`; without `vcpu=`, the top of
the range, 8 by default), and its tasks are sure to get that many vCPUs and `mem=`. The scaler
asks for the class's vCPUs per task (`whole`: the worker's vCPUs).
Pools without `size=` keep their launch template and user data unchanged.

## Pool settings

Pools are set with `aegc deploy --pool` (see the [CLI](cli.md#aegc-deploy)) and stored in the
manifest. Settings that `--pool` doesn't expose are fixed:

| Setting | Value |
|---|---|
| Allowed instance types | `c8g.*`, `m8g.*`, `r8g.*`, `c9g.*`, `m9g.*`, `r9g.*`, `c7g.*`, `m7g.*` |
| Instance filters | AWS-designed CPUs, burstable types excluded; `MemoryGiBPerVCpu` from the [size class](#size-classes), if any |
| Spot allocation | `price-capacity-optimized`, capacity rebalance on |
| AMI | `/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id`, resolved at deploy |
| Capacity | `DesiredCapacityType=vcpu`: minimum, maximum and desired capacity are vCPUs |
| Root volume | gp3, encrypted, deleted with the instance; `disk=` GiB (default 30) at `throughput=` MiB/s (default 125), 3,000 IOPS (4 × the throughput above 750 MiB/s) |
| Instance metadata | IMDSv2 required, hop limit 2, IPv6 endpoint disabled, instance tags enabled |
| Task isolation | Pools whose image has aegc 0.4 or later: the worker container runs as root with `no-new-privileges` and six capabilities, task processes run as uid 57439, which the host's iptables bars from instance metadata, and get the [task role](iam.md#task-role)'s credentials; workers are tagged `aegc:isolated=true`. See [Task isolation](../concepts/security.md#task-isolation) |
| Worker logs | The awslogs driver to `/aegc/<d>`, stream `<pool>/<instance-id>`, `awslogs-format=json/emf` (so EMF lines become [metrics](../how-to/observability.md#metrics)) |
| Auto Scaling group metrics | Enabled at 1-minute granularity |
| Scale-in protection | New instances protected; `AZRebalance` suspended |
| Subnets | The default VPC's default subnets |

## Worker configuration

The launch template passes this to `aegc-worker run` as base64 JSON in `AEGC_WORKER_CONFIG_B64`:

| Key | Value |
|---|---|
| `deployment`, `pool`, `region`, `task_queue_url`, `bucket` | From the deployment |
| `slots` | The pool's `slots` (`auto` = the instance's CPU count, or what the size class fits) |
| `size_class`, `task_cpus`, `task_memory_gib` | The pool's [size class](#size-classes); only for sized pools |
| `grants` | The deployment's [grants](../how-to/data.md), `{access, bucket, prefix}` each; only when there are any. `aegc.outputs` falls back to these if it can't read the manifest's current grants |
| `idle_timeout_s` | The pool's `idle` (300) |
| `visibility_s` | 300; the heartbeat runs every `visibility_s / 3` = 100 s |
| `max_deliveries` | 4 |
| `aegc_version` | The aegc version that deployed it |
| `state_parameter` | `/aegc/<d>/state`, read every 60 s |
| `max_lifetime_s` | The pool's `lifetime` (86,400; 3,600 to 259,200) |
| `root_volume_gib`, `throughput_mibps` | The pool's `disk` and `throughput`, for the cost ledger |
| `task_role_arn`, `task_uid`, `state_dir` | Isolated pools only: the [task role](iam.md#task-role) to assume, the task uid (57439) and the agent's root-owned state directory (`/run/aegc`, for the heartbeat and the credentials token) |

At start the worker adds the instance type, Availability Zone and lifecycle (Spot or on-demand)
from instance metadata, for its [cost ledger](../concepts/cost.md#cost-ledger) record, its log
lines and [provenance](../how-to/observability.md#provenance). The user data also passes
`AEGC_INSTANCE_ID`, `AWS_REGION` and `AEGC_IMAGE` (the pinned image, for provenance).

In isolated pools each task process gets `AWS_CONTAINER_CREDENTIALS_FULL_URI` and
`AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE` (the agent's task-role credentials on `127.0.0.1`),
`AWS_REGION`, `AWS_DEFAULT_REGION` and `AWS_EC2_METADATA_DISABLED=true`. Every task gets
`AEGC_TASK_UUID` and `GC_TASK_TIMEOUT` (its walltime).
