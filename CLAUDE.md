# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Production-oriented RESTful backend that ingests Twitter customer support conversations (from sample.csv) and generates insights via the Grok API. Focus areas: systems engineering depth, cost optimization, and production readiness.

## Quick Start

```bash
# Start everything (API + DB + auto-load sample data)
docker compose up

# API available at http://localhost:8000
# API docs at http://localhost:8000/docs
```

## Development Commands

```bash
# Run tests
python -m pytest tests/ -v

# Run locally (requires Docker DB running)
export DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5433/conversations"
uvicorn app.main:app --reload

# Rebuild after code changes
docker compose build --no-cache && docker compose up -d
```

## Project Structure

```
app/
├── api/v1/          # API endpoints (conversations, insights, trends)
├── models/          # SQLAlchemy models (Tweet, Conversation, Insight)
├── schemas/         # Pydantic schemas for validation
├── services/        # Business logic (ingestion, grok_client, analysis, cache)
├── processing/      # Queue, rate limiter, circuit breaker, workers
└── main.py          # FastAPI application entry
```

## Data Schema (sample.csv)

CSV columns: `tweet_id`, `author_id`, `inbound`, `created_at`, `text`, `response_tweet_id`, `in_response_to_tweet_id`

- `inbound=True`: customer message; `inbound=False`: company response
- `response_tweet_id`: comma-separated list of tweet IDs that responded to this tweet
- `in_response_to_tweet_id`: parent tweet ID (empty for thread roots)
- Conversations form tree structures via these relationships

## API Endpoints

- `POST /api/v1/conversations` - Single conversation
- `POST /api/v1/conversations/bulk` - Up to 500 conversations
- `GET /api/v1/conversations` - List conversations
- `GET /api/v1/insights` - Filtered insights retrieval
- `GET /api/v1/insights/stats` - Aggregate statistics
- `GET /api/v1/trends` - Time-windowed aggregates (sentiment drift, gaps, volume)
- `GET /metrics` - Prometheus-style metrics
- `GET /health` - Health check with component status

## Key Architecture Patterns

**Processing Pipeline:**
- Two-stage filtering: cheap heuristic pre-filter → expensive Grok calls only on interesting conversations
- Adaptive batch sizing: grow on success, shrink on errors/latency spikes
- Semantic similarity cache (text hash) to avoid repeated analysis

**Resilience:**
- Circuit breakers for Grok API failures (CLOSED → OPEN → HALF_OPEN)
- Backpressure handling with queue depth monitoring
- Rate limiting with 429 + Retry-After headers

**Observability (Prometheus metrics):**
- Request latency histograms (p50/p95/p99)
- Grok call success/error rates
- Token/cost consumption estimates
- Queue depth and backpressure events

## Environment

- Grok API key stored in `.env` as `xai_api_key`
- PostgreSQL exposed on port `5433` (host) → `5432` (container)
- API exposed on port `8000`
