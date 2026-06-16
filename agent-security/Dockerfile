FROM python:3.12-slim

# The agent's tool implementations (filesystem/shell/web) are bind-mounted
# into /agent_tools at runtime, so the image only needs their dependencies.
RUN pip install --no-cache-dir beautifulsoup4

# Keep the container alive; the host runs tools via `docker exec`.
CMD ["sleep", "infinity"]
