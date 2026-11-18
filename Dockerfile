# Local container only. Neurons has real shell/file tools and no built-in auth.
# Publish the port to 127.0.0.1 (see README) or put it behind an authenticated
# private network / reverse proxy. Do not expose this container publicly.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN mkdir -p /app/data
VOLUME ["/app/data"]
EXPOSE 8770

CMD ["python", "run.py", "--host", "0.0.0.0", "--no-screen"]
