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
| `size=CLASS` | none | What one task gets: `small`, `medium`, `mem`, `large` or `whole`; see [Size classes](#size-classes) |
| `slots=N` or `slots=auto` | auto | Tasks run at once on each worker; `auto` is one per vCPU, or what the size class fits |
| `idle=S` | 300 | Seconds a worker waits with no work before leaving the group |
| `disk=GiB` | 30 | Root gp3 volume, 30 to 16,384 GiB |
| `throughput=MiBps` | 125 | Root volume throughput, 125 to 1,000 MiB/s |
| `walltime=S` | 39,600 | Default walltime for the pool's tasks |
| `max_walltime=S` | 41,400 | Most walltime a task may ask for (at most 41,400) |
| `lifetime=S` | 86,400 | Seconds (3,600 to 259,200) after which a worker stops claiming and leaves once its tasks finish |
| `on-demand` | Spot | Use on-demand instances instead of Spot |
| `spot` | Spot | Use Spot again, undoing an earlier `on-demand` |
| `python=3.NN` | every minor | Only the env's image for this Python minor |

`max=N` and `min=N`, the v0.1 spelling, still work. They count workers, so aegc converts them to
vCPUs (N × the low end of `vcpu=`) and prints a warning: `max=3` with the default `vcpu=2-8`
becomes `max_vcpus=6`.

Each `--pool` makes one pool per Python minor the env has an image for, named `<env>-py<NNN>`
from the env and the image's Python version, for example `geospatial-py314`. With `size=`, the
class is added: `geospatial-py314-mem`. So a deployment can have several pools for one env, one for
each minor and size class; `python=3.13` limits a `--pool` to one minor. See
[Several Python minors per env](images.md#several-python-minors-per-env).

Points to know:

- **Capacity is in vCPUs.** `max_vcpus=8` allows four 2-vCPU workers or one 8-vCPU worker,
  whichever Spot provides; see [Capacity in vCPUs](../concepts/architecture.md#capacity-in-vcpus).
  `max_vcpus` must be at least the high end of `vcpu=`, so that no single worker can exceed it, and
  must fit your account's vCPU quota. If you set a small `max_vcpus` without `vcpu=`, `deploy` lowers the
  default high end to fit: `max_vcpus=4` gives `vcpu=2-4`.
- **Memory per task.** Slots share the instance. With the defaults, a 2-vCPU, 4 GiB instance runs
  two tasks with about 2 GiB each. For memory-hungry or multi-threaded tasks, use a
  [size class](#size-classes).
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
  filtered by your `vcpu` and `mem`, and by the size class's memory per vCPU.

## Size classes

By default each task gets one vCPU and about 2 GiB. For tasks that need more, give a pool a size
class. It sets what one task gets, picks instances with enough memory per vCPU, and sets how many
tasks each worker runs:

```sh
aegc deploy e2e --pool geospatial --pool geospatial:size=mem,max_vcpus=16
```

| `size=` | Per task | Instances (`MemoryGiBPerVCpu` at least) | Pool name |
|---|---|---|---|
| none | 1 vCPU, about 2 GiB (slots by vCPU only) | not set | `<env>-py<NNN>` |
| `small` | 1 vCPU, 2 GiB | not set | `<env>-py<NNN>-small` |
| `medium` | 2 vCPUs, 8 GiB | 4 GiB (`m` and `r` families) | `<env>-py<NNN>-medium` |
| `mem` | 2 vCPUs, 16 GiB | 8 GiB (`r` families) | `<env>-py<NNN>-mem` |
| `large` | 8 vCPUs, 32 GiB | 4 GiB | `<env>-py<NNN>-large` |
| `whole` | the whole instance, one task at a time | not set | `<env>-py<NNN>-whole` |

Each size class is its own pool, with its own queue, group and `max_vcpus`, so the example above
deploys `geospatial-py314` (the default) and `geospatial-py314-mem` side by side.

**Slots.** With `slots=auto`, a worker runs as many tasks as both its vCPUs and its memory allow:
vCPUs ÷ the class's vCPUs, and memory ÷ (the class's memory × 0.9), whichever is smaller, at
least one. The 0.9 allows for memory the kernel, Docker and the worker keep. An `r8g.xlarge`
(4 vCPUs, 32 GiB) runs two `mem` tasks, and an `m8g.2xlarge` (8 vCPUs, 32 GiB) one `large` task.
`whole` always runs one. `slots=N` still sets the count yourself.

**Worker sizes.** If `vcpu=` isn't given and its low end is less than the class's vCPUs, `deploy`
raises it, so `size=large` gives `vcpu=8-8` by default. With the default `max_vcpus=8` that's one
`large` task at a time; raise `max_vcpus` (for example `size=large,max_vcpus=32,vcpu=8-16`). A
`vcpu=` range below the class fails:
`size=large gives each task 8 vCPUs, so workers need at least that many: vcpu=4-8 is too small`.
For `whole`, set `vcpu=` and `mem=` to the instance you want each task to have: what a task is
sure to get is the low end of `vcpu=` and `mem=`.

**Scaling.** The scaler asks for the class's vCPUs per task (`whole`: the top of `vcpu=` per task,
so each task gets an instance even if Auto Scaling picks the largest size). `max_vcpus` bounds the
pool as before.

**Choosing a size from the client.** Tasks ask for a class by name, or for what they need:

```python
resource_specification={"size": "mem"}                  # a pool of that class
resource_specification={"cpus": 4, "memory_gib": 24}    # the cheapest pool that fits
```

With `cpus` and `memory_gib` (positive numbers; either alone works), the engine takes the env's
pools for the client's Python minor, keeps those whose tasks get at least that many vCPUs and GiB,
and picks the one that holds the fewest vCPUs per task, then the least memory. `size` wins if both
are given. Without either, the task goes to the cheapest pool, normally the one without a size
class. A pool without `size=` counts as `small`. If nothing fits, the task fails at once:

```
no pool for env 'geospatial' gives a task 4 vCPUs and 24 GiB; available: geospatial-py314 (small:
1 vCPU, 2 GiB per task), geospatial-py314-mem (mem: 2 vCPU, 16 GiB per task). Deploy one with
--pool geospatial:size=...
```

`resource_specification={"pool": ...}` names a pool directly and skips the size check.

Points to know:

- **Rebuild worker images.** Workers built before size classes ignore them and run one task per
  vCPU. Rebuild the env's images with this release's code before you deploy a sized pool.
- **Rebuild the endpoint image** (`aegc endpoint build`, then `stop` and `start`). An older engine
  fails tasks that pass `size`, `cpus` or `memory_gib` as unsupported keys.
- **Env names get shorter.** The pool name must fit in 40 characters; with `size=medium`, env
  names can have at most 27.
- **A redeploy is per pool name.** `--pool geospatial:size=mem` and `--pool geospatial` are
  different pools, so to change a sized pool, give its `size=` again.

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
  pool name), `walltime_s` (1 to the pool's `max_walltime`, at most 41,400; default the pool's
  `walltime`, else 39,600), and `size`, `cpus` and `memory_gib` (see
  [Size classes](#size-classes)). Any other key fails the task; `vcpus`, `memory` and a few other
  common spellings get a "did you mean" hint.
- The client's Python minor version picks the pool among the env's minors, and must match one of
  them. It's taken from the client automatically; you don't pass it.

## Give tasks access to your data

Tasks can read and write under your own S3 prefixes once you grant them, and return large outputs
as `s3://` URIs:

```sh
aegc deploy e2e --pool geospatial \
  --grant read=s3://my-lab-data/inputs/ --grant write=s3://my-lab-results/runs/
```

See [Read and write your data](data.md).
