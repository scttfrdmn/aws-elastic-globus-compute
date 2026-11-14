# Troubleshooting

Start with `aegc status <d>` and the worker logs (`aws logs tail /aegc/<d> --follow`). Errors that
come back to the client as task failures carry a code: `AEGC_CONFIGURATION`,
`AEGC_WALLTIME_EXCEEDED`, or a worker system failure such as `CHILD_CRASHED`.

## Python version mismatch

**Symptom:** every task fails at once with

```
no worker image for Python 3.13 in env 'geospatial'; available: 3.14 (geospatial-py314). Submit
from a matching Python version.
```

**Cause:** Globus Compute serializes functions with the client's Python, so workers must run the
same minor version. The client is on 3.13 and the only image for `geospatial` runs 3.14.

**Fix:** submit from Python 3.14 (for example `uv venv --python 3.14`). In v0.1 the aarchsci images
run 3.14, and each env has one image. With `resource_specification={"pool": ...}` the message reads
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
| `walltime_s must be between 1 and 41400` | Walltime out of range | Split the task or lower `walltime_s` |

The engine reads the manifest only when its process starts, so after fixing a deployment, restart
the endpoint.

`user_endpoint_config` accepts only `env`, matching `^[a-z][a-z0-9-]{0,40}$`; Globus Compute
rejects anything else before it reaches aegc.

## Tasks stay queued and no workers start

Check the pool in `aegc status`:

- **`desired` is 0:** the engine isn't scaling. Is the endpoint running (`aegc endpoint ... logs`)?
  Was it restarted after the last deploy? Is `max` greater than 0?
- **`desired` > 0 but nothing in service:** look at the last scaling activity. Common causes:
  the account's Spot vCPU quota for standard instances, no Spot capacity for the allowed types, or
  instance profile propagation right after a first deploy. After 15 minutes like this the engine
  pauses scale-out for 10 minutes. Raising the quota, widening `vcpu=`, or using `on-demand`
  usually helps.
- **Instances start but the worker doesn't:** check `/aegc/<d>` for a log stream from the instance.
  With none, the boot failed (Docker, ECR login or the image pull). The boot script terminates the
  instance on failure, and the watchdog does if the worker stays down. A common cause is an image
  digest that has expired from the repository; rebuild and redeploy.

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
`walltime_s` in `resource_specification` (at most 41,400) or split the work.

## Reading open data fails with Access Denied

Workers have no S3 permissions outside aegc's payload prefix, so signed requests to other buckets
are denied, even public ones. Use unsigned requests, for example `s3fs.S3FileSystem(anon=True)`,
`botocore.UNSIGNED` in boto3, or `AWS_NO_SIGN_REQUEST=YES` for GDAL. Private and requester-pays
buckets can't be read in v0.1. See [Security model](../concepts/security.md#data-access).

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
- **`deployment name ... must be 1-20 chars`** (or pool name): names are lowercase letters, digits
  and hyphens, start with a letter, and don't end in or contain double hyphens. Pool names are
  `<env>-py<NNN>`, so envs longer than 14 characters can't be deployed in v0.1.
- **`no image recorded for env 'x'`:** run `aegc image build --env x ...` first.
- **`no default VPC in this region`:** aegc needs the region's default VPC.
- **`tasks are queued or running: ...; use force`:** wait, or `aegc destroy <d> --yes --force`.
- **`deployments still exist: ...`** from `unbootstrap`: destroy them first.
