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

aegc uses three IAM identities. `aegc iam-policy` prints each policy; the documents are in the
[IAM reference](../reference/iam.md).

| Identity | Who uses it | Scope |
|---|---|---|
| **worker** role, `aegc-<d>-worker-<region>` | Worker instances, through an instance profile | The deployment's task queues (receive, delete, extend), its result queues (send), `payloads/<d>/tasks/*` (read) and `payloads/<d>/results/*` (write), pulls from `aegc/*` ECR repositories, its log group, and terminating instances in groups tagged with the deployment |
| **operator** | The endpoint container's AWS credentials | Reading the deployment's manifest, sending tasks, creating and reading its own result queues, payloads, and `SetDesiredCapacity` on groups tagged with the deployment |
| **deployer** | Whoever runs `aegc bootstrap`, `deploy`, `destroy` | Creates conditioned on the `aegc:managed=true` request tag, changes and deletes on the resource tag where the service supports it; IAM limited to the `/aegc/` path; roles passed only to EC2 and ECR |

The endpoint container mounts `~/.aws` read-only and uses `AWS_PROFILE` if it's set. For anything
beyond a trial, give it a profile with only the operator policy rather than an administrator
profile. `aegc image build` needs ECR push rights that the deployer policy doesn't include.

## Task code runs with the worker role

A task is arbitrary Python from whoever submitted it. It runs in the worker container, which can
read the instance's credentials from IMDS. So any task can do what the worker role allows, for the
whole deployment:

- read other tasks' large payloads under `payloads/<d>/tasks/`,
- receive or delete messages from the deployment's task queues,
- send messages, including forged results, to the deployment's result queues,
- terminate the deployment's worker instances.

Tasks that share a worker also share its container, its `/scratch` volume and its host network;
each task runs in its own process, not in its own sandbox.

**Treat a deployment as one trust domain.** Anyone who can submit to an endpoint can run code with
that deployment's worker role. Use separate deployments, and separate endpoints, for groups that
shouldn't see each other's work. Globus Compute's own controls decide who may submit to an
endpoint; aegc's endpoint template doesn't add any.

The worker only ever invokes Globus Compute's `execute_task` on a message; it never imports a
dotted path named in a message.

## Data access

The worker role has no access to S3 beyond the deployment's payload prefix. As a result:

- **Open data must be read unsigned.** A signed request from the worker role is denied even for a
  public bucket, because the role's own policy doesn't allow it. Use anonymous access in your code,
  for example `s3fs.S3FileSystem(anon=True)`, `botocore.UNSIGNED` as the signature version in boto3,
  or `AWS_NO_SIGN_REQUEST=YES` for GDAL.
- **Requester-pays data, private buckets and output buckets aren't reachable.** There is no
  setting in v0.1 to grant the worker role more access. aegc doesn't manage policies you add to the
  role yourself, and a managed policy attached by hand makes `aegc destroy` fail when it deletes
  the role (it removes only inline policies).
- **Outputs come back as results**, within Globus Compute's size limit.

This is a limitation, not a recommendation. Data-access grants are listed as gap G1 in
[#16](https://github.com/scttfrdmn/aws-elastic-globus-compute/issues/16).

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
- Bases come from quay.io through the `aegc-quay` pull-through cache. aegc pins the base tag you
  name, but doesn't verify the upstream image signatures itself.
- The build fails if installing `globus-compute-endpoint` changes any package already in the base
  image, except `click`.

## Ownership: delete only what aegc created

Every resource is tagged on its create call with `aegc:managed=true`, `aegc:deployment`,
`aegc:role` and `aegc:version` (and `aegc:pool` for pool resources). aegc deletes a resource only if
its name has aegc's prefix for that deployment **and** its tags say aegc created it for that
deployment. A resource with an aegc-style name but without the tags is refused or left alone, and
reported as `kept`. Region-wide resources carry `aegc:deployment=_bootstrap`.

`destroy`, `unbootstrap` and `reap` are dry runs unless you pass `--yes`. `bootstrap` and `deploy`
accept `--dry-run`. See [Resources and tags](../reference/resources.md).
