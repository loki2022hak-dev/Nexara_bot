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

COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
