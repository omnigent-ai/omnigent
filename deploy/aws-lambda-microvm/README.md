# Omnigent on AWS Lambda MicroVMs

[AWS Lambda MicroVMs](https://aws.amazon.com/about-aws/whats-new/2026/06/aws-lambda-microvms/)
run Omnigent hosts inside Firecracker-isolated, snapshot-resumable microVMs
(up to 8 hours) in your own AWS account. This is a **server-managed** provider:
the server provisions a microVM when a session is created with
`"host_type": "managed"` and terminates it when the session is deleted. There is
no `omnigent sandbox` CLI flow for this provider.

> [!IMPORTANT]
> **Lambda MicroVMs boot from a pre-built *MicroVM image*, not a registry
> reference.** Unlike the Modal / Daytona / CoreWeave launchers — which pull
> `ghcr.io/omnigent-ai/omnigent-host` directly — Lambda MicroVMs start from an
> image built ahead of time with `create-microvm-image` (a Dockerfile + zip in
> S3, closer to the E2B template model). You build that image once; the
> launcher's `image_identifier` config then names it. This directory is **not**
> a server deploy target — it holds the image-build inputs.

## Why this provider

- **AWS-native isolation.** Sessions stay in your account, VPC, IAM, and
  CloudWatch, with per-tenant Firecracker isolation.
- **Sleep and wake.** A microVM idles to a snapshot and resumes in place with
  the workspace intact. It is the first provider whose resume *preserves the
  running host* (`resume_preserves_host = True`) — the thaw brings the host back
  with its live token, so the wake reconnects it without a fresh start — letting
  a session that sits between turns stop billing compute and wake on the next
  message.

## Prerequisites

```bash
pip install 'omnigent[lambda-microvm]'   # installs the boto3 extra
```

The **server process** needs AWS credentials (profile, environment, or an
instance role) that can call the `lambda-microvms` API and pass the execution
role. Lambda MicroVMs is available in `us-east-1`, `us-east-2`, `us-west-2`,
`ap-northeast-1` (Tokyo), and `eu-west-1` (Ireland); pin a region your server
can reach.

## IAM: two roles

Lambda MicroVMs uses two roles (see the AWS docs for exact trust policies):

- **Build role** (`buildRoleArn`) — assumed while `create-microvm-image` builds
  the image. Needs `s3:GetObject` on your artifact bucket and CloudWatch Logs
  write. Trusts `lambda.amazonaws.com` with an `aws:SourceAccount` condition
  (confused-deputy prevention).
- **Execution role** (`executionRoleArn`) — assumed by the running microVM.
  Needs CloudWatch Logs write. Add `bedrock:InvokeModel` here if you want
  runners to reach Bedrock through the role instead of a long-lived key in the
  microVM environment (see "Credentials" below).

## Build the MicroVM image

The microVM runs the official Omnigent host image plus a tiny lifecycle-hooks
shim. `Dockerfile` in this directory layers that shim on
`ghcr.io/omnigent-ai/omnigent-host:latest` (the host image already publishes for
arm64, which Lambda MicroVMs require, so no cross-build is needed on a Graviton
builder; on an x86 host, register QEMU first).

```bash
# 1. Package the build context (Dockerfile at the zip root, alongside the files
#    it COPYs) and upload to S3 in the same region as the image.
zip -j omnigent-host-microvm.zip \
  deploy/aws-lambda-microvm/Dockerfile \
  deploy/aws-lambda-microvm/hooks_server.py \
  deploy/aws-lambda-microvm/entrypoint.sh \
  deploy/aws-lambda-microvm/start_host.sh
aws s3 cp omnigent-host-microvm.zip s3://my-omnigent-artifacts/omnigent-host-microvm.zip

# 2. Build the MicroVM image.
aws lambda-microvms create-microvm-image \
  --name omnigent-host \
  --base-image-arn arn:aws:lambda:<region>:aws:microvm-image:al2023-1 \
  --build-role-arn arn:aws:iam::<acct>:role/omnigent-microvm-build \
  --code-artifact '{"uri":"s3://my-omnigent-artifacts/omnigent-host-microvm.zip"}'
```

The launcher's `image_identifier` names the resulting image (`omnigent-host` or
its ARN). Image versions incur storage cost even when idle; prune old ones with
`delete-microvm-image-version`.

## Server config

```yaml
sandbox:
  provider: lambda_microvm
  server_url: https://omnigent.example.com   # the public URL the host dials back to
  lambda_microvm:
    region: us-east-1
    image_identifier: omnigent-host           # from create-microvm-image above
    image_version: "1.0"                      # optional; default: latest
    execution_role_arn: arn:aws:iam::<acct>:role/omnigent-microvm-exec
    env: [ANTHROPIC_API_KEY, GIT_TOKEN]       # SERVER env var NAMES → microVM env
```

`provider` + `server_url` + `image_identifier` + `execution_role_arn` is a
complete config. Everything under `lambda_microvm:` also has an env-var fallback
(`OMNIGENT_LAMBDA_MICROVM_*`) for deployments that prefer environment config.

## Credentials

Harness credentials (model keys, `GIT_TOKEN`) reach the microVM the same way
Daytona and BoxLite inject them: the server reads the **named** environment
variables from its own process at launch and passes their values into the
microVM environment. Secret *values* never live in the config file — only the
names do, and a name that is unset in the server environment fails the launch
loud rather than starting a host that can't authenticate.

For a key-free setup, grant the execution role `bedrock:InvokeModel` and point
runners at Bedrock; the microVM then reaches the model through its role and no
model key enters the sandbox.

## Lifecycle and caveats

- **8-hour cap.** A microVM lives at most 8 hours. The managed launch-token TTL
  is derived above that cap (override the requested lifetime with
  `OMNIGENT_LAMBDA_MICROVM_MAX_LIFETIME_S`).
- **Idle suspend/resume.** The launcher sets an idle policy
  (`maxIdleDurationSeconds` 900, `autoResumeEnabled` true), so an idle microVM
  suspends to a snapshot and auto-resumes on the next request. Idle is measured
  by **inbound** traffic to the microVM's proxy endpoint; because the Omnigent
  host holds an **outbound** tunnel, a between-turns session suspends and the
  managed wake path resumes it when the next message arrives.
- **Snapshot uniqueness.** A resumed microVM restores memory state, so reseed
  CSPRNGs and rotate any in-memory secrets on resume (the host does this on
  restart). See the AWS "snapshots and uniqueness" docs.
- **No self-suspend.** A microVM cannot suspend itself; suspension is driven by
  the idle policy or an external `suspend-microvm` call.
