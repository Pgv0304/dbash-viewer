FROM python:3.12-slim

# Optional: Oracle Instant Client 21 for "thick" mode (needed for Oracle DB 11.2; IC 23 no longer supports 11g).
# Build with --build-arg INSTANT_CLIENT=1 and run with ORACLE_THICK=1.
ARG INSTANT_CLIENT=0
ARG IC_URL=https://download.oracle.com/otn_software/linux/instantclient/2119000/instantclient-basiclite-linux.x64-21.19.0.0.0dbru.zip
RUN if [ "$INSTANT_CLIENT" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
      && (apt-get install -y --no-install-recommends libaio1t64 || apt-get install -y --no-install-recommends libaio1) \
      && (ln -s /usr/lib/x86_64-linux-gnu/libaio.so.1t64 /usr/lib/x86_64-linux-gnu/libaio.so.1 || true) \
      && curl -fsSL -o /tmp/ic.zip "$IC_URL" \
      && unzip -q /tmp/ic.zip -d /opt/oracle && rm /tmp/ic.zip \
      && echo /opt/oracle/instantclient_* > /etc/ld.so.conf.d/oic.conf && ldconfig \
      && apt-get purge -y curl unzip && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /opt/dbash
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV ASHV_DB=/data/dbash.db ASHV_CONFIG=/config/targets.json PGASH_CONN_FILE=/config/pg.txt PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
