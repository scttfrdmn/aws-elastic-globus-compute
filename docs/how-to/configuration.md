# Configure pools and endpoints

Configuration lives in three places:

- **Pools**, set with `aegc deploy --pool` and recorded in the deployment's manifest.
- **The endpoint**, written by `aegc endpoint configure`.
- **Per submission**, chosen by the client with `user_endpoint_config` and `resource_specification`.

The exact keys and defaults are in the [configuration reference](../reference/configuration.md).

## Size a pool

Each `--pool` takes an env with a recorded image, then options:

```sh
aegc deploy e2e --pool geospatial:max_vcpus=32,vcpu=4-8,mem=16384,slots=4
```

| Option | Default | Meaning |
|---|---|---|
| `max_vcpus=N` | 8 | Most vCPUs the pool runs, across all its workers |
| `min_vcpus=N` | 0 | vCPUs kept running even when idle |
| `vcpu=LO-HI` (or `vcpu=N`) | 2-8 | vCPU range of each worker, for instance selection; HI can't exceed `max_vcpus` |
| `mem=MiB` | 4096 | Minimum memory per worker |
| `slots=N` or `slots=auto` | auto | Tasks run at once on each worker; `auto` is one per vCPU |
| `idle=S` | 300 | Seconds a worker waits with no work before leaving the group |
| `disk=GiB` | 30 | Root gp3 volume, 30 to 16,384 GiB |
| `throughput=MiBps` | 125 | Root volume throughput, 125 to 1,000 MiB/s |
| `walltime=S` | 39,600 | Default walltime for the pool's tasks |
| `max_walltime=S` | 41,400 | Most walltime a task may ask for (at most 41,400) |
| `lifetime=S` | 86,400 | Seconds (3,600 to 259,200) after which a worker stops claiming and leaves once its tasks finish |
| `on-demand` | Spot | Use on-demand instances instead of Spot |
| `spot` | Spot | Use Spot again, undoing an earlier `on-demand` |

`max=N` and `min=N`, the v0.1 spelling, still work. They count workers, so aegc converts them to
vCPUs (N × the low end of `vcpu=`) and prints a warning: `max=3` with the default `vcpu=2-8`
becomes `max_vcpus=6`.

The pool is named `<env>-py<NNN>` from the env and its image's Python version, for example
`geospatial-py314`. One deployment has at most one pool per env.

Points to know:

- **Capacity is in vCPUs.** `max_vcpus=8` allows four 2-vCPU workers or one 8-vCPU worker,
  whichever Spot provides; see [Capacity in vCPUs](../concepts/architecture.md#capacity-in-vcpus).
  `max_vcpus` must be at least the high end of `vcpu=`, so that no single worker can exceed it, and
  must fit your account's vCPU quota. If you set a small `max_vcpus` without `vcpu=`, `deploy` lowers the
  default high end to fit: `max_vcpus=4` gives `vcpu=2-4`.
- **Memory per task.** Slots share the instance. With the defaults, a 2-vCPU, 4 GiB instance runs
  two tasks with about 2 GiB each. For memory-hungry tasks, raise `mem=` or lower `slots=`.
- **Fixed slots.** With `slots=N`, the scaler counts each worker as the smallest size in `vcpu=`. If
  Auto Scaling picks larger instances, the group's vCPUs fill up with fewer workers, and fewer
  tasks run at once than the scaler planned for. Use a narrow `vcpu` range with `slots=N`.
- **Warm workers.** `min_vcpus=2` keeps one 2-vCPU worker up for fast starts, and you pay for it
  all the time.
- **Walltime.** `walltime=` sets the default for tasks that don't pass `walltime_s`, and
  `max_walltime=` refuses requests above it. Both are capped at 41,400 s, the SQS limit.
- **Lifetime.** A worker stops claiming new tasks after `lifetime` seconds (1 to 72 hours),
  finishes what it has, and leaves, so no instance runs for days. A worker kept by `min_vcpus` is
  terminated without lowering desired capacity, so the group replaces it with a fresh instance. The
  host watchdog terminates an instance that's still up at lifetime + max walltime + 15 minutes.
- **Root volume.** It holds the worker image and `/scratch`. 30 GiB at 125 MiB/s, the gp3 baseline,
  costs about $0.0033 per worker-hour; raise `disk=` for big images or scratch data, and
  `throughput=` for faster image pulls. Above 750 MiB/s, IOPS rise with throughput, as gp3 requires.
- **Instance types** are fixed: Graviton `c7g`, `m7g`, `c8g`, `m8g`, `r8g`, `c9g`, `m9g`, `r9g`,
  filtered by your `vcpu` and `mem`.

## Set spend limits

```sh
aegc deploy e2e --pool geospatial --max-usd-day 5 --max-usd-month 50 --on-cap fail
aegc limits e2e --max-usd-day 10        # change one later
aegc limits e2e --max-usd-day none      # remove it
```

`--max-usd-day`, `--max-usd-month` and `--max-instance-hours-day` take a number or `none`.
`--on-cap fail` (the default) lets running tasks finish when a limit is reached; `--on-cap kill`
fails them. How limits are enforced, and what the estimates leave out, is in
[Spend limits](../concepts/cost.md#spend-limits).

## Set the lease and TTL

```sh
aegc deploy e2e --pool geospatial --lease 72h   # expire after 3 days without a running endpoint
aegc deploy e2e --pool geospatial --ttl 7d      # and in 7 days regardless
aegc deploy e2e --pool geospatial --ttl none    # remove the TTL
```

Durations are `<N>m`, `<N>h` or `<N>d`. The lease defaults to 48 hours and must be at least
3 hours; `--lease none` turns it off. `--ttl` counts from the deploy that sets it. See
[Lease and TTL](../concepts/architecture.md#lease-and-ttl).

## Change a deployment

Run `aegc deploy` again with the options you want to change. Options you leave out keep their
last values, and so do the limits, the lease and the TTL:

```sh
aegc deploy e2e --pool geospatial:max_vcpus=16 --dry-run   # show what would change
aegc deploy e2e --pool geospatial:max_vcpus=16
```

A redeploy is idempotent: unchanged resources are reported as `exists`, and a changed launch
template gets a new version. Running endpoints pick up the new manifest within a minute; you don't
need to restart them. An endpoint that started before the deployment existed does need a restart.

A pool you leave out of a redeploy is kept, with a warning, and stays in the manifest, the lease
and `aegc stop`/`resume`. To remove it, pass `--prune --yes`. `--prune` alone is a dry run of the
removal: it reports what it would delete, and the rest of the deploy goes ahead. With `--yes` it
deletes the pool's group, launch template and queues. Before changing anything, it refuses if a
pool to be removed has queued or running tasks; `--force` removes it anyway, losing them.

```sh
aegc deploy e2e --pool geospatial --prune          # shows which pools would go
aegc deploy e2e --pool geospatial --prune --yes    # removes them
```

New instances pick up a changed launch template; running ones keep what they booted with until they
drain.

A redeploy doesn't change the [deployment state](../concepts/architecture.md#deployment-state).
While the deployment is stopped, capped or expired, a redeploy keeps every group's minimum at 0,
and `aegc resume` sets them from `min_vcpus`.

Images built with aegc before 0.2 are refused, because their workers ignore spend limits, `aegc
stop` and the lease, and write no cost ledger. Rebuild them, or pass `--allow-old-image` to deploy
one anyway.

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
the file. Restart the endpoint afterwards: the template is read when a user endpoint process starts.

You can run several endpoints, each with its own `--name` (and its own container,
`aegc-endpoint-<name>`), for example one per deployment.

## Choose the env and walltime from the client

```python
from globus_compute_sdk import Executor

with Executor(
    endpoint_id=ENDPOINT_ID,
    amqp_port=5671,
    user_endpoint_config={"env": "geospatial"},  # which science environment
    resource_specification={"walltime_s": 3600},  # per-task settings
) as ex:
    fut = ex.submit(fn, arg)
```

- `user_endpoint_config={"env": ...}` selects the env for every task from this executor. Globus
  Compute starts a separate user endpoint process for each distinct config. Without it, the env
  given to `configure` applies.
- `resource_specification` can set `env` (overrides the above for these tasks), `pool` (an exact
  pool name), and `walltime_s` (1 to the pool's `max_walltime`, at most 41,400; default the pool's
  `walltime`, else 39,600). Any other key fails the task.
- The client's Python minor version must match the pool's. It's taken from the client
  automatically; you don't pass it.
