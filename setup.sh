#!/usr/bin/env bash
set -e

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

mkdir -p data/logs data/results data/pdf

echo "SETUP DONE"
echo "1) Copy .env.example to .env"
echo "2) Fill keys + DATABASE_URL"
echo "3) Run: . .venv/bin/activate && python bot.py"
