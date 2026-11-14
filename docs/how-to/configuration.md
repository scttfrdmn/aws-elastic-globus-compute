# Configure pools and endpoints

Configuration lives in three places:

- **Pools**, set with `aegc deploy --pool` and recorded in the deployment's manifest.
- **The endpoint**, written by `aegc endpoint configure`.
- **Per submission**, chosen by the client with `user_endpoint_config` and `resource_specification`.

The exact keys and defaults are in the [configuration reference](../reference/configuration.md).

## Size a pool

Each `--pool` takes an env with a recorded image, then options:

```sh
aegc deploy e2e --pool geospatial:max=8,vcpu=4-8,mem=16384,slots=4
```

| Option | Default | Meaning |
|---|---|---|
| `max=N` | 4 | Most workers the pool runs |
| `min=N` | 0 | Workers kept running even when idle |
| `vcpu=LO-HI` (or `vcpu=N`) | 2-16 | vCPU range for instance selection |
| `mem=MiB` | 4096 | Minimum memory per instance |
| `slots=N` or `slots=auto` | auto | Tasks run at once on each worker; `auto` is one per vCPU |
| `idle=S` | 300 | Seconds a worker waits with no work before leaving the group |
| `on-demand` | Spot | Use on-demand instances instead of Spot |

The pool is named `<env>-py<NNN>` from the env and its image's Python version, for example
`geospatial-py314`. One deployment has at most one pool per env.

Points to know:

- **Memory per task.** Slots share the instance. With the defaults, a 2-vCPU, 4 GiB instance runs
  two tasks with about 2 GiB each. For memory-hungry tasks, raise `mem=` or lower `slots=`.
- **Scaling estimates.** The scaler assumes each worker has `vcpu` LO slots when `slots=auto`. If
  Auto Scaling picks larger instances, workers have more slots than the scaler counted, and the pool
  can grow further than it needs to. For predictable scaling, set `slots=N` with a narrow `vcpu`
  range.
- **Warm workers.** `min=1` keeps one worker up for fast starts, and you pay for it all the time.
- **Instance types** are fixed in v0.1: Graviton `c7g`, `m7g`, `c8g`, `m8g`, `r8g`, `c9g`, `m9g`,
  `r9g`, filtered by your `vcpu` and `mem`. The root volume is 50 GiB gp3.

## Change a deployment

Run `aegc deploy` again with the **complete** list of pools and options. Options you leave out go
back to their defaults, so `--pool geospatial` after `--pool geospatial:max=8` sets `max` back to 4.
A redeploy is idempotent: unchanged resources are reported as `exists`, and a changed launch template
gets a new version.

```sh
aegc deploy e2e --pool geospatial:max=8 --dry-run   # show what would change
aegc deploy e2e --pool geospatial:max=8
```

Then restart the endpoint, because the engine reads the manifest only when its process starts:

```sh
aegc endpoint --name aegc-e2e stop
aegc endpoint --name aegc-e2e start
```

A pool you leave out of a redeploy isn't deleted: it drops out of the manifest, so the engine stops
using it, but its queues and Auto Scaling group stay until `aegc destroy`.

New instances pick up a changed launch template; running ones keep what they booted with until they
drain.

## Configure the endpoint

```sh
aegc endpoint --name aegc-e2e --deployment e2e --env geospatial configure
```

`configure` creates the Globus Compute endpoint `aegc-e2e` in the `aegc-endpoint-home` volume if
it doesn't exist, then writes:

- `user_config_template.yaml.j2`: the `AWSEngine` template, with the deployment, the region and
  the default env filled in;
- `user_config_schema.json`: allows one `user_endpoint_config` key, `env`;
- `user_environment.yaml`: `AWS_REGION`, and `AWS_PROFILE` if one is set;
- `amqp_port: 5671` in `config.yaml`, if it has no `amqp_port` line.

The region comes from `--region` or the profile; the profile from `--profile` or `AWS_PROFILE`.
`configure` overwrites the template each time, so make changes by running it again, not by editing
the file. Restart the endpoint afterwards.

You can run several endpoints, each with its own `--name` (and its own container,
`aegc-endpoint-<name>`), for example one per deployment.

## Choose the env and walltime from the client

```python
from globus_compute_sdk import Executor

with Executor(
    endpoint_id=ENDPOINT_ID,
    amqp_port=5671,
    user_endpoint_config={"env": "geospatial"},   # which science environment
    resource_specification={"walltime_s": 3600},  # per-task settings
) as ex:
    fut = ex.submit(fn, arg)
```

- `user_endpoint_config={"env": ...}` selects the env for every task from this executor. Globus
  Compute starts a separate user endpoint process for each distinct config. Without it, the env
  given to `configure` applies.
- `resource_specification` can set `env` (overrides the above for these tasks), `pool` (an exact
  pool name), and `walltime_s` (1 to 41,400; default 39,600). Any other key fails the task.
- The client's Python minor version must match the pool's. It's taken from the client
  automatically; you don't pass it.
