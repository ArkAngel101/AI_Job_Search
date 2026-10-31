FROM python:3.12-slim

# LibreOffice (PDF export, Phase 8) and poppler (pdftotext, Phase 7) are installed now so the image is stable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer poppler-utils fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# The container runs read-only, so HOME (LibreOffice profile, pip cache) points at the /tmp tmpfs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin forge

WORKDIR /srv/forge
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

USER forge
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
