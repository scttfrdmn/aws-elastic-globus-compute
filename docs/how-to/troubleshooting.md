# Troubleshooting

Start with `aegc status <d>` and the worker logs (`aws logs tail /aegc/<d> --follow`). Errors that
come back to the client as task failures carry a code:

| Code | Meaning | Retried | See |
|---|---|---|---|
| `AEGC_CONFIGURATION` | The task can't be placed (env, Python, walltime, keys) | No | [below](#other-configuration-errors) |
| `AEGC_WALLTIME_EXCEEDED` | The task ran past its walltime and was killed | No | [below](#task-exceeded-its-walltime) |
| `AEGC_BUDGET_EXCEEDED` | The deployment reached a spend limit, or the limits admit no worker | No | [below](#aegc_budget_exceeded) |
| `AEGC_STOPPED` | Someone ran `aegc stop` | No | [below](#aegc_stopped) |
| `AEGC_EXPIRED` | The deployment's lease or TTL expired | No | [below](#aegc_expired) |
| `AEGC_NO_CAPACITY` | The pool got no workers for an hour | No | [below](#aegc_no_capacity) |
| `CHILD_CRASHED`, `WORKER_LOST_REPEATEDLY`, `PAYLOAD_MISSING` | Worker system failures | Once | [below](#tasks-fail-with-a-system-error) |

## Python version mismatch

**Symptom:** every task fails at once with

```
no worker image for Python 3.13 in env 'geospatial'; available: 3.14 (geospatial-py314). Submit
from a matching Python version.
```

**Cause:** Globus Compute serializes functions with the client's Python, so workers must run the
same minor version. The client is on 3.13 and the only image for `geospatial` runs 3.14.

**Fix:** submit from Python 3.14 (for example `uv venv --python 3.14`). The aarchsci images run
3.14, and each env has one image. With `resource_specification={"pool": ...}` the message reads
`pool '<name>' runs Python 3.14, but this client is Python 3.13` instead.

## Other configuration errors

These fail the task at once, without a retry, as `AEGC_CONFIGURATION`:

| Message starts with | Cause | Fix |
|---|---|---|
| `deployment 'x' not found in <region>` | No manifest `/aegc/x/manifest` in the endpoint's region, or the credentials can't read it | Check `--deployment` and the region given to `aegc endpoint configure`; run `aegc deploy x`; restart the endpoint |
| `AWS setup failed:` | The engine couldn't create AWS clients or its result queue at start (often credentials) | See [Credentials in the endpoint](#credentials-in-the-endpoint); restart the endpoint |
| `no pool for env 'x' in 'd'; envs: [...]` | No pool in the deployment has that env | Check `user_endpoint_config` / `resource_specification`, or deploy a pool for the env |
| `no pool 'x' in deployment 'd'` | `resource_specification={"pool": "x"}` names a pool that doesn't exist | Use a name from `aegc status` |
| `unsupported resource_specification keys [...]` | Keys other than `env`, `pool`, `walltime_s` | Remove them |
| `walltime_s must be between 1 and N` | Walltime out of range; N is the pool's `max_walltime`, at most 41,400 | Split the task, lower `walltime_s`, or redeploy the pool with a higher `max_walltime=` |

Running engines re-read the manifest every minute, so a fixed deployment takes effect without a
restart. The exceptions are `deployment ... not found` and `AWS setup failed`, which happen when
the engine starts: restart the endpoint after fixing those.

`user_endpoint_config` accepts only `env`, matching `^[a-z][a-z0-9-]{0,40}$`; Globus Compute
rejects anything else before it reaches aegc.

## Tasks fail because of the deployment state

These codes mean the deployment isn't `running`. They're final: the endpoint doesn't retry them, and
resuming the deployment doesn't resubmit them. Submit the tasks again once it's running. `aegc status
<d>` shows the state and why.

### AEGC_BUDGET_EXCEEDED

```
deployment 'e2e' has reached a spend limit ($5.0123 of $5/day); new tasks are refused until
2026-11-15 00:00 UTC. To resume sooner, raise the limit: `aegc limits e2e --max-usd-day N --resume`
(or --max-usd-month).
```

**Cause:** the deployment's estimated spend reached one of its limits, and an engine capped it.

**Fix:** wait for the end of the UTC day or month, when the deployment runs again by itself (and
its group minimums come back), or raise the limit and resume:
`aegc limits e2e --max-usd-day 10 --resume`. Without a higher limit, `--resume` refuses with
`still over a limit (...); raise it`. Check the spend first with `aegc cost e2e`. See
[Spend limits](../concepts/cost.md#spend-limits).

The same code, with a different message, means the limits left no room for even one worker:

```
pool 'geospatial-py314': the spend limits (limits $5/day (on_cap=fail)) leave no room for a
worker, so tasks waited 10 min without one. Raise a limit with `aegc limits e2e --max-usd-day N`
(or --max-usd-month).
```

**Cause:** tasks waited with no worker running for 10 minutes, because admission wouldn't start
one: what's left under a limit, spread over the next hour with a 1.2× margin, is less than one
worker's (deliberately high) price. The deployment isn't capped; only the waiting tasks fail.

**Fix:** raise the limit (`aegc limits e2e --max-usd-day N`), or narrow `vcpu=` to cheaper sizes,
then submit again.

### AEGC_STOPPED

```
deployment 'e2e' is stopped (aegc stop); resume it with `aegc resume e2e`.
```

**Cause:** someone ran `aegc stop e2e`. With `--hard`, running tasks failed with this code too.

**Fix:** `aegc resume e2e`.

### AEGC_EXPIRED

```
deployment 'e2e''s lease expired: no endpoint renewed it (no endpoint renewed the lease for 48 h);
resume it with `aegc resume e2e`.
```

or, after a TTL:

```
deployment 'e2e' reached its TTL (TTL reached at 2026-11-21T14:05:00 UTC); resume it with
`aegc resume e2e` (and redeploy with a later --ttl to move it).
```

**Cause:** no running endpoint renewed the deployment's lease for its length (48 hours by default),
or its TTL passed. See [Lease and TTL](../concepts/architecture.md#lease-and-ttl).

**Fix:** `aegc resume e2e`, which also pushes the lease forward. Keep an endpoint running while you
use the deployment, or lengthen the lease with `aegc deploy e2e --pool <env> --lease 7d`. After a
TTL, set a new one with `--ttl 7d` if you want another end date; the passed one isn't scheduled
again. If the lease expired while an endpoint was running, check that the endpoint's credentials
have the current [operator policy](../reference/iam.md#operator), which renews it.

### AEGC_NO_CAPACITY

```
pool 'geospatial-py314' got no workers for 60 min (<last scaling error>). If it's the Spot vCPU
quota, request more: aws service-quotas request-service-quota-increase --service-code ec2
--quota-code L-34B43A08 --desired-value N. Widening `vcpu=` or using `on-demand` can also help.
```

**Cause:** the pool wanted capacity, but no instance was in service for `stall_fail_s` (1 hour), so
the engine failed its queued tasks for the pool. The message includes the group's last failed
scaling activity, if there was one. Running tasks aren't affected.

**Fix:** read the scaling error. For a Spot vCPU quota, run the printed command with a larger
value (a new account's default is 5); for no Spot capacity, widen `vcpu=`, or use `on-demand`.
Then submit again. See [Tasks stay queued](#tasks-stay-queued-and-no-workers-start).

## Tasks stay queued and no workers start

Check the pool in `aegc status`:

- **The deployment isn't `running`:** new tasks fail at once, and queued ones are failed; see
  [above](#tasks-fail-because-of-the-deployment-state).
- **`desired` is 0:** the engine isn't scaling. Is the endpoint running (`aegc endpoint ... logs`)?
  Is `max_vcpus` greater than 0? If the deployment has spend limits, the engine may be holding
  scale-out back: its log says `spend limits admit 0 of N more vCPUs`. With no worker running,
  those tasks fail with [`AEGC_BUDGET_EXCEEDED`](#aegc_budget_exceeded) after 10 minutes; raise
  the limit to let them run.
- **`desired` > 0 but nothing in service:** look at the last scaling activity. Common causes:
  the account's Spot vCPU quota for standard instances, no Spot capacity for the allowed types, or
  instance profile propagation right after a first deploy. After 15 minutes like this the engine
  pauses scale-out for 10 minutes, and after an hour it fails the queued tasks with
  [`AEGC_NO_CAPACITY`](#aegc_no_capacity). `aegc status` shows the quota and usage; a new account's
  Spot default is 5 vCPUs. Raising the quota, widening `vcpu=`, or using `on-demand` usually helps.
- **Instances start but the worker doesn't:** check `/aegc/<d>` for a log stream from the instance.
  With none, the boot failed (Docker, ECR login or the image pull). The boot script terminates the
  instance on failure, and the watchdog does if the worker stays down. A common cause is an image
  digest that's no longer in the repository (deleted by hand: `deploy` tags the images it uses so
  the lifecycle rule keeps them); rebuild and redeploy.

The first task from zero workers takes about 40 s even when everything works.

## Results don't arrive

- **The endpoint isn't running.** `aegc status` shows results waiting in a result queue. Start the
  endpoint. If its container was restarted, submit any task to start the user endpoint process
  again; it then delivers the waiting results.
- **The task is still running.** `running` counts in-flight tasks. Tasks can run up to their
  walltime (default 11 h).
- **Dead-lettered messages.** A non-zero `dead-lettered` count means messages the workers couldn't
  decode or never finished after 6 receives. Inspect them in the `aegc-<d>-<pool>-dlq` queue.

## Tasks fail with a system error

- `CHILD_CRASHED: killed by signal SIGKILL` usually means the task ran out of memory. Raise `mem=`
  or lower `slots=` (see [Configuration](configuration.md#size-a-pool)).
- `WORKER_LOST_REPEATEDLY: delivered N times without completing` means the task was claimed
  repeatedly and never finished: workers died under it, or it outlived its claim. Check memory use
  and the worker logs.
- `PAYLOAD_MISSING` means a large payload was gone from S3 (after 15 days, or deleted).

Each of these is retried once before the client sees it. See
[Delivery and idempotency](../concepts/delivery.md).

## Task exceeded its walltime

`AEGC_WALLTIME_EXCEEDED: task exceeded its walltime of N s and was killed`. Pass a longer
`walltime_s` in `resource_specification` (at most the pool's `max_walltime`, 41,400 at most), raise
the pool's default with `walltime=`, or split the work.

## Reading open data fails with Access Denied

Workers have no S3 permissions outside aegc's payload prefix, so signed requests to other buckets
are denied, even public ones. Use unsigned requests, for example `s3fs.S3FileSystem(anon=True)`,
`botocore.UNSIGNED` in boto3, or `AWS_NO_SIGN_REQUEST=YES` for GDAL. Private and requester-pays
buckets can't be read yet. See [Security model](../concepts/security.md#data-access).

## Credentials in the endpoint

The endpoint container reads credentials from your `~/.aws`, mounted read-only, and gets
`AWS_PROFILE` if you set one (via `--profile` or the environment). Credentials held only in
environment variables (`AWS_ACCESS_KEY_ID` and so on) aren't passed into the container. With IAM
Identity Center (SSO), run `aws sso login` on the host when the session expires; the container sees
the refreshed cache through the mount. Restart the endpoint if tasks failed with `AWS setup
failed` meanwhile.

## Endpoint problems

- **`aegc endpoint start` times out waiting for the endpoint id:** the first start registers the
  endpoint with Globus and `start` waits up to 90 s for `endpoint.json`. Check `aegc endpoint ...
  logs`; you may need to run `login` again.
- **AMQP connection drops:** make sure `amqp_port: 5671` is in the endpoint's `config.yaml`
  (`configure` adds it) and pass `amqp_port=5671` to `Executor` in clients.
- **`start` fails because the container exists:** run `aegc endpoint ... stop` first, which removes
  it.

## Deploy and destroy refuse

- **`... exists but isn't tagged as ...'s`:** a resource with aegc's name exists but wasn't created
  by aegc for this deployment (or region). aegc won't adopt or delete it. Rename your deployment or
  remove the other resource yourself.
- **`deployment name ... must be 1-20 chars`** (or `pool name ... 1-40`): names are lowercase
  letters, digits and hyphens, start with a letter, and don't end in or contain double hyphens.
  Pool names are `<env>-py<NNN>`, so envs can be up to 34 characters.
- **`no image recorded for env 'x'`:** run `aegc image build --env x ...` first.
- **`no default VPC in this region`:** aegc needs the region's default VPC.
- **`max_vcpus=N is smaller than one worker`** or **`vcpu=LO-HI allows workers larger than
  max_vcpus=N`:** no worker may be larger than the pool; lower the top of `vcpu=` or raise
  `max_vcpus`.
- **`lifetime= must be 3600-259200 s (1-72 h)`:** fix the pool option.
- **`a lease shorter than 3 h could expire between renewals`:** use `--lease 3h` or more, or
  `--ttl` for a hard end.
- **`the 'x' image was built with aegc 0.1.0; its workers ignore spend limits, stop and the
  lease`:** rebuild it with `aegc image build --env x ...`, or pass `--allow-old-image`.
- **`root volume (disk=) must be 30-16384 GiB`**, **`gp3 throughput (throughput=) must be
  125-1000 MiB/s`**, **`walltime_s must not exceed max_walltime_s`:** fix the pool option.
- **`can't prune pools with queued or running tasks: ...; wait, or add --force`** from
  `deploy --prune`: nothing was changed. Wait, or add `--force` to delete the pool and lose those
  tasks.
- **`note: --prune would remove pool(s) ...; this is a dry run of that part`:** add `--yes` to
  remove them.
- **`warning: keeping pool(s) not listed here`** from `deploy`: a redeploy keeps pools it isn't
  given; add `--prune --yes` to remove them.
- **`warning: max=N counts workers`:** use `max_vcpus=`/`min_vcpus=`; aegc converted the value.
- **`tasks are queued or running: ...; use force`:** wait, or `aegc destroy <d> --yes --force`.
- **`aegc: N resource(s) of <d> remain`** from `destroy`: the lines marked `remaining` are still
  there after the delete. Instances can take a few minutes to terminate; run
  `aegc destroy <d> --verify` later to check again, and `aegc footprint <d>` to see everything.
- **`--hard fails running tasks; add --yes to confirm`** from `stop`: add `--yes`.
- **`deployments still exist: ...`** from `unbootstrap`: destroy them first.
