# Operate a deployment

## Check status

```sh
aegc status e2e
aegc status e2e --json   # the same, as JSON
```

The output looks like this (values are illustrative):

```
deployment e2e (us-west-2): running
  spend: now $0.0866/h, today $0.4120, this month $3.18 (estimates) | limits $5/day (on_cap=fail)
  lease until 2026-11-16 14:05 UTC (renewed by endpoints)
  pool geospatial-py314: env geospatial, Python 3.14 | tasks queued 3, running 4, dead-lettered 0 | workers 2 in service, 4 vCPUs desired, max 8
    2x c8g.large spot | $0.0866/h | oldest task waiting 2 min | running tasks could cost up to $0.95 more
    last scaling activity: Successful: Launching a new EC2 instance: i-0123456789abcdef0
  spot vCPU quota 32, in use 4; pools can use up to 8
  result queue aegc-e2e-res-1a2b3c4d5e6f: 0 result(s)
```

- **The first line** gives the [deployment state](../concepts/architecture.md#deployment-state).
  When it isn't `running`, the next line says why and how to resume.
- **spend** is the estimated rate of the workers running now, and the estimated spend today and this
  month from the [cost ledger](../concepts/cost.md#cost-ledger), followed by the limits.
- **lease** and **TTL** show when the deployment will expire unless an endpoint renews it.
- **queued / running** are the task queue's visible and in-flight messages (SQS's approximate
  counts).
- **dead-lettered** counts messages in the pool's dead-letter queue. It should be 0.
- **workers** shows instances in service, desired capacity and the pool's maximum, in vCPUs (in
  workers for a pool deployed by v0.1).
- **The second pool line** lists the workers by instance type and Spot or on-demand, their
  estimated $/h, the age of the oldest waiting task (CloudWatch's `ApproximateAgeOfOldestMessage`
  over the last 10 minutes), and a worst case for the running tasks: each running for the pool's
  default walltime at its share of the pool's rate.
- **last scaling activity** is the Auto Scaling group's latest activity, including failures such
  as an exhausted Spot quota.
- **vCPU quota** lines compare the account's Spot (or on-demand) vCPU quota and current usage with
  what the deployment's pools can ask for (every pool at `max_vcpus`), with a warning and the
  `aws service-quotas` command to raise it when it falls short.
- **result queue** lines list each user endpoint process's queue. Results that stay there mean that
  endpoint process isn't running.

The spend figures need read access to the deployment's ledger, which the
[operator policy](../reference/iam.md#operator) has and the deployer policy doesn't; without it,
`status` leaves them out.

## Stop and resume

```sh
aegc stop e2e                # stop spending; running tasks finish
aegc stop e2e --hard --yes   # stop spending now; running tasks fail
aegc resume e2e
```

`aegc stop` sets the deployment's state to `stopped` and every group's minimum to 0. Within about a
minute, engines fail their queued tasks with `AEGC_STOPPED` and stop scaling, and workers stop
claiming, fail anything they still receive, finish their running tasks and leave their groups. It
doesn't need a running endpoint.

`--hard` (which needs `--yes`) also tells workers to fail their running tasks at once, waits
2 minutes, and terminates any instance still in a group. This is the emergency stop.

`aegc resume` sets the state back to `running`, restores each group's minimum from the manifest,
and pushes the lease forward. It works on a stopped, capped or expired deployment. A redeploy in
the meantime keeps the minimums at 0, so only `resume` brings warm workers back. Tasks that
failed while it was stopped aren't resubmitted; submit them again.

## Spend limits and cost

```sh
aegc limits e2e                              # show the limits
aegc limits e2e --max-usd-day 10             # change one (none removes it)
aegc limits e2e --max-usd-day 10 --resume    # and end a cap now
aegc cost e2e                                # estimated spend today
aegc cost e2e --since month --by type        # this month, by instance type
aegc cost e2e --since 7d --actual            # the last 7 days, and Cost Explorer's figure
```

Running endpoints pick up new limits within a minute. `--resume` only ends a `capped` state, and
refuses while spend is still over a limit (`still over a limit (...); raise it`), so raise the limit
in the same command. See
[Spend limits](../concepts/cost.md#spend-limits) and [Cost ledger](../concepts/cost.md#cost-ledger).

`aegc cost`, `aegc limits --resume` and the spend lines of `status` read the ledger, so they need
the deployment's operator credentials (the deployer policy has no ledger access). The operator
policy also grants `ce:GetCostAndUsage` for `--actual`. `aegc cost` works after the deployment is
destroyed.

## Run the endpoint

```sh
# A shell function, so this works in zsh (the macOS default) as well as bash.
ep() { aegc endpoint --name aegc-e2e --deployment e2e --env geospatial "$@"; }
ep start         # start the container (restarts with Docker unless stopped)
ep logs          # follow the container's log
ep id            # print the endpoint id
ep test          # submit a probe task and 10 squares (-n N for more)
ep stop          # stop the endpoint cleanly, then the container
```

`stop` runs `globus-compute-endpoint stop` inside the container before stopping it; a bare
`docker stop` leaves a stale pid file (the entrypoint removes it on the next start).

Restart the endpoint (`stop`, then `start`) after you reconfigure it or rebuild its image. A
redeploy doesn't need a restart: running engines re-read the manifest every minute.

When the endpoint stops, nothing in AWS stops with it. Queued tasks still run, workers still drain
when idle, and results wait in the result queue for up to 14 days. Spend limits aren't checked
while no endpoint runs, and if none runs for the length of the lease (48 hours by default), the
deployment expires; `aegc resume` brings it back.

## Read logs

- **Endpoint:** `ep logs` follows the container's output. Engine messages
  (`aegc.engine`) go to the user endpoint process's log under `/home/gc/.globus_compute/` in the
  container. To find them:

    ```sh
    docker exec aegc-endpoint-aegc-e2e find /home/gc/.globus_compute -name '*.log'
    ```

- **Workers:** CloudWatch Logs group `/aegc/<d>`, one stream per instance, `<pool>/<instance-id>`,
  kept for 14 days. Each claim and its outcome are logged with the duration:

    ```sh
    aws logs tail /aegc/e2e --follow
    ```

- **Worker boot:** the user-data script logs to `/var/log/aegc-boot.log` on the instance. Instances
  have no SSH access; the script's output usually also appears in the EC2 console output
  (`aws ec2 get-console-output --instance-id <id>`).

## Scale

The engine scales each pool out from queue depth, every 10 s and on each submission, up to the
pool's `max_vcpus` and within what the spend limits admit. It never scales in. Each worker leaves
the group after `idle` seconds (default 300) with nothing to do, decrementing desired capacity, so
the pool shrinks as work runs out. A worker stays up if leaving would take the group below
`min_vcpus`. After its `lifetime` (default 24 hours), a worker stops claiming and leaves once its
tasks finish; at the group's minimum it terminates without lowering desired capacity, so the group
launches a fresh replacement.

**Stall breaker.** If a pool has wanted capacity for 15 minutes with no instance in service, the
engine stops asking for more for 10 minutes and logs an error. Check the group's last scaling
activity in `aegc status`. If it still has none after `stall_fail_s` (1 hour), the engine fails its
queued tasks for the pool with `AEGC_NO_CAPACITY`; see
[Troubleshooting](troubleshooting.md#aegc_no_capacity).

**Watchdog.** Every 2 minutes after boot, a timer on each instance checks the worker. If the
worker service is down, or hasn't touched its heartbeat file for 600 s, and the instance has been up
for at least 900 s, the watchdog terminates the instance and decrements desired capacity. It does
the same to an instance older than the pool's lifetime + max walltime + 15 minutes (35.75 hours
with the defaults). If decrementing is refused because the group is at its minimum, it terminates
the instance for a replacement.

## Clean up result queues

Each user endpoint process creates a result queue, `aegc-<d>-res-<12 hex>`. A running engine
refreshes its queue daily. To remove queues that are empty and haven't been touched for 14 days:

```sh
aegc reap e2e          # dry run: lists queues it would delete or keep, and why
aegc reap e2e --yes
```

`aegc destroy` removes all of a deployment's result queues anyway.

## Destroy a deployment

```sh
aegc destroy e2e          # dry run
aegc destroy e2e --yes
```

`destroy` checks the task queues first and refuses if tasks are queued or running; `--force`
destroys anyway, and those tasks are lost. Then it deletes, in order:

1. the Auto Scaling groups (force-deleted, which terminates their instances; it waits up to
   10 minutes for each to go),
2. the launch templates,
3. the security group (retrying while the terminated instances' network interfaces go away),
4. all of the deployment's queues: task, dead-letter and result queues,
5. the lease's schedules, schedule group and scheduler role,
6. the worker instance profile and role,
7. the log group,
8. the deployment's `inuse-` tags on its images (the images stay),
9. every parameter under `/aegc/<d>/`: the manifest and the state.

Every deletion requires the name prefix and the ownership tags; anything else is reported as `kept`.
Payload objects aren't deleted by `destroy`; the bucket's lifecycle rule removes them after 15
days. Cost ledger records stay until they expire after 400 days.

**Verification.** After deleting, `destroy` re-lists everything it's responsible for: instances,
volumes and network interfaces tagged with the deployment (waiting up to 10 minutes while any are
still terminating or detaching), groups, launch templates, the security group, queues, the log
group, `/aegc/<d>/` parameters, the worker role and instance profile, the lease's group and role,
and in-use image tags. Anything still there is reported as `remaining`, and the command exits 1.
`--no-verify` skips the check. `aegc destroy e2e --verify`, without `--yes`, only runs the check,
for example to confirm an earlier destroy.

Stop the endpoint before you destroy its deployment. An engine that's still running recreates its
result queue if it finds it missing.

## Find what exists

```sh
aegc footprint e2e
```

`footprint` lists everything that exists for a deployment and changes nothing. It combines the
Resource Groups Tagging API (everything tagged `aegc:deployment=<d>`) with a scan by name of
queues, Auto Scaling groups, launch templates and `/aegc/<d>/` parameters. Each line is `owned`
(aegc's name and tags) or `look-alike` (aegc's name without its tags, which aegc never deletes).
For a while after a destroy, the tagging API can still list terminated instances.

## Upgrade a v0.1 deployment

The state, the cost ledger and the lifetime are worker features, and the spend checks and lease
renewal are engine features, so both images need rebuilding with the new code. From an updated
source tree:

1. `aegc bootstrap`, with the [bootstrap policy](../reference/iam.md#bootstrap), adds the ledger's
   lifecycle rule to the payload bucket.
2. Give the endpoint's credentials the new [operator policy](../reference/iam.md#operator)
   (`aegc iam-policy operator --deployment <d>`), and your own the deployer and bootstrap policies.
3. `aegc image build --env <env> --base-tag <tag>` for each env. The tag includes the aegc wheel's
   hash, so this pushes a new image. `aegc deploy` refuses images built with aegc before 0.2.
4. `aegc deploy <d> --pool <env> ... --max-usd-day N`. This converts the pools to vCPUs at their
   largest size, the bound v0.1 had (`max_vcpus` = v0.1's `max` × the top of its `vcpu=` range),
   creates the state and the lease, and pins the new images. With v0.1's defaults that's
   `max_vcpus=64` for `max=4`; pass a smaller `max_vcpus=` (and `vcpu=`) for a tighter bound.
5. `aegc endpoint ... build`, then `stop` and `start`, so the endpoint runs the new engine.

`aegc deploy --allow-old-image` deploys a v0.1 image anyway. Its workers ignore the state and write
no ledger, so `aegc stop`, the lease and spend limits don't reach them, and their spend isn't
counted. They drain as before when idle. Workers already running a v0.1 image behave the same way
until they leave.

## Removing everything

1. Destroy every deployment in the region: `aegc destroy <d> --yes`.
2. `aegc unbootstrap` (dry run), then `aegc unbootstrap --yes`.

`unbootstrap` refuses while any deployment manifest exists. It empties and deletes the payload
bucket, deletes the creation template, the pull-through cache rule, the cache's `aegc-quay/…`
repositories, the ECR template role, and `/aegc/bootstrap`.

It doesn't delete worker image repositories (`aegc/<env>`) or image records
(`/aegc/images/<env>`). Delete those yourself if you're done with them, for example:

```sh
aws ecr delete-repository --repository-name aegc/geospatial --force
aws ssm delete-parameter --name /aegc/images/geospatial
```

Locally, `aegc endpoint stop` removes the container but leaves the Docker image `aegc-endpoint`
and the volume `aegc-endpoint-home`, which holds your Globus login and endpoint configuration.
