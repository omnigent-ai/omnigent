# MicroVM image for the AWS Lambda MicroVMs sandbox provider.
#
# Layers a lifecycle-hooks shim on the official Omnigent host image.
# `create-microvm-image` compiles this Dockerfile into a MicroVM image; the
# launcher's `image_identifier` names the result. Lambda MicroVMs require arm64,
# and the host image publishes a multi-arch manifest — pin `--platform=linux/arm64`
# so a build on an amd64 host (or a multi-arch builder that would otherwise
# default to the builder's arch) still produces an arm64 image that boots.
#
# RunMicrovm carries no per-launch environment, so the host identity + token do
# NOT arrive as container env. The launcher passes them in `runHookPayload`, and
# the platform delivers that string as the body of the /run lifecycle hook after
# the VM thaws from its build-time snapshot. The hooks server reads /run and
# spawns `omnigent host` (start_host.sh), which dials OUT to OMNIGENT_SERVER over
# the managed launch-token tunnel — the real work rides that tunnel.
FROM --platform=linux/arm64 ghcr.io/omnigent-ai/omnigent-host:latest

# The hooks server binds 0.0.0.0 on the hooks port and answers the platform's
# lifecycle probes: /ready gates the build-time snapshot, and /run delivers the
# per-launch identity payload that starts the host. Stdlib only, so it runs
# under the image's bare python3.
COPY hooks_server.py /opt/omnigent/hooks_server.py

# entrypoint.sh runs the hooks server as the container's main process.
COPY entrypoint.sh /opt/omnigent/entrypoint.sh

# start_host.sh is spawned by the hooks server on /run with the identity payload
# exported into its environment; it clones OMNIGENT_REPO_URL when set, then execs
# `omnigent host`.
COPY start_host.sh /opt/omnigent/start_host.sh

# The build role has no shell-script chmod guarantee across bases; make them
# executable at build time.
RUN chmod +x /opt/omnigent/entrypoint.sh /opt/omnigent/start_host.sh /opt/omnigent/hooks_server.py

# Hooks bind here; override with the --hooks config at create-microvm-image time.
ENV OMNIGENT_MICROVM_HOOKS_PORT=9000

CMD ["/opt/omnigent/entrypoint.sh"]
