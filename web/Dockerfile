FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir pip --upgrade

COPY pyproject.toml .
COPY saorsa_web/ saorsa_web/
COPY wsgi.py .
COPY alembic.ini .
COPY alembic/ alembic/
COPY templates/ templates/

RUN pip install --no-cache-dir .

EXPOSE 8080

CMD ["gunicorn", "wsgi:app", "--bind", "0.0.0.0:8080", "--workers", "2"]
