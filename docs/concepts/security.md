# Security model

## Network

- **Nothing listens on the network.** The endpoint container makes outbound connections only:
  AMQPS on port 5671 to Globus, and HTTPS to AWS APIs. Workers make outbound HTTPS calls to SQS,
  S3, ECR, CloudWatch Logs, STS and Auto Scaling. In isolated pools the worker agent serves task
  credentials on `127.0.0.1` only, behind a token (see [Task isolation](#task-isolation)).
- **Lambda pools' functions** run without a VPC configuration, in Lambda's own network, and
  make the same outbound calls as workers. Nothing invokes them but their SQS event source
  mapping.
- **Workers have no ingress.** The deployment's security group, `aegc-<d>-workers`, has no inbound
  rules; the [cloud endpoint](#the-cloud-endpoint)'s host uses it too. Instances have no SSH key
  pair. They run in the default VPC's default subnets, which give
  them public IPv4 addresses for outbound traffic.
- **IMDSv2 only**, with a hop limit of 2 so the worker agent in its container can reach it, and the
  IPv6 metadata endpoint off. In isolated pools the host's firewall rejects metadata requests from
  task processes.

## Identities

`aegc iam-policy` prints each of aegc's policies; the documents are in the
[IAM reference](../reference/iam.md).

| Identity | Who uses it | Scope |
|---|---|---|
| **worker** role, `aegc-<d>-worker-<region>` | The worker agent on each instance, through an instance profile | The deployment's task queues (receive, delete, extend), its result queues (send), `payloads/<d>/tasks/*` (read) and `payloads/<d>/results/*` (write), pulls from `aegc/*` ECR repositories, its log group, terminating instances in the deployment's worker groups (tagged `aegc:role=asg`), reading the deployment's state and manifest, writing `ledger/<d>/*`, and assuming the task role; plus `aegc-grants` while pools or instances with images from before aegc 0.4 remain |
| **task** role, `aegc-<d>-task-<region>` | Task code in [isolated](#task-isolation) pools, with credentials the agent hands it | Reading the deployment's manifest (`aegc-task`, for `aegc.outputs`), and with grants, reading or writing under your granted S3 prefixes (`aegc-grants`). Nothing else. Only the worker role may assume it |
| **function** role, `aegc-<d>-<pool>-<region>` | Task code and `aegc-worker lambda` in a [Lambda pool](#lambda-pools); one role per Lambda pool | Receiving, deleting and releasing on its own pool's task queue, sending to the deployment's result queues, `payloads/<d>/tasks/*` (read) and `payloads/<d>/results/*` (write), reading the deployment's state and manifest, its log group, pulling its env's image; plus `aegc-grants`. No cost ledger, Auto Scaling or task role |
| **operator** | The endpoint container's AWS credentials | Reading the deployment's parameters, sending tasks, creating and reading its own result queues, payloads, `SetDesiredCapacity` and `UpdateAutoScalingGroup` on the deployment's worker groups (tagged `aegc:role=asg`), receiving and deleting on its task queues, writing its state, reading its ledger, prices and Cost Explorer, renewing its lease, searching its log group (`aegc provenance`), and enabling and disabling its Lambda pools' event source mappings |
| **deployer** | Whoever runs `aegc deploy`, `destroy`, `stop`, `resume` | Creates conditioned on the `aegc:managed=true` request tag, changes and deletes on the resource tag where the service supports it; IAM limited to the `/aegc/` path; roles passed only to EC2 and EventBridge Scheduler |
| **lambda-deployer** | Whoever runs `aegc deploy`, `destroy`, `stop`, `resume` on a deployment with Lambda pools, with the deployer policy | Functions named `aegc-*` (created with the `aegc:managed=true` request tag, changed and deleted when they carry it), their event source mappings, passing `/aegc/` roles to Lambda only, and the `aegc/*` repositories' policies |
| **endpoint** role, `aegc-<d>-endpoint-<region>` | The [cloud endpoint](#the-cloud-endpoint)'s host, through an instance profile | The operator policy, plus reading its own login secret, pulling `aegc/endpoint`, writing its log streams, `PutMetricData` in `aegc/endpoint`, marking its own instance unhealthy, and deleting the deployment's result queues |
| **endpoint-deployer** | Whoever runs `aegc endpoint deploy`, `undeploy`, `status`, with the deployer policy | Creating, updating and deleting the login secret (never reading it), the offline alarm and its SNS topic, and the host's instance refresh |
| **bootstrap** | Whoever runs `aegc bootstrap`, `unbootstrap` | The payload bucket, the pull-through cache and its template and role, `/aegc/bootstrap`; roles passed only to ECR |
| **scheduler** role, `aegc-<d>-scheduler-<region>` | EventBridge Scheduler, when the lease or TTL fires | Writing the deployment's state parameter, updating groups tagged with the deployment, and updating event source mappings tagged with it that feed its functions |

The endpoint container mounts `~/.aws` read-only and uses `AWS_PROFILE` if it's set. For anything
beyond a trial, give it a profile with only the operator policy rather than an administrator
profile. `aegc image build` uses the separate [builder policy](../reference/iam.md#builder).

## Task isolation

A task is arbitrary Python from whoever submitted it. In EC2 pools whose worker images were built
with aegc 0.4 or later, it runs apart from the worker agent and without the worker role
([Lambda pools](#lambda-pools) aren't isolated):

- **The agent runs as root, with little else.** The worker container runs with `--user 0`,
  `no-new-privileges` and all capabilities dropped except the six it needs to start, own and
  signal task processes (`SETUID`, `SETGID`, `KILL`, `CHOWN`, `DAC_OVERRIDE`, `FOWNER`). Its
  state (the heartbeat file and the credentials token) lives in the root-owned `/run/aegc`.
- **Tasks run as uid 57439.** Each task's process drops to uid 57439, for good, before it
  deserializes the task, so your function never runs with the agent's rights. It can't read the
  agent's environment (`/proc/1/environ`) or memory.
- **No instance metadata.** The user data adds a host firewall rule (iptables) that rejects
  connections from uid 57439 to the metadata service, and the launch template turns its IPv6
  endpoint off. Tasks can't get the instance role's credentials.
- **The task role instead.** The agent assumes the deployment's task role,
  `aegc-<d>-task-<region>`, which only the worker role can assume, and serves its credentials on
  `127.0.0.1` in the container-credentials format. Tasks get
  `AWS_CONTAINER_CREDENTIALS_FULL_URI`, `AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE`, `AWS_REGION` and
  `AWS_DEFAULT_REGION`, so boto3, the AWS CLI and GDAL find credentials and the region by
  themselves and refresh them as needed. Sessions last an hour; the agent renews them with 15
  minutes left, so long tasks keep working.
- **The task role holds only your grants.** It has [`aegc-grants`](../reference/iam.md#aegc-grants)
  and a read of the deployment's manifest (for `aegc.outputs`), nothing more: no queues, payloads,
  cost ledger, deployment state or Auto Scaling.
- **Results can't be forged by a neighbour.** A task's result comes back to the agent over a pipe,
  not a file another task could overwrite. And the engine ignores a result message whose S3
  payload isn't that task's own result key, so a forged message can neither return another
  object nor make the engine delete it.

The v0.4 live run checked this from inside a task: its identity was the task role, the metadata
service refused the connection, SQS, Auto Scaling, the state parameter and the ledger returned
`AccessDenied`, `/proc/1/environ` was unreadable, and reading under a read grant and
`aegc.outputs.put` worked. See [Performance](../reference/performance.md#v04-live-run-2026-11-16).

### What tasks still share

Isolation separates tasks from the worker role, not from each other:

- **Tasks on one worker share uid 57439.** They can see each other's processes and signal or kill
  them, and read and write each other's run directories under `/scratch/tasks/` and anything else
  in `/scratch`. Each task still runs in its own process, not in its own sandbox.
- **Tasks in a deployment share the task role.** Every task can use every grant, whoever submitted
  it (see [Data access](#data-access)).
- **They share the worker's host network and memory.** There's no per-task memory limit.

Separate tasks per uid, per-task session policies and a separate task container aren't
implemented yet ([#32](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/32)).

**Treat a deployment as one trust domain.** Anyone who can submit to an endpoint can run code with
that deployment's task role, and with its grants. Use separate deployments, and separate
endpoints, for groups that shouldn't see each other's work. Globus Compute's own controls decide
who may submit to an endpoint, and
[`aegc endpoint configure --allow-function`](../how-to/flows.md#allow-only-your-functions) limits
which registered functions it runs.

Spend limits guard against runaway scaling and forgotten deployments, not against the code you run:
anyone who can submit can keep a pool busy up to its `max_vcpus`, or a Lambda pool up to its
concurrency. Keep an AWS Budget on the
account as well.

The worker only ever invokes Globus Compute's `execute_task` on a message; it never imports a
dotted path named in a message.

### Pools with older images

Workers from images built before aegc 0.4 run tasks in the worker container with the worker role,
as before. Their tasks can do what the worker role allows, for the whole deployment:

- read other tasks' large payloads under `payloads/<d>/tasks/`,
- receive or delete messages from the deployment's task queues,
- send messages, including forged results, to the deployment's result queues,
- terminate the deployment's worker instances, and set its groups' desired capacity (up to each
  pool's `max_vcpus`),
- overwrite cost ledger records under `ledger/<d>/`, which makes `aegc cost` wrong and can hide
  spend from the [spend limits](cost.md#spend-limits) or trip them early,
- read everything under the deployment's read grants, and overwrite anything under its write
  grants.

`aegc deploy` warns about such pools
(`warning: <pools>: images built before aegc 0.4 run tasks with the worker role's credentials
...`); rebuild their images with `aegc image build` to isolate them. While any pool, or any
running worker launched before its pool was isolated, still runs tasks with the worker role,
`deploy` keeps `aegc-grants` on the worker role too. Workers of isolated pools carry the instance
tag `aegc:isolated=true`; once no untagged workers remain, the next deploy deletes the worker
role's copy of the grants. See [Upgrade from v0.3](../how-to/operations.md#upgrade-from-v03).

## Lambda pools

[Lambda pools](../how-to/lambda-pools.md) run tasks without any of the isolation above. Lambda
gives the function role's credentials to the process in environment variables and has no uid to
drop to, so task code runs with the pool's **function role**, `aegc-<d>-<pool>-<region>`. aegc
keeps that role narrow: its own pool's task queue, no cost ledger (Lambda spend comes from
Lambda's own metrics), no Auto Scaling, no task role. But payloads and results aren't kept per
pool yet, so a task on a Lambda pool can:

- read every task payload of the deployment under `payloads/<d>/tasks/`, including those of
  isolated EC2 pools;
- write result payloads under `payloads/<d>/results/` and send to every result queue of the
  deployment, so it could forge a result for another pool's task;
- use every grant of the deployment, as any task can;
- read the deployment's state and manifest.

Per-pool payload prefixes, a function role scoped to its pool's, and results checked against
their task's pool are tracked in
[#80](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/80). Until then, **keep
untrusted functions on isolated EC2 pools in a deployment without Lambda pools.** Every
`aegc deploy` with a Lambda pool prints a note saying so, and a pool can't be both a Lambda pool
and isolated.

Execution environments are reused between invocations, one task at a time: `/tmp`, where the
task scratch (`/tmp/aegc`) and `HOME` live, persists from one task to the next in the same
environment.

## Data access

Without grants, task code has no S3 access at all in isolated pools, and the worker role reaches
only the deployment's payload prefix and ledger:

- **Open data must be read unsigned.** A signed request from the task role (or the worker role) is
  denied even for a public bucket, because the role's own policy doesn't allow it. Use anonymous
  access in your code, for example `s3fs.S3FileSystem(anon=True)`, `botocore.UNSIGNED` as the
  signature version in boto3, or `AWS_NO_SIGN_REQUEST=YES` for GDAL.
- **Your own buckets, and requester-pays data, need a grant.**
  `aegc deploy <d> --grant read=s3://bucket/prefix` and `--grant write=s3://bucket/prefix` add the
  inline policy [`aegc-grants`](../reference/iam.md#aegc-grants) to the task role (and to the
  worker role while pools with older images remain). Read grants allow getting and listing
  objects under the prefix, in any account whose bucket policy allows it. Write grants allow
  putting them, multipart included, but not reading or deleting, and only in buckets the
  deployment's own account owns (`s3:ResourceAccount`), so a mistyped or squatted bucket name
  elsewhere can't receive outputs. See [Read and write your data](../how-to/data.md).
- **Large outputs** go to a write grant with [`aegc.outputs`](../how-to/data.md#return-large-outputs),
  and only their `s3://` URI comes back through Globus Compute.

**A grant extends the deployment's trust domain.** Every task in the deployment runs with the same
task role, so every task can use every grant, whoever submitted it. Anyone who can submit to the
deployment's endpoints can read all of a read grant and overwrite anything under a write grant,
including other tasks' outputs. Grant the narrowest prefixes that do the job, keep data some
submitters mustn't see in a separate deployment, and consider S3 Versioning on buckets you grant
writes to.

Grants are checked before anything changes: no wildcards or policy variables, valid general
purpose bucket names, not aegc's own bucket, at most 32, and no more than fit in either role's
inline policies.
aegc refers to granted buckets but never creates, changes or deletes them or their policies.

aegc doesn't manage policies you add to the worker or task role yourself. `aegc deploy` puts
`aegc-worker`, `aegc-task` and `aegc-grants` back as aegc would write them whenever they differ,
and a managed policy attached by hand makes `aegc destroy` fail when it deletes the role (it
removes only inline policies). Use grants instead.

## Data at rest and in transit

- **Payload bucket:** Block Public Access, bucket-owner-enforced object ownership, SSE-S3
  encryption, a bucket policy that denies non-TLS requests, and a lifecycle rule that deletes
  payloads after 15 days.
- **Queues:** SQS-managed server-side encryption. Messages are kept for 14 days.
- **Worker volumes:** encrypted gp3, deleted with the instance.
- **Endpoint state:** the Globus login and endpoint configuration live in the Docker volume
  `aegc-endpoint-home` on your machine. A cloud endpoint's copy is in Secrets Manager (encrypted
  with the account's default key for Secrets Manager) and on the host's encrypted volume.
- `AWSEngine` refuses to run on a high-assurance endpoint: task data leaves Globus's control.

## Images

- Worker images are pinned by digest in the launch template, and in a Lambda pool's function; a
  tag is never resolved at boot.
- For Lambda pools, `deploy` adds the statement `AegcLambdaPull` to the `aegc/<env>` repository
  policy: the Lambda service may pull, for functions named `aegc-*` in the account and region
  only (`aws:sourceArn`). Other statements in the policy are left alone. Without it,
  `CreateFunction` would add an unconditioned statement of its own.
- ECR repositories `aegc/<env>` have immutable tags and scan on push.
- aarchsci and aarchbio bases come from quay.io through the `aegc-quay` pull-through cache. Other
  bases (`aegc image build --base`) and registered images (`aegc image register`) are pulled from
  their own registries with your Docker login, then copied into `aegc/<env>`; workers pull only
  from ECR. aegc pins the reference you give, but doesn't verify upstream image signatures itself.
  You're responsible for what's in an image you bring.
- The conda build fails if installing `globus-compute-endpoint` changes any package already in the
  base image, except `click`. The pip build installs with the base's packages as constraints, so
  it fails rather than change them.
- `aegc image register` runs the image the way workers do, with its own entrypoint and user, and
  checks that it's arm64 and pinned, has aegc 0.3 or later, runs as root or uid 57439, and passes
  `aegc-worker selftest`. It doesn't inspect what else the image contains or does.
- Built and registered images are identified by image id, not tag, so a tag moved upstream gives a
  new image rather than silently reusing an old one.

## Ownership: delete only what aegc created

Every resource is tagged on its create call with `aegc:managed=true`, `aegc:deployment`,
`aegc:role` and `aegc:version` (and `aegc:pool` for pool resources). aegc deletes a resource only if
its name has aegc's prefix for that deployment **and** its tags say aegc created it for that
deployment. A resource with an aegc-style name but without the tags is refused or left alone, and
reported as `kept`. Region-wide resources carry `aegc:deployment=_bootstrap`.

`destroy`, `unbootstrap` and `reap` are dry runs unless you pass `--yes`, and `stop --hard` needs
`--yes`. `bootstrap` and `deploy` accept `--dry-run`. `destroy` checks afterwards that nothing it
owns remains, and `aegc footprint` lists what exists, including look-alikes aegc won't touch. See [Resources and tags](../reference/resources.md).

## The cloud endpoint

[`aegc endpoint deploy`](../how-to/cloud-endpoint.md) runs your endpoint on an EC2 instance in
your account, as you: it copies your Globus login (the token storage `globus-compute-endpoint
login` wrote) into the Secrets Manager secret `aegc-<d>-endpoint`, and the host restores it at
boot. That makes the login the thing to protect.

- **Only the host reads it.** The secret's resource policy denies `secretsmanager:GetSecretValue`
  to every principal but the endpoint role, whatever their IAM policies say. The
  [endpoint-deployer](../reference/iam.md#endpoint-deployer) policy has no `GetSecretValue`;
  `status` reads the endpoint's name and id from tags. An administrator who may change the
  secret's resource policy could lift the deny, so treat `secretsmanager:PutResourcePolicy` on
  `aegc-*` secrets as access to your Globus login.
- **The host has no way in.** The deployment's security group (no inbound rules), no SSH key,
  IMDSv2 only, an encrypted volume, the login under a mode-700 directory. Its role is the operator
  policy plus a few narrow additions ([endpoint](../reference/iam.md#endpoint)). Task code never
  runs there: it runs on workers, whose roles can't read the secret or change the endpoint's
  group.
- **Session Manager is opt-in** (`--session-manager`). With it, anyone allowed `ssm:StartSession`
  on the instance gets root there, and with it the login and the role's credentials.
- **What the tokens can do.** Whoever holds them can act as you in Globus Compute, including
  submitting tasks to any endpoint you may use, not just this one, until they're revoked.
  `globus-compute-endpoint logout` (run in the `aegc-endpoint` image with your volume; see
  [Security](../how-to/cloud-endpoint.md#security)) revokes them with Globus, which also stops the
  cloud endpoint until you log in and deploy again.
- **One endpoint id, one place.** `deploy` refuses while the local endpoint runs (unless
  `--stop-local`) and refuses an id that's another deployment's cloud endpoint, and the host's
  group never runs two instances at once.
- **The offline alarm can't be silenced by tasks.** The liveness metric is published with
  PutMetricData (workers have none) and carries a token dimension that only the host knows, so a
  task printing CloudWatch EMF into the worker logs can't publish the series the alarm watches.
