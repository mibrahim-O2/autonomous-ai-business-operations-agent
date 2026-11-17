# Autonomous AI Business Operations Agent: API, worker, and dashboard in one image.
#
#   docker build -t bizops-agent .
#   docker build -t bizops-agent --build-arg EXTRAS="[openai]" .   # with the OpenAI SDK
#
# The container runs as an unprivileged user, keeps state in /data (mount a volume),
# and receives every secret from the environment at run time. No secret is baked in.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BIZOPS_DATA_DIR=/data \
    BIZOPS_LOG_FORMAT=json

WORKDIR /app
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin bizops \
    && mkdir -p /data && chown bizops:bizops /data

# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml README.md ./
COPY src ./src
ARG EXTRAS=""
RUN pip install -e ".${EXTRAS}"

# Seed data, policy documents, and the dashboard are read at run time from the source tree.
COPY data ./data
COPY dashboard ./dashboard

USER bizops
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["bizops", "serve", "--host", "0.0.0.0", "--port", "8000"]
