FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git tmux curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY . .
RUN uv sync --frozen --extra vault --no-dev
ENV PATH="/app/.venv/bin:$PATH"
CMD ["omnigent", "host", "--server", "http://host.docker.internal:18780", "--no-open", "--non-interactive"]
