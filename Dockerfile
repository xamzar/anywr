FROM python:3.12-slim
RUN pip install --no-cache-dir "fastmcp==4.0.5" "fastapi>=0.115" "uvicorn>=0.30" "httpx>=0.27" playwright==1.55.0
WORKDIR /app
COPY app.py test_app.py ./
COPY static/ static/
ENV PYTHONUNBUFFERED=1
CMD ["python", "app.py"]
