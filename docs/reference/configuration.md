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
  python_minor: "{{ py[:2] | join('.') }}"
  max_retries_on_system_failure: 1
```

| Key | Value | Why |
|---|---|---|
| `idle_heartbeats_soft` | `0` | Keeps user endpoint processes running. Globus Compute's idle check counts results replayed from `unacked_results` as forwarded, so after a recovery it stopped the process with tasks still running on AWS, and their results waited for the next submission ([globus/globus-compute#2233](https://github.com/globus/globus-compute/issues/2233)). |
| `amqp_port` | `5671` | AMQPS on 443 had cut connections and a reconnect race ([globus/globus-compute#2232](https://github.com/globus/globus-compute/issues/2232)). `configure` also adds it to the endpoint's `config.yaml`. |
| `engine.type` | `AWSEngine` | Resolvable through the `.pth` hook installed with aegc. |
| `engine.env` | from `user_endpoint_config`, else the `--env` given to `configure` | Default env for this process's tasks. |
| `engine.python_minor` | the submitter's Python, e.g. `"3.14"` | Selects the pool for the env's image of that minor. |
| `engine.max_retries_on_system_failure` | `1` | One resubmission after a worker system failure. |

The endpoint's `user_environment.yaml` sets `AWS_REGION`, and `AWS_PROFILE` if one was given.

## AWSEngine parameters

Parameters of `AWSEngine` ([src/aegc/engine.py](../../src/aegc/engine.py)), in addition to those of
Globus Compute's engine base class. The template sets the first four; the others keep their
defaults.

| Parameter | Default | Meaning |
|---|---|---|
| `deployment` | `"default"` | aegc deployment to use; its manifest is `/aegc/<deployment>/manifest` |
| `region` | the session's | AWS region of the deployment |
| `env` | `""` | Default env; empty means any pool, subject to the Python check |
| `python_minor` | `""` | Submitter's Python minor version; empty skips the Python check |
| `aws_profile` | `""` | AWS profile for the engine's clients |
| `max_retries_on_system_failure` | `1` | Resubmissions after a system failure |
| `scale_interval_s` | `10.0` | Seconds between scaler passes |
| `result_wait_s` | `20` | Long-poll wait on the result queue |
| `inline_limit_bytes` | `700000` | Task bodies larger than this go to S3 |
| `stall_fail_s` | `3600.0` | Seconds a pool may want capacity with no instance in service before this process's queued tasks for it fail with `AEGC_NO_CAPACITY` |
| `label` | `"AWSEngine"` | Engine label |

## user_endpoint_config

Set by the client: `Executor(user_endpoint_config={...})`. The schema
([src/aegc/templates/endpoint/user_config_schema.json](../../src/aegc/templates/endpoint/user_config_schema.json))
allows one key:

| Key | Type | Meaning |
|---|---|---|
| `env` | string matching `^[a-z][a-z0-9-]{0,40}$` | Science environment for this executor's tasks |

No other keys are accepted. Globus Compute runs one user endpoint process per distinct config.

## resource_specification

Set by the client: `Executor(resource_specification={...})`. `AWSEngine` accepts these keys and
fails a task with any other:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `env` | string | the engine's `env` | Run on the pool for this env |
| `pool` | string | | Run on this exact pool (e.g. `geospatial-py314`); overrides `env` and `size`. `cpus` and `memory_gib`, if given, must fit it |
| `walltime_s` | integer, 1 to the pool's `max_walltime` (at most 41,400) | the pool's `walltime`, else 39,600 | Kill the task after this many seconds |
| `size` | `small`, `medium`, `mem`, `large` or `whole` | | Run on a pool of this [size class](#size-classes); a pool without one counts as `small`. `cpus` and `memory_gib`, if given too, must fit it |
| `cpus` | positive number | | vCPUs the task needs: run on the cheapest pool whose tasks get at least this many |
| `memory_gib` | positive number | | GiB the task needs: run on the cheapest pool whose tasks get at least this much |

Common misspellings get a hint in the error: `vcpus`, `num_cores` and `cores` for `cpus`;
`memory`, `mem` and `mem_per_worker` for `memory_gib`.

## Pool selection

For each task the engine:

1. fails the task with the state's code (`AEGC_BUDGET_EXCEEDED`, `AEGC_STOPPED` or `AEGC_EXPIRED`)
   if the deployment isn't running;
2. fails the task if the deployment couldn't be loaded, or `resource_specification` has unknown
   keys;
3. with `pool`, uses that pool if it exists and its Python matches the submitter;
4. otherwise takes the pools whose env is `resource_specification["env"]`, else the engine's `env`
   (all pools if both are empty), and keeps those whose image's Python minor matches the submitter
   (if known);
5. from those, with `size`, picks the first by name of that class, and checks that its tasks get
   at least `cpus` and `memory_gib`; otherwise keeps the pools whose tasks get at least `cpus`
   vCPUs and `memory_gib` GiB (both 0 if not given) and picks the cheapest: the fewest vCPUs held
   per task (for `whole`, the worker's vCPUs), then a sized pool over a `whole` one, then the
   least memory per task, then the name. With `pool`, it checks `cpus` and `memory_gib` against
   that pool. What a task gets: its class's vCPUs and GiB; for `whole`, `vcpu=` and `mem=`; for a
   pool without a class and with fixed `slots=N`, the low end of `vcpu=` and `mem=` divided by N;
   otherwise 1 vCPU and 2 GiB;
6. checks `walltime_s` against the chosen pool's `max_walltime`, or applies its default `walltime`;
7. fails the task with an `AEGC_CONFIGURATION` error if any of this fails.

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
| Instance metadata | IMDSv2 required, hop limit 2, instance tags enabled |
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

At start the worker adds the instance type, Availability Zone and lifecycle (Spot or on-demand)
from instance metadata, for its [cost ledger](../concepts/cost.md#cost-ledger) record.
