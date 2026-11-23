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
  grants: read s3://my-lab-data/inputs/, write s3://my-lab-results/runs/
  lease until 2026-11-16 14:05 UTC (renewed by endpoints)
  pool geospatial-py314: env geospatial, Python 3.14 | tasks queued 3, running 4, dead-lettered 0 | workers 2 in service, 4 vCPUs desired, max 8
    size per-vCPU: 1 vCPU(s), 2 GiB per task | 2x c8g.large spot | $0.0866/h | oldest task waiting 2 min | running tasks could cost up to $0.95 more
    last scaling activity: Successful: Launching a new EC2 instance: i-0123456789abcdef0
  spot vCPU quota 32, in use 4; pools can use up to 8
  result queue aegc-e2e-res-1a2b3c4d5e6f: 0 result(s)
```

- **The first line** gives the [deployment state](../concepts/architecture.md#deployment-state).
  When it isn't `running`, the next line says why and how to resume.
- **spend** is the estimated rate of the workers running now, and the estimated spend today and this
  month from the [cost ledger](../concepts/cost.md#cost-ledger), followed by the limits.
- **grants** lists the deployment's [data-access grants](data.md), or says
  `grants: none (workers reach only aegc's own bucket)`.
- **lease** and **TTL** show when the deployment will expire unless an endpoint renews it.
- **queued / running** are the task queue's visible and in-flight messages (SQS's approximate
  counts).
- **dead-lettered** counts messages in the pool's dead-letter queue. It should be 0.
- **workers** shows instances in service, desired capacity and the pool's maximum, in vCPUs (in
  workers for a pool deployed by v0.1).
- **The second pool line** starts with the pool's [size class](configuration.md#size-classes) and
  what one task gets (`size per-vCPU` for a pool without `size=`; for `whole`, `one task per
  instance, at least N vCPUs`). Then it lists the workers by instance type and Spot or on-demand, their
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

`--actual` needs aegc's cost allocation tags activated once per account; see
[Cost Explorer and cost allocation tags](../concepts/cost.md#cost-explorer-and-cost-allocation-tags).
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

To keep an endpoint up with your laptop closed, run it in the cloud:
`aegc endpoint ... deploy --stop-local` moves it to a small EC2 instance with the same endpoint id,
which also checks the spend limits every 5 minutes, task or no task. See
[Run the endpoint in the cloud](cloud-endpoint.md).

## Read logs

- **Endpoint:** `ep logs` follows the container's output. Engine messages
  (`aegc.engine`) go to the user endpoint process's log under `/home/gc/.globus_compute/` in the
  container. To find them:

    ```sh
    docker exec aegc-endpoint-aegc-e2e find /home/gc/.globus_compute -name '*.log'
    ```

- **Workers:** CloudWatch Logs group `/aegc/<d>`, one stream per instance, `<pool>/<instance-id>`,
  kept for 14 days. Workers from aegc 0.4 images log one JSON object per line, with a
  `task finished` line per task; the tasks' own output is in the same stream. See
  [Watch tasks](observability.md#worker-logs):

    ```sh
    aws logs tail /aegc/e2e --follow
    ```

- **Cloud endpoint:** streams `endpoint/<instance-id>` (the endpoint, with user endpoint
  process lines prefixed `[uep]`) and `endpoint/<instance-id>/evaluate` in the same log group. See
  [Logs](cloud-endpoint.md#logs).

- **One task:** `aegc provenance e2e <task id>` finds where and how it ran; `aegc metrics e2e`
  sums up the pools. See [Watch tasks](observability.md).

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
   10 minutes for each to go), the [cloud endpoint](cloud-endpoint.md)'s included,
2. the launch templates,
3. the security group (retrying while the terminated instances' network interfaces go away),
4. all of the deployment's queues: task, dead-letter and result queues,
5. the lease's schedules, schedule group and scheduler role,
6. the worker instance profile and role, and the task role,
7. the cloud endpoint's alarm, SNS topic, image `inuse-` tag, instance profile and role, and its
   login secret, if it has one,
8. the log group,
9. the deployment's `inuse-` tags on its images (the images stay),
10. every parameter under `/aegc/<d>/`: the manifest and the state.

Every deletion requires the name prefix and the ownership tags; anything else is reported as `kept`.
Payload objects aren't deleted by `destroy`; the bucket's lifecycle rule removes them after 15
days. Cost ledger records stay until they expire after 400 days.

**Verification.** After deleting, `destroy` re-lists everything it's responsible for: instances,
volumes and network interfaces tagged with the deployment (waiting up to 10 minutes while any are
still terminating or detaching), groups, launch templates, the security group, queues, the log
group, `/aegc/<d>/` parameters, the worker role and instance profile, the task role, the lease's
group and role, the cloud endpoint's role, secret, alarm and topic,
and in-use image tags. Anything still there is reported as `remaining`, and the command exits 1.
`--no-verify` skips the check. `aegc destroy e2e --verify`, without `--yes`, only runs the check,
for example to confirm an earlier destroy.

Stop the endpoint before you destroy its deployment. An engine that's still running recreates its
result queue if it finds it missing. A cloud endpoint goes with the deployment; you don't need to
undeploy it first.

**Without the endpoint-deployer policy.** The cloud endpoint's secret, alarm and topic need the
[endpoint-deployer](../reference/iam.md#endpoint-deployer) policy. With the deployer policy alone,
`destroy` still removes everything else and reports

```
        kept  cloud endpoint             e2e (not checked: needs the endpoint-deployer policy (aegc iam-policy endpoint-deployer))
```

For a deployment that never had a cloud endpoint, that's all, and verification passes. If it did
have one, its host goes with the groups, but its secret, alarm and topic stay, and so do its role,
instance profile and the endpoint image's `inuse-` tag, which verification reports as
`remaining`. Run `aegc destroy <d> --yes`
again with both policies to finish.

## Find what exists

```sh
aegc footprint e2e
```

`footprint` lists everything that exists for a deployment and changes nothing. It combines the
Resource Groups Tagging API (everything tagged `aegc:deployment=<d>`) with a scan by name of
queues, Auto Scaling groups, launch templates and `/aegc/<d>/` parameters. Each line is `owned`
(aegc's name and tags) or `look-alike` (aegc's name without its tags, which aegc never deletes).
For a while after a destroy, the tagging API can still list terminated instances.

## Other regions

Every aegc resource is regional, so a second region (us-east-1, say, next to RODA datasets such as
1000 Genomes, OpenNeuro or the NCBI BLAST databases) is a second, independent setup. Pass
`--region` to each command, or set `AWS_DEFAULT_REGION`:

1. **Bootstrap it.** `aegc --region us-east-1 bootstrap` creates that region's payload bucket
   `aegc-<account>-us-east-1`, its pull-through cache rule, the ECR template role
   `aegc-ecr-template-us-east-1`, and the region's `/aegc/bootstrap` parameter. In us-east-1 the
   bucket is created without a location and tagged straight after (us-east-1 takes no
   `LocationConstraint`); if the tagging fails, the empty bucket is deleted again, so a retry
   doesn't find an untagged bucket it would refuse.
2. **Build the images there.** Images live in the region's ECR and their records
   (`/aegc/images/<env>`) in the region's SSM, so an image built in us-west-2 isn't visible in
   us-east-1. Run `aegc --region us-east-1 image build --env <env> ...` again, or
   `image register` a copy you pushed to that region's ECR.
3. **Deploy.** `aegc --region us-east-1 deploy <d> --pool <env> ...`. The manifest, state and
   lease are the region's, and the IAM roles carry the region in their names, so the same
   deployment name can exist in two regions without clashing.
4. **Run an endpoint per region.** `aegc --region us-east-1 endpoint ... configure` writes the
   region into the endpoint's config. One endpoint serves one deployment in one region, and a
   [cloud endpoint](cloud-endpoint.md) runs in its deployment's region.

**Zones without Graviton.** Not every Availability Zone offers the pool's instance families
(us-east-1's use1-az3 has no Graviton at all). `deploy` asks EC2 which zones offer at least one of
the pool's `allowed_instance_types` and leaves default subnets in other zones out of that pool's
Auto Scaling group:

```
     skipped  subnet                     subnet-0abc (us-east-1e, use1-az3) (pool geospatial-py314: the zone offers none of c8g.*, m8g.*, ...)
```

Zone names map to zone ids differently in each account; the id (`use1-az3`) is the same
everywhere. The live run in us-east-1 skipped one subnet this way, in `us-east-1e`. The cloud
endpoint's host is filtered the same way, by its instance type.

Subnets you pass explicitly are all used; `deploy` reports one in such a zone as `kept`, as a
warning. If EC2 won't say (a deployer policy from before 0.6 lacks
`ec2:DescribeInstanceTypeOfferings`), every subnet is kept and the report says so.

Cost estimates use the region's Spot and on-demand prices. The static fallback table holds
us-west-2's on-demand prices, which are also us-east-1's for these types.

## Upgrade from v0.3

v0.4's [task isolation](../concepts/security.md#task-isolation), JSON logs, metrics and provenance
are worker features, and the `python` option is an endpoint feature, so rebuild both images.
From an updated source tree:

1. Update the IAM policies you attached yourself: the
   [operator policy](../reference/iam.md#operator) gains `logs:FilterLogEvents` on the
   deployment's log group (for `aegc provenance`), and the
   [deployer policy](../reference/iam.md#deployer) `autoscaling:EnableMetricsCollection` (for
   `aegc metrics`' capacity figures). `aegc iam-policy operator --deployment <d>` and
   `aegc iam-policy deployer` print them.
2. `aegc image build --env <env> ...` for each env and Python minor, as before. Images built with
   aegc 0.4 isolate tasks; `aegc image list --env <env>` shows each image's aegc version.
3. `aegc deploy <d> --pool <env> ...`. This creates the task role, gives the worker role
   `AssumeTaskRole`, pins the new images, turns on the groups' metrics, and adds a launch
   template version to every pool, because the user data changed (the log format and
   `AEGC_IMAGE`) even for pools you didn't rebuild. Pools still on older images get a warning.
4. `aegc endpoint ... build`, then `configure` again (the schema and template changed: `python`),
   then `stop` and `start`.

Running workers keep their old launch template version until they leave. While any of them is
still running, `deploy` keeps your grants on the worker role as well, updated to match any
change (`kept  iam role policy  aegc-grants (worker role) (N pre-0.4 instance(s) still running; deploy
again after they leave)`). Once they've gone (idle workers leave after 5 minutes; `aegc status`
shows what's running), run `aegc deploy <d> --pool <env>` again: the grants then live only on the
task role.

Code in tasks may notice the change: tasks no longer reach instance metadata, and get
`AccessDenied` for anything outside their grants. See
[Isolated tasks](troubleshooting.md#isolated-tasks).

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
