FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Los_Angeles

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml requirements.txt README.md ./
COPY schwab_tracker ./schwab_tracker

RUN pip install .

# Runtime workdir — mount your .env here from the host.
WORKDIR /data

# Token store — mount a host directory here so tokens survive container rebuilds.
VOLUME ["/root/.schwab_tracker"]

ENTRYPOINT ["schwab-tracker"]
CMD ["--help"]
