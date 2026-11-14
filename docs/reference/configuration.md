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
| `engine.python_minor` | the submitter's Python, e.g. `"3.14"` | Selects the worker image. |
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
| `pool` | string | | Run on this exact pool (e.g. `geospatial-py314`); overrides `env` |
| `walltime_s` | integer, 1 to 41,400 | 39,600 | Kill the task after this many seconds |

## Pool selection

For each task the engine:

1. fails the task if the deployment couldn't be loaded, or `resource_specification` has unknown keys
   or an invalid `walltime_s`;
2. with `pool`, uses that pool if it exists and its Python matches the submitter;
3. otherwise takes the pools whose env is `resource_specification["env"]`, else the engine's `env`
   (all pools if both are empty);
4. keeps those whose image's Python minor matches the submitter (if known), and picks the first by
   name;
5. fails the task with an `AEGC_CONFIGURATION` error if nothing is left.

## Pool settings

Pools are set with `aegc deploy --pool` (see the [CLI](cli.md#aegc-deploy)) and stored in the
manifest. Settings that `--pool` doesn't expose are fixed in v0.1:

| Setting | Value |
|---|---|
| Allowed instance types | `c8g.*`, `m8g.*`, `r8g.*`, `c9g.*`, `m9g.*`, `r9g.*`, `c7g.*`, `m7g.*` |
| Instance filters | AWS-designed CPUs, burstable types excluded |
| Spot allocation | `price-capacity-optimized`, capacity rebalance on |
| AMI | `/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id`, resolved at deploy |
| Root volume | 50 GiB gp3, 3,000 IOPS, 500 MiB/s, encrypted |
| Instance metadata | IMDSv2 required, hop limit 2, instance tags enabled |
| Scale-in protection | New instances protected; `AZRebalance` suspended |
| Subnets | The default VPC's default subnets |

## Worker configuration

The launch template passes this to `aegc-worker run` as base64 JSON in `AEGC_WORKER_CONFIG_B64`:

| Key | Value |
|---|---|
| `deployment`, `pool`, `region`, `task_queue_url`, `bucket` | From the deployment |
| `slots` | The pool's `slots` (`auto` = the instance's CPU count) |
| `idle_timeout_s` | The pool's `idle` (300) |
| `visibility_s` | 300; the heartbeat runs every `visibility_s / 3` = 100 s |
| `max_deliveries` | 4 |
| `aegc_version` | `0.1.0` |
