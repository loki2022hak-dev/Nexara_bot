FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    git curl build-essential \
    libxml2-dev libxslt1-dev zlib1g-dev \
    libjpeg-dev libfreetype6-dev \
    liblcms2-dev libopenjp2-7-dev \
    libtiff5-dev libwebp-dev \
    libharfbuzz-dev libfribidi-dev \
    libxcb1-dev libpng-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN python -m venv /opt/maigret-venv && \
    /opt/maigret-venv/bin/pip install --upgrade pip setuptools wheel && \
    /opt/maigret-venv/bin/pip install --no-cache-dir git+https://github.com/soxoj/maigret.git

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

CMD ["python", "bot.py"]
