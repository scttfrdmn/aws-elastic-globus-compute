# Operate a deployment

## Check status

```sh
aegc status e2e
```

The output looks like this (values are illustrative):

```
deployment e2e (us-west-2)
  pool geospatial-py314: env geospatial, Python 3.14 | tasks queued 0, running 2, dead-lettered 0 | workers 1 in service, 1 desired, max 3
    last scaling activity: Successful: Launching a new EC2 instance: i-0123456789abcdef0
  result queue aegc-e2e-res-1a2b3c4d5e6f: 0 result(s)
```

- **queued / running** are the task queue's visible and in-flight messages (SQS's approximate
  counts).
- **dead-lettered** counts messages in the pool's dead-letter queue. It should be 0.
- **workers** shows instances in service, desired capacity and the pool's maximum.
- **last scaling activity** is the Auto Scaling group's latest activity, including failures such
  as an exhausted Spot quota.
- **result queue** lines list each user endpoint process's queue. Results that stay there mean that
  endpoint process isn't running.

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

Restart the endpoint (`stop`, then `start`) after you redeploy, reconfigure or rebuild the endpoint
image: each engine reads the deployment manifest only when its process starts.

When the endpoint stops, nothing in AWS stops with it. Queued tasks still run, workers still drain
when idle, and results wait in the result queue for up to 14 days.

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
pool's `max`. It never scales in. Each worker leaves the group after `idle` seconds (default 300)
with nothing to do, decrementing desired capacity, so the pool shrinks as work runs out. A worker
stays up if leaving would take the group below `min`.

**Stall breaker.** If a pool has wanted capacity for 15 minutes with no instance in service, the
engine stops asking for more for 10 minutes and logs an error. Check the group's last scaling
activity in `aegc status`.

**Watchdog.** Every 2 minutes after boot, a timer on each instance checks the worker. If the
worker service is down, or hasn't touched its heartbeat file for 600 s, and the instance has been up
for at least 900 s, the watchdog terminates the instance and decrements desired capacity.

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
5. the worker instance profile and role,
6. the log group,
7. the manifest `/aegc/<d>/manifest`.

Every deletion requires the name prefix and the ownership tags; anything else is reported as `kept`.
Payload objects aren't deleted by `destroy`; the bucket's lifecycle rule removes them after 15
days. For a while after a destroy, the tagging API can still list terminated instances.

Stop the endpoint before you destroy its deployment. An engine that's still running recreates its
result queue if it finds it missing.

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
