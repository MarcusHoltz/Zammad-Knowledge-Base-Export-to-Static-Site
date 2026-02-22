# It was easiest to just generate a new image for repeat use. Use `docker compose run`
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir requests markdownify python-slugify pyyaml

COPY export.py .

CMD ["python", "export.py"]
