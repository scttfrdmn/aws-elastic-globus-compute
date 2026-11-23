# Troubleshooting

Start with `aegc status <d>`, the worker logs (`aws logs tail /aegc/<d> --follow`), and for one
task, `aegc provenance <d> <task id>` (see [Watch tasks](observability.md)). Errors that come back
to the client as task failures carry a code:

| Code | Meaning | Retried | See |
|---|---|---|---|
| `AEGC_CONFIGURATION` | The task can't be placed (env, Python, size, walltime, keys) | No | [below](#other-configuration-errors) |
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

**Fix:** submit from Python 3.14 (for example `uv venv --python 3.14`), or give the env an image
for 3.13 and redeploy: build one from a 3.13 base with `aegc image build --env geospatial --base
REF`, or register one, then `aegc deploy <d> --pool geospatial` adds `geospatial-py313` next to
`geospatial-py314`. See [Several Python minors per env](images.md#several-python-minors-per-env).
With `user_endpoint_config={"pool": ...}` the message reads
`pool '<name>' runs Python 3.14, but this client is Python 3.13` instead.

## Other configuration errors

These fail the task at once, without a retry, as `AEGC_CONFIGURATION`:

| Message starts with | Cause | Fix |
|---|---|---|
| `deployment 'x' not found in <region>` | No manifest `/aegc/x/manifest` in the endpoint's region, or the credentials can't read it | Check `--deployment` and the region given to `aegc endpoint configure`; run `aegc deploy x`; restart the endpoint |
| `AWS setup failed:` | The engine couldn't create AWS clients or its result queue at start (often credentials) | See [Credentials in the endpoint](#credentials-in-the-endpoint); restart the endpoint |
| `no pool for env 'x' in 'd'; envs: [...]` | No pool in the deployment has that env | Check `user_endpoint_config`, or deploy a pool for the env |
| `no pool 'x' in deployment 'd'` | `user_endpoint_config={"pool": "x"}` names a pool that doesn't exist | Use a name from `aegc status` |
| `AWSEngine runs each task on one instance; num_nodes must be 1, not N` | `resource_specification` asks for several nodes | Use `{"num_nodes": 1}`, or leave `resource_specification` out |
| `AWSEngine doesn't launch MPI ranks; use cpus= for a multi-core task` | `resource_specification` has `ranks_per_node` or `num_ranks` above 1, or `launcher_options` | Drop them; ask for cores with `user_endpoint_config={"cpus": N}` |
| `unsupported task options [...]; AWSEngine accepts [...]` | A key aegc doesn't know reached the engine (in a `resource_specification`). A "did you mean" hint follows for `vcpus`, `memory` and similar | Put aegc options in `user_endpoint_config` |
| `task option cpus must be a positive number` (or `memory_gib`) | Zero, negative or not a number | Pass a number above 0 |
| `num_nodes, ranks_per_node and num_ranks must be numbers` | A `resource_specification` MPI key isn't a number | Use `{"num_nodes": 1}`, or leave `resource_specification` out |
| `this submission doesn't say its Python version (Globus Flows doesn't send one), and env 'x' has worker images for 3.13, 3.14; add "python": "3.14" ...` | The submission carried no Python version (from Globus Flows, say), and the env has images for several minors | Add `"python": "3.NN"` to `user_endpoint_config`; see [Flows](flows.md#write-the-flow) |
| `no size='mem' pool for env 'x'; available: ...` | No pool of that size class for the env and the client's Python | Deploy one with `--pool x:size=mem`, or ask for another size |
| `no pool for env 'x' gives a task N vCPUs and M GiB; available: ...` | Every pool's tasks get less than `cpus` or `memory_gib` | Deploy a larger [size class](configuration.md#size-classes), or ask for less |
| `walltime_s must be between 1 and N` | Walltime out of range; N is the pool's `max_walltime`, at most 41,400 | Split the task, lower `walltime_s`, or redeploy the pool with a higher `max_walltime=` |

Running engines re-read the manifest every minute, so a fixed deployment takes effect without a
restart. The exceptions are `deployment ... not found` and `AWS setup failed`, which happen when
the engine starts: restart the endpoint after fixing those.

`user_endpoint_config` accepts only `env`, `python`, `pool`, `size`, `cpus`, `memory_gib` and
`walltime_s`, with the types in the
[configuration reference](../reference/configuration.md#user_endpoint_config); Globus Compute
rejects anything else before it reaches aegc. If it rejects `python` (or `size`, `cpus` or
`memory_gib`), the endpoint was configured by an older aegc: run `aegc endpoint ... build` and
`configure`, then `stop` and `start`.

### Options in resource_specification are ignored, or the submission fails with HTTP 422

The Globus Compute web service validates `resource_specification` as an MPI request. Without
`num_nodes` it refuses the submission (`422 REQUEST_MALFORMED`); with it, it keeps only
`num_nodes`, `ranks_per_node`, `num_ranks` and `launcher_options` and silently drops everything
else. So `walltime_s`, `pool`, `size`, `cpus`, `memory_gib` and `env` set there have no effect.
Move them to `user_endpoint_config`; see
[Choose the env, size and walltime](configuration.md#choose-the-env-size-and-walltime-from-the-client).

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

**Cause:** the deployment's estimated spend reached one of its limits, and an engine capped it (or, with a [cloud endpoint](cloud-endpoint.md#the-evaluator), `aegc evaluate`).

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
`walltime_s` in `user_endpoint_config` (at most the pool's `max_walltime`, 41,400 at most), raise
the pool's default with `walltime=`, or split the work.

## Reading S3 data fails with Access Denied

Workers have no S3 permissions outside aegc's payload prefix and the deployment's grants, so signed
requests to other buckets are denied, even public ones.

- **Open data:** use unsigned requests, for example `s3fs.S3FileSystem(anon=True)`,
  `botocore.UNSIGNED` in boto3, or `AWS_NO_SIGN_REQUEST=YES` for GDAL.
- **Your own buckets:** grant the prefix, `aegc deploy <d> --grant read=s3://bucket/prefix/`.
  Remember that `--grant` replaces the deployment's grants, so list them all.
- **Requester-pays buckets:** grant the prefix and pass `RequestPayer="requester"` (boto3) or
  `AWS_REQUEST_PAYER=requester` (GDAL). Without it, S3 denies the request.
- **Reading back what tasks wrote:** a write grant doesn't allow reads; add a read grant on the
  same prefix.
- **Another account's bucket, or a KMS key:** for reads, the bucket policy must allow the task
  role, `arn:aws:iam::<account>:role/aegc/aegc-<d>-task-<region>` (and the worker role while
  pools with images from before aegc 0.4 remain), and a customer managed key must allow
  `kms:Decrypt`. Writes to another account's bucket are always denied.

See [Read and write your data](data.md).

## aegc.outputs fails

- **`PermissionError: no write grant: deploy with ...`:** the deployment has no write grant. Add
  `--grant write=s3://bucket/prefix/`; running workers see it within a minute.
- **`PermissionError: s3://... isn't inside a write grant (...)`:** `prefix=` points outside every
  write grant. Use one of the grants listed, or add one.
- **`ModuleNotFoundError: No module named 'aegc.outputs'`:** the worker image was built with an
  older aegc. Rebuild it from the current source tree.
- **`FileNotFoundError: no file '...' to upload`:** `put` takes bytes or the path of a file that
  exists. To upload text, pass `text.encode()`.
- **`AccessDenied` on a write:** the bucket isn't in the deployment's account; write grants only
  reach your own account's buckets.

## Isolated tasks

Tasks in pools with images built by aegc 0.4 or later run with the task role, not the worker role
(see [Task isolation](../concepts/security.md#task-isolation)). Some code notices:

- **`AccessDenied` on SQS, Auto Scaling, the deployment's state or ledger, or aegc's payloads:**
  expected. The task role holds only your grants and a read of the manifest.
- **Code that asks the instance metadata service directly** (for the instance id, its region or
  credentials) gets its connection refused: the host's firewall blocks it for task processes, and
  `AWS_EC2_METADATA_DISABLED=true` stops the AWS SDKs from trying. Use `AWS_REGION` for the
  region, and [`details["aegc"]`](observability.md#provenance) or `aegc provenance` for the
  instance.
- **`NoCredentialsError`, or `task credentials unavailable` (HTTP 503 from `127.0.0.1`):** the agent
  couldn't renew the task role's session. Check the worker's log for the STS error.
- **Presigned URLs from a task stop working early:** they last only as long as the task role's
  session, at most an hour. Presign on your own machine.
- **The worker exits at start with an `AssumeRole` error:** the worker role couldn't assume the
  task role, so the worker claims nothing rather than run tasks unisolated. systemd restarts it
  (up to three times in 10 minutes), which covers IAM's delay right after a first deploy; after
  that the watchdog replaces the instance. If it persists, check that the worker role's
  `aegc-worker` policy has `AssumeTaskRole`, and run `aegc deploy` again.
- **`this pool isolates tasks, but the agent isn't root; tasks run as uid N with the agent's
  credentials`** in a worker's log: the worker container didn't start as root, so tasks weren't
  isolated. The user data runs it with `--user 0`; this means something else started it.
- **Tasks can see each other's processes and files** on a worker: they share uid 57439. Isolation
  separates tasks from the worker role, not from each other.

## Flows and Timers

- **`ENDPOINT_ACCESS_FORBIDDEN (Forbidden)`** as soon as the action starts: the run was started by
  an identity other than the endpoint owner's. Start it as the endpoint owner; see
  [Use the right identity](flows.md#use-the-right-identity).
- **`NOT_FOUND` for a flow that exists:** your identity isn't one of the flow's starters (or
  viewers), so Flows doesn't show it.
- **`403 FUNCTION_NOT_PERMITTED`** at submission: the endpoint was configured with
  `--allow-function`, and this function isn't on the list. Add its id with `configure
  --allow-function ...` (every id you want, not just the new one), then `stop` and `start`.
- **The action times out:** `WaitTime` (300 s by default) is shorter than a cold start, the wait in
  the queue and the task's walltime together. Raise it.
- **The Python refusal** (`this submission doesn't say its Python version ...`): add `"python"` to
  the action's `user_endpoint_config`.
- **The run fails on the size of its state:** the result is too large for Flows (about 200 KB).
  Return an `s3://` URI from [`aegc.outputs`](data.md#return-large-outputs) instead.
- **No `details["aegc"]` in the action's output:** Flows doesn't pass it. Use
  `aegc provenance <d> <task id>`.
- **`aegc: --allow-function takes function ids (UUIDs), or none to allow any`:** pass the ids
  `register_function` printed.

## Metrics and provenance

- **`aegc metrics` says `no data in this window`:** no tasks ran in the window, or the workers'
  images predate aegc 0.4 and emit no metrics. Metrics also arrive a few minutes late; try
  `--since 1d`.
- **The capacity figures are missing:** the groups' metrics aren't enabled. `deploy` reports
  `kept  auto scaling group metrics ... (not enabled: ...)` when the deployer policy lacks
  `autoscaling:EnableMetricsCollection`; update the policy and deploy again.
- **`aegc: no task_finished line for <uuid> in /aegc/<d> since 14d`:** the task ran on a worker
  from an image older than 0.4, ran more than 14 days ago (or before `--since`), or hasn't
  finished. An `AccessDeniedException` means the credentials lack the operator policy's
  `logs:FilterLogEvents`.

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

## The cloud endpoint is offline

Start with `aegc endpoint --deployment <d> status` and the host's logs:

```sh
aws logs tail /aegc/<d> --log-stream-name-prefix endpoint/ --since 1h
```

- **`instance: none yet`, or instances keep changing:** the group is launching, or keeps
  replacing a host that fails to boot. `aws autoscaling describe-scaling-activities
  --auto-scaling-group-name aegc-<d>-endpoint` shows each launch and why it ended.
- **The boot failed.** A host that can't pull the image, read its secret, restore the login,
  configure the endpoint or start it retries, then logs `aegc: endpoint boot failed: <why>` and
  marks itself unhealthy, so the group replaces it. The boot script's output is in
  `/var/log/aegc-boot.log` and in the console, which works without any access to the instance:

    ```sh
    aws ec2 get-console-output --instance-id <id> --output text | grep aegc
    ```

    `couldn't read the secret aegc-<d>-endpoint` means the secret is gone or its policy doesn't
    name this role: deploy again. `couldn't pull` means the image isn't in `aegc/endpoint` any more.
- **The host runs, but Globus says offline.** Read `endpoint/<instance-id>`. Authentication errors
  mean the stored login no longer works (revoked by `globus-compute-endpoint logout`, say): run
  `aegc endpoint ... login` on your machine and deploy again, which replaces the host. If you
  started the local endpoint again, stop it: one endpoint id can't run in two places.
- **Tasks arrive but fail or hang.** The engine runs in user endpoint processes; their lines are in
  the same stream, prefixed `[uep]`. They start when the first task arrives, so there are none
  before that.
- **The alarm is in `ALARM` but the endpoint is fine.** The evaluator couldn't publish: look for
  `error:` lines in `endpoint/<instance-id>/evaluate`. A pass that can't tell (`endpoint status
  unknown`) publishes nothing, and missing data counts as offline after 15 minutes.
- **No alarm email.** Confirm the SNS subscription from the email AWS sent when you deployed with
  `--alarm-email`; nothing is delivered until you do.
- **The deployment is `capped` with `by: evaluator`.** The cloud endpoint's evaluator tripped a
  spend limit, as an engine would. See [AEGC_BUDGET_EXCEEDED](#aegc_budget_exceeded).

See [Run the endpoint in the cloud](cloud-endpoint.md).

## Deploy and destroy refuse

- **`... exists but isn't tagged as ...'s`:** a resource with aegc's name exists but wasn't created
  by aegc for this deployment (or region). aegc won't adopt or delete it. Rename your deployment or
  remove the other resource yourself.
- **`deployment name ... must be 1-20 chars`** (or `pool name ... 1-40`): names are lowercase
  letters, digits and hyphens, start with a letter, and don't end in or contain double hyphens.
  Pool names are `<env>-py<NNN>`, so envs can be up to 34 characters.
- **`no image recorded for env 'x'`:** run `aegc image build --env x ...` or
  `aegc image register --env x ...` first.
- **`no 'x' image for Python 3.12; have 3.13, 3.14`:** `python=` names a minor the env has no
  image for. Check `aegc image list --env x`.
- **`unknown size class 'huge'; one of: ...`:** use `small`, `medium`, `mem`, `large` or `whole`.
- **`size=large gives each task 8 vCPUs, so workers need at least that many: vcpu=4-8 is too
  small`:** raise the low end of `vcpu=`, or leave `vcpu=` out and `deploy` picks it.
- **`size=whole needs one worker size, e.g. vcpu=16; not vcpu=4-16`:** give one number,
  `vcpu=16`.
- **`size=mem sets each worker's slots from its vCPUs and memory; drop slots=4`:** remove `slots=`
  from a sized pool.
- **`size=mem needs a worker image built with aegc 0.3 or later ...`:** rebuild the env's image
  with `aegc image build` (or register a newer one) and deploy again.
- **`note: x also has images for Python 3.14; add a pool with --pool x:python=3.14`:** a redeploy
  updates only the minors already deployed. Add the new one with `python=`.
- **`warning: skipping x Python 3.12: built with aegc < 0.2`:** rebuild that minor's image, or
  `aegc image forget --env x --python 3.12`.
- **`pool name ... must be 1-40 chars ... (with size=medium, env names can be at most 27
  characters)`:** use a shorter env name for sized pools.
- **Grant errors:** `a grant is read=s3://bucket/prefix or write=s3://...`, `s3://x has no prefix:
  give s3://x/PREFIX, or s3://x/ to grant the whole bucket`, `grants can't use wildcards`,
  `not a valid general purpose S3 bucket name`, `... is aegc's own bucket`, `--grant none clears
  the grants; don't combine it with others`, and `deployment d's N grants need a ...-character IAM
  policy, but the worker and task roles have room for ...`, and `deployment d has N grants; at
  most 32`: fix the `--grant` values; nothing was changed.
- **`warning: <pools>: images built before aegc 0.4 run tasks with the worker role's credentials
  ...`:** those pools' tasks aren't isolated. Rebuild their images with `aegc image build` (or
  register images with aegc 0.4) and deploy again.
- **`kept  iam role policy  aegc-grants (worker role) (N pre-0.4 instance(s) still running; deploy
  again after they leave)`:** every pool is isolated now, but older workers still run tasks with
  the worker role, so it keeps the deployment's grants (changes and `--grant none` reach it too).
  Deploy again once those workers have left (`aegc status` shows the workers) to remove them.
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

## Building or registering images

Docker failures during `image build` and `image register` are reported as
``aegc: `<command>` failed (exit N)``, with Docker's own output above it.

- **`give either --base REF or --base-tag TAG (with --source)`**, or argparse's `one of the
  arguments --base-tag --base is required`: give exactly one.
- **`pin the base image (...): use repo@sha256:... or an immutable tag, not latest or no tag`**
  (or `pin the image` from `register`): add a digest or a tag.
- **`can't pull <ref>: ...`:** the image isn't local and can't be pulled. Log in to its registry
  (aegc logs in to this account's ECR itself), or check the name.
- **`<ref> is amd64, not arm64: workers are Graviton`:** build with `--platform linux/arm64`.
- **`couldn't run a shell in <base>: a base image needs sh`:** distroless bases can't be built
  over. Use a base with a shell, or build the image yourself.
- **`aegc workers need Python >= 3.11`** during a `--base` build: the base's Python is too old, or
  `--python` names the wrong one.
- **`aegc: <python> has no pip and no ensurepip; add pip to the base`:** install pip in the base
  (`apt-get install python3-pip` on Debian and Ubuntu).
- **pip can't resolve the install** during a `--base` build: the base has a package at a version
  `globus-compute-endpoint` can't use. Change the base, or build the image yourself and register it.
- **`base env modified outside allowlist: ...`** during a conda build: installing
  `globus-compute-endpoint` would change a package in the base.
- **`` `docker run <image> aegc-worker version` failed ``** from `register`: the image has no
  `aegc-worker` on `PATH`, an aegc older than 0.3, or an entrypoint that doesn't run the command
  it's given. Try `docker run --rm <image> aegc-worker version` yourself.
- **`<image> has aegc 0.2.0; registered images need aegc 0.3 or later`:** install the current aegc
  wheel in the image.
- **`<image> runs as uid 1000; workers need root or uid 57439`:** add `USER 57439` (after creating
  the user) to your Dockerfile.
- **`` aegc: `docker run --rm <image> aegc-worker selftest` failed ``:** the probe task didn't run
  in the image; its output above says why, often a missing or broken `globus-compute-endpoint`.
- **`no 'x' image for Python 3.12; have ...`** from `image forget`: check `aegc image list --env x`.
