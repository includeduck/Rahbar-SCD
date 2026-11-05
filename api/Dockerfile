# The api image: FastAPI reading the index, never writing it (section 4.2).
# Build context: api/   ->   docker build -t rahbar-api:dev api

# Stage 1: install the pinned dependencies into a virtualenv we can copy out.
FROM python:3.12.16-slim-trixie AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

# Stage 2: the runtime. No compilers, no pip cache, no tests.
FROM python:3.12.16-slim-trixie
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# A fixed non-root uid, so the pod's runAsNonRoot check can verify it (section 4.7).
RUN groupadd --system --gid 10001 rahbar \
 && useradd --system --uid 10001 --gid rahbar --no-create-home --shell /usr/sbin/nologin rahbar
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
USER 10001
EXPOSE 8000
# Exec form, so SIGTERM from the kubelet reaches uvicorn and the preStop drain works.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
