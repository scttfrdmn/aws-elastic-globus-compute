# Security model

## Network

- **Nothing listens.** The endpoint container makes outbound connections only: AMQPS on port 5671 to
  Globus, and HTTPS to AWS APIs. Workers make outbound HTTPS calls to SQS, S3, ECR, CloudWatch Logs
  and Auto Scaling.
- **Workers have no ingress.** The deployment's security group, `aegc-<d>-workers`, has no inbound
  rules. Instances have no SSH key pair. They run in the default VPC's default subnets, which give
  them public IPv4 addresses for outbound traffic.
- **IMDSv2 only**, with a hop limit of 2 so the worker container can reach it.

## Identities

`aegc iam-policy` prints each of aegc's policies; the documents are in the
[IAM reference](../reference/iam.md).

| Identity | Who uses it | Scope |
|---|---|---|
| **worker** role, `aegc-<d>-worker-<region>` | Worker instances, through an instance profile | The deployment's task queues (receive, delete, extend), its result queues (send), `payloads/<d>/tasks/*` (read) and `payloads/<d>/results/*` (write), pulls from `aegc/*` ECR repositories, its log group, terminating instances in groups tagged with the deployment, reading the deployment's state and manifest, and writing `ledger/<d>/*`; plus, with grants, reading or writing under your granted S3 prefixes (`aegc-grants`) |
| **operator** | The endpoint container's AWS credentials | Reading the deployment's parameters, sending tasks, creating and reading its own result queues, payloads, `SetDesiredCapacity` and `UpdateAutoScalingGroup` on groups tagged with the deployment, receiving and deleting on its task queues, writing its state, reading its ledger, prices and Cost Explorer, and renewing its lease |
| **deployer** | Whoever runs `aegc deploy`, `destroy`, `stop`, `resume` | Creates conditioned on the `aegc:managed=true` request tag, changes and deletes on the resource tag where the service supports it; IAM limited to the `/aegc/` path; roles passed only to EC2 and EventBridge Scheduler |
| **bootstrap** | Whoever runs `aegc bootstrap`, `unbootstrap` | The payload bucket, the pull-through cache and its template and role, `/aegc/bootstrap`; roles passed only to ECR |
| **scheduler** role, `aegc-<d>-scheduler-<region>` | EventBridge Scheduler, when the lease or TTL fires | Writing the deployment's state parameter and updating groups tagged with the deployment |

The endpoint container mounts `~/.aws` read-only and uses `AWS_PROFILE` if it's set. For anything
beyond a trial, give it a profile with only the operator policy rather than an administrator
profile. `aegc image build` uses the separate [builder policy](../reference/iam.md#builder).

## Task code runs with the worker role

A task is arbitrary Python from whoever submitted it. It runs in the worker container, which can
read the instance's credentials from IMDS. So any task can do what the worker role allows, for the
whole deployment:

- read other tasks' large payloads under `payloads/<d>/tasks/`,
- receive or delete messages from the deployment's task queues,
- send messages, including forged results, to the deployment's result queues,
- terminate the deployment's worker instances,
- overwrite cost ledger records under `ledger/<d>/`, which makes `aegc cost` wrong and can hide
  spend from the [spend limits](cost.md#spend-limits) or trip them early,
- read everything under the deployment's read grants, and overwrite anything under its write
  grants (see [Data access](#data-access)).

Spend limits guard against runaway scaling and forgotten deployments, not against the code you run.
Keep an AWS Budget on the account as well.

Tasks that share a worker also share its container, its `/scratch` volume and its host network;
each task runs in its own process, not in its own sandbox.

**Treat a deployment as one trust domain.** Anyone who can submit to an endpoint can run code with
that deployment's worker role. Use separate deployments, and separate endpoints, for groups that
shouldn't see each other's work. Globus Compute's own controls decide who may submit to an
endpoint; aegc's endpoint template doesn't add any.

That code can also do what the worker role allows (#32): read and write the deployment's payloads,
post results, write cost ledger records, and set its groups' desired capacity (up to each pool's
`max_vcpus`). Spend limits are enforced from the ledger, so they protect against mistakes, not
against code that's trying to get around them.

The worker only ever invokes Globus Compute's `execute_task` on a message; it never imports a
dotted path named in a message.

## Data access

Without grants, the worker role has no access to S3 beyond the deployment's payload prefix:

- **Open data must be read unsigned.** A signed request from the worker role is denied even for a
  public bucket, because the role's own policy doesn't allow it. Use anonymous access in your code,
  for example `s3fs.S3FileSystem(anon=True)`, `botocore.UNSIGNED` as the signature version in boto3,
  or `AWS_NO_SIGN_REQUEST=YES` for GDAL.
- **Your own buckets, and requester-pays data, need a grant.**
  `aegc deploy <d> --grant read=s3://bucket/prefix` and `--grant write=s3://bucket/prefix` add the
  inline policy [`aegc-grants`](../reference/iam.md#aegc-grants) to the worker role. Read grants
  allow getting and listing objects under the prefix, in any account whose bucket policy allows
  it. Write grants allow putting them, multipart included, but not reading or deleting, and only
  in buckets the deployment's own account owns (`s3:ResourceAccount`), so a mistyped or squatted
  bucket name elsewhere can't receive outputs. See [Read and write your data](../how-to/data.md).
- **Large outputs** go to a write grant with [`aegc.outputs`](../how-to/data.md#return-large-outputs),
  and only their `s3://` URI comes back through Globus Compute.

**A grant extends the deployment's trust domain.** It's granted to the worker role, and every task
in the deployment runs with that role, so every task can use every grant, whoever submitted it.
Anyone who can submit to the deployment's endpoints can read all of a read grant and overwrite
anything under a write grant, including other tasks' outputs. Grant the narrowest prefixes that do
the job, keep data some submitters mustn't see in a separate deployment, and consider S3
Versioning on buckets you grant writes to.

Grants are checked before anything changes: no wildcards or policy variables, valid general
purpose bucket names, not aegc's own bucket, at most 32, and no more than fit in the role's inline
policies.
aegc refers to granted buckets but never creates, changes or deletes them or their policies.

aegc doesn't manage policies you add to the worker role yourself. `aegc deploy` rewrites
`aegc-worker` and `aegc-grants` on every run, and a managed policy attached by hand makes
`aegc destroy` fail when it deletes the role (it removes only inline policies). Use grants instead.

## Data at rest and in transit

- **Payload bucket:** Block Public Access, bucket-owner-enforced object ownership, SSE-S3
  encryption, a bucket policy that denies non-TLS requests, and a lifecycle rule that deletes
  payloads after 15 days.
- **Queues:** SQS-managed server-side encryption. Messages are kept for 14 days.
- **Worker volumes:** encrypted gp3, deleted with the instance.
- **Endpoint state:** the Globus login and endpoint configuration live in the Docker volume
  `aegc-endpoint-home` on your machine.
- `AWSEngine` refuses to run on a high-assurance endpoint: task data leaves Globus's control.

## Images

- Worker images are pinned by digest in the launch template; a tag is never resolved at boot.
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
