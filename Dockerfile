FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y \
    git gcc python3-dev libxml2-dev libxslt-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --upgrade pip
RUN pip install maigret
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
