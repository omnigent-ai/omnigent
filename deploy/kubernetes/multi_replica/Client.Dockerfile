# Test fixture: a user machine with a real host, runner, and shell.
FROM omnigent-server:nginx-prototype
RUN apt-get update \
 && apt-get install -y --no-install-recommends git tmux \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
HEALTHCHECK NONE
