# Pinned: python 3.12.16 (digest checked 2026-11-01). Upgrade on a test machine first.
FROM python:3.12.16-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

RUN useradd --system --uid 1000 dcv && mkdir -p /data/audio && chown -R dcv /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY dcv_stt_chain ./dcv_stt_chain

USER dcv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 10310
HEALTHCHECK --interval=30s --timeout=12s --start-period=10s --retries=3 \
    CMD ["python", "-m", "dcv_stt_chain.healthcheck"]
ENTRYPOINT ["python", "-m", "dcv_stt_chain"]
