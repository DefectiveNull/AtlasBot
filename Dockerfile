# Pin a digest for production: --build-arg PYTHON_BASE=python:3.13-slim@sha256:...
ARG PYTHON_BASE=python:3.13-slim
FROM ${PYTHON_BASE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY aibot /app/aibot
COPY aibot.py LICENSE /app/
USER 10001:10001
CMD ["python", "aibot.py"]
