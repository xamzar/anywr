FROM python:3.12-slim-bookworm
# CDP client only: no browsers in this image.
RUN pip install --no-cache-dir fastmcp==3.4.5 playwright==1.55.0 pyyaml==6.0.2
RUN useradd -m -u 1000 soak
USER soak
WORKDIR /app/src
ENV PYTHONUNBUFFERED=1 SOAK_DB=/data/soak.db
CMD ["python", "mcp_server.py"]
