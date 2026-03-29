NEXARA Stabilized Monolith (defensive asset-intel architecture)

Contents:
- config.py
- db.py
- models.py
- providers.py
- pipeline.py
- report_builder.py
- bot.py
- setup.sh
- requirements.txt
- .env.example

Capabilities:
- Aiogram 3 bot
- PostgreSQL via SQLAlchemy async
- Retry/backoff + timeout handling
- API health checks
- Module fallback recording
- Dedup engine
- Entity linking
- Confidence scoring
- Async task queue
- Watchdog task
- TXT/PDF export
- Pricing + limits scaffold

Notes:
- Fill .env with your own keys and DATABASE_URL
- Run ./setup.sh
- Start with: . .venv/bin/activate && python bot.py
