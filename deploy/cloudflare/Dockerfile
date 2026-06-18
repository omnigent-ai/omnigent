# Omnigent server on Cloudflare Containers, backed by D1 (database) and R2
# (artifact store via omnigent's native S3 backend). Derived from the official
# server image; adds the Cloudflare D1 SQLAlchemy dialect + a behavior shim, and
# boto3 for the S3/R2 artifact store.
#
# No FUSE mount: the server writes artifacts straight to R2 over the S3 API,
# selected with ``OMNIGENT_ARTIFACT_URI=s3://<bucket>`` (set by the Worker).
#
# Requires an omnigent-server image that includes the S3 artifact backend —
# i.e. the ``deploy/docker/entrypoint.py`` change that ships alongside this
# directory. (Until that lands in the published image, build a server image
# from this branch.)
FROM ghcr.io/omnigent-ai/omnigent-server:latest

ARG SITE=/opt/venv/lib/python3.12/site-packages
USER root

# Cloudflare D1 SQLAlchemy dialect + boto3 (for the S3/R2 artifact store).
RUN /opt/venv/bin/pip install --no-cache-dir \
        "sqlalchemy-cloudflare-d1==0.3.10" "boto3>=1.30,<2"

# Shim: make the D1 dialect behave like SQLite (auto-loaded at startup).
COPY sitecustomize.py ${SITE}/sitecustomize.py
RUN /opt/venv/bin/python -c "\
import sitecustomize; \
from sqlalchemy import create_engine; \
e = create_engine('cloudflare_d1://a:b@c'); \
from alembic.ddl.impl import _impls; \
assert 'cloudflare_d1' in _impls; \
assert e.dialect.ddl_compiler.__name__ == 'SQLiteDDLCompiler'; \
print('D1 dialect + shim OK')"
