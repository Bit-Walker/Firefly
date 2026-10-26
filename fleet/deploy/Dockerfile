FROM python:3.12-slim
WORKDIR /app
COPY fleet/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home fleet && mkdir /data && chown fleet:fleet /data
COPY apps/control-plane/src /app/apps/control-plane/src
COPY packages/analysis/src /app/packages/analysis/src
COPY apps/web /app/apps/web
COPY hardware/boards /app/hardware/boards
COPY contracts/drafts/operation-v0/request.schema.json /app/contracts/drafts/operation-v0/request.schema.json
ENV FLEET_STATE=/data
ENV PYTHONPATH=/app/apps/control-plane/src:/app/packages/analysis/src
USER fleet
EXPOSE 8000
CMD ["uvicorn", "edbg_server.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
