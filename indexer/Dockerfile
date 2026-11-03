# The indexer image: the Job and CronJob that build the index, then exit (section 4.2).
#
# Build context: the REPOSITORY ROOT, not indexer/, because the image also carries the
# corpus fetch script and its manifest, and the CI fixture corpus:
#     docker build -t rahbar-indexer:dev -f indexer/Dockerfile .
# indexer/Dockerfile.dockerignore limits that context to the files copied below.

# Stage 1: install the pinned dependencies into a virtualenv we can copy out.
FROM python:3.12.16-slim-trixie AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY indexer/requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

# Stage 2: the runtime, with the system packages OCR needs (section 3.1, extract):
# tesseract for OCR, poppler-utils for pdf2image (pdftoppm) and pdffonts.
FROM python:3.12.16-slim-trixie
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils \
 && rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CORPUS_DIR=/corpus
RUN groupadd --system --gid 10001 rahbar \
 && useradd --system --uid 10001 --gid rahbar --no-create-home --shell /usr/sbin/nologin rahbar
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY indexer/pipeline ./pipeline
# How the real corpus reaches the Job: an initContainer runs fetch.py into a volume (ADR 0001).
COPY corpus/fetch.py corpus/sources.yaml corpus/MANIFEST.md ./corpus/
# The CI corpus: the e2e-eval job sets CORPUS_DIR=/app/fixtures.
COPY eval/fixtures ./fixtures
USER 10001
CMD ["python", "-m", "pipeline.run"]
