# Hugging Face Spaces (Docker SDK) entrypoint. HF builds a Dockerfile at the
# Space repo root and runs it, routing to the `app_port` in the README
# front-matter. This shim just pulls the prebuilt image.
# Pin a release for reproducible builds (H1): --build-arg OMNIGENT_IMAGE=ghcr.io/omnigent-ai/omnigent-server:vX.Y.Z
# (or @sha256:<digest>). Defaults to :latest to match the other deploy paths.
ARG OMNIGENT_IMAGE=ghcr.io/omnigent-ai/omnigent-server:latest
FROM ${OMNIGENT_IMAGE}
