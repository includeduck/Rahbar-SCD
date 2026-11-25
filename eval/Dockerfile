# The eval image: the Part B suite (eval/suite.py) and the release gate, run as a Kubernetes
# Job (k8s/eval-job.yaml). It carries the api package, so retrieval is measured with the
# api's own retriever and guardrails.
# Build context: the repository root:
#   docker build -t rahbar-eval:dev --build-arg GIT_SHA=$(git rev-parse --short HEAD) -f eval/Dockerfile .
FROM python:3.12.16-slim-trixie AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY api/requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt pyyaml==6.0.2

FROM python:3.12.16-slim-trixie
ARG GIT_SHA=-
RUN apt-get update \
 && apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EVAL_RESULTS_DIR=/results \
    GIT_SHA=$GIT_SHA
RUN groupadd --system --gid 10001 rahbar \
 && useradd --system --uid 10001 --gid rahbar --no-create-home --shell /usr/sbin/nologin rahbar
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY api/app ./app
COPY eval ./eval
USER 10001
CMD ["python", "-m", "eval.suite"]
