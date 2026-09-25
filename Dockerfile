FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# mysqlclient builds against the MySQL/MariaDB C client library
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential pkg-config default-libmysqlclient-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python manage.py collectstatic --noinput

RUN useradd -m -u 1000 obuser \
 && mkdir -p /app/data /app/Media \
 && chown -R obuser /app
USER obuser

EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn OpenSite.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 180"]
