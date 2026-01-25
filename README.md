# X Conversation Analytics Backend

Production-oriented RESTful backend that ingests Twitter customer support conversations and generates insights via the Grok API.

## Features

- **Conversation Ingestion**: Bulk and single conversation submission with automatic thread reconstruction
- **Grok API Integration**: Sentiment analysis, topic extraction, and gap identification
- **Two-Stage Filtering**: Cheap pre-filters before expensive API calls
- **Adaptive Rate Limiting**: Self-tuning rate limiter that grows on success, shrinks on errors
- **Circuit Breaker**: Automatic failure recovery with CLOSED → OPEN → HALF_OPEN states
- **Backpressure Handling**: Queue depth monitoring with 503 responses when at capacity
- **Prometheus Metrics**: Request latency histograms, Grok API success/error rates, cost tracking

## Quick Start

### Using Docker Compose (Recommended)

```bash
# 1. Set your Grok API key
echo "xai_api_key=your_key_here" > .env

# 2. Start everything (API + DB + auto-loads sample data)
docker compose up

# API available at http://localhost:8000
# API docs at http://localhost:8000/docs
```

That's it! The sample data loads automatically on first startup.

> **Note**: Without the API key, the system runs but Grok analysis is disabled. All other features work.

### Local Development

```bash
# Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Start database via Docker
docker compose up -d db

# Set environment variables
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/conversations
export xai_api_key=your_api_key_here

# Run the application
uvicorn app.main:app --reload

# Load sample data (in another terminal)
python scripts/load_sample_data.py
```

## API Endpoints

### Conversations

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/conversations` | POST | Submit single conversation |
| `/api/v1/conversations/bulk` | POST | Submit up to 500 conversations |
| `/api/v1/conversations/{id}` | GET | Get conversation by ID |
| `/api/v1/conversations` | GET | List conversations with filtering |

### Insights

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/insights` | GET | Get insights with filtering |
| `/api/v1/insights/{id}` | GET | Get specific insight |
| `/api/v1/insights/conversation/{id}` | GET | Get insights for a conversation |
| `/api/v1/insights/stats` | GET | Get aggregate statistics |

### Trends

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/trends` | GET | Time-windowed aggregates |

### System

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check |
| `/health` | GET | Detailed health check |
| `/metrics` | GET | Prometheus metrics |
| `/docs` | GET | OpenAPI documentation |

## API Examples

### Submit a Conversation

```bash
curl -X POST http://localhost:8000/api/v1/conversations \
  -H "Content-Type: application/json" \
  -d '{
    "tweets": [
      {
        "tweet_id": 1,
        "author_id": "customer123",
        "inbound": true,
        "created_at": "2024-01-01T10:00:00Z",
        "text": "Having issues with my account @Support"
      },
      {
        "tweet_id": 2,
        "author_id": "Support",
        "inbound": false,
        "created_at": "2024-01-01T10:05:00Z",
        "text": "Sorry to hear that! DM us your details.",
        "in_response_to_tweet_id": 1
      }
    ]
  }'
```

### Get Insights with Filtering

```bash
# Filter by sentiment
curl "http://localhost:8000/api/v1/insights?sentiment=negative&limit=10"

# Filter by date range
curl "http://localhost:8000/api/v1/insights?start_date=2024-01-01&end_date=2024-01-31"

# Filter by topic
curl "http://localhost:8000/api/v1/insights?topic=billing"
```

### Get Trends

```bash
# Daily trends for last 7 days
curl "http://localhost:8000/api/v1/trends?window=day"

# Hourly trends with custom date range
curl "http://localhost:8000/api/v1/trends?window=hour&start_date=2024-01-01&end_date=2024-01-02"
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        FastAPI Application                       │
├─────────────────┬─────────────────┬─────────────────────────────┤
│   Rate Limiter  │  Error Handler  │     CORS Middleware         │
└────────┬────────┴────────┬────────┴──────────────┬──────────────┘
         │                 │                       │
         ▼                 ▼                       ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────────────┐
│ /api/v1/convers │ │ /api/v1/insights│ │ /api/v1/trends          │
│ POST single     │ │ GET filtered    │ │ GET aggregated          │
│ POST bulk       │ │ GET stats       │ │                         │
└────────┬────────┘ └────────┬────────┘ └─────────────────────────┘
         │                   │
         ▼                   │
┌─────────────────┐         │
│ Processing Queue│◄────────┘
│ (Backpressure)  │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Worker Pool                               │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐   │
│  │ Worker 1│ │ Worker 2│ │ Worker 3│ │ Worker 4│ │ Worker 5│   │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘   │
└───────┼──────────┼──────────┼──────────┼──────────┼─────────────┘
        │          │          │          │          │
        ▼          ▼          ▼          ▼          ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Analysis Pipeline                            │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │ Pre-Filter   │─▶│ Cache Check  │─▶│ Grok API Call        │   │
│  │ (cheap)      │  │ (SHA-256)    │  │ (expensive)          │   │
│  └──────────────┘  └──────────────┘  └──────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Resilience Layer                              │
│  ┌──────────────────┐           ┌───────────────────────────┐   │
│  │ Adaptive Rate    │           │ Circuit Breaker           │   │
│  │ Limiter          │           │ CLOSED ─▶ OPEN ─▶ HALF    │   │
│  │ grow/shrink      │           │                           │   │
│  └──────────────────┘           └───────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      PostgreSQL                                  │
│  tweets │ tweet_relationships │ conversations │ insights │ cache │
└─────────────────────────────────────────────────────────────────┘
```

## Architecture Trade-offs

### 1. Async vs Sync Processing
**Choice**: Fully async with background workers
**Trade-off**: More complex code, but handles 5k-10k conversations without blocking API responses
**Alternative**: Sync processing would be simpler but create request timeouts on large batches

### 2. Two-Stage Filtering
**Choice**: Cheap pre-filter before expensive Grok API calls
**Trade-off**: May skip some conversations that could be interesting, but significantly reduces API costs
**Alternative**: Analyze everything - simpler but 10x+ more expensive

### 3. Circuit Breaker Pattern
**Choice**: Fail fast when Grok API is down
**Trade-off**: Some conversations won't be analyzed during outages, but prevents cascade failures
**Alternative**: Retry indefinitely - could exhaust resources and delay recovery

### 4. Adaptive Rate Limiting
**Choice**: Dynamic rate adjustment based on success/failure
**Trade-off**: More complex than fixed rate, but optimizes throughput automatically
**Alternative**: Fixed rate limit - simpler but either too conservative or causes errors

### 5. SHA-256 Text Hash for Caching
**Choice**: Exact text match via hash
**Trade-off**: Won't catch semantically similar conversations, but simple and fast
**Alternative**: Embedding-based similarity - more powerful but requires vector DB

### 6. PostgreSQL for Everything
**Choice**: Single database for data + cache + queue state
**Trade-off**: Simpler deployment, but PostgreSQL isn't optimized for queue workloads
**Alternative**: Redis for cache/queue - better performance but more infrastructure

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | - | PostgreSQL connection string |
| `xai_api_key` | - | Grok API key |
| `DEBUG` | `false` | Enable debug mode |
| `WORKER_COUNT` | `5` | Number of background workers |
| `QUEUE_MAX_SIZE` | `1000` | Max queue depth |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `100` | API rate limit |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | Failures before opening |
| `CIRCUIT_BREAKER_RECOVERY_TIMEOUT` | `30` | Seconds before half-open |

## Metrics

The `/metrics` endpoint exposes Prometheus-compatible metrics:

| Metric | Type | Description |
|--------|------|-------------|
| `request_latency_seconds` | Histogram | Request latency (p50/p95/p99) |
| `grok_requests_total` | Counter | Grok API calls by status |
| `grok_tokens_total` | Counter | Tokens consumed |
| `grok_cost_dollars_total` | Counter | Estimated API cost |
| `queue_depth` | Gauge | Current queue depth |
| `backpressure_events_total` | Counter | Queue full rejections |
| `circuit_breaker_state` | Gauge | 0=closed, 1=open, 2=half_open |
| `cache_hits_total` | Counter | Analysis cache hits |
| `cache_misses_total` | Counter | Analysis cache misses |

## Cost Optimization

The system implements several cost optimization strategies:

1. **Two-Stage Filtering**
   - Stage 1 (cheap): Skip single-message threads, short conversations, already-analyzed
   - Stage 2 (expensive): Only call Grok for interesting conversations

2. **Semantic Cache**
   - SHA-256 hash of normalized conversation text
   - Cached results reused for identical conversations
   - Configurable TTL (default 24 hours)

3. **Adaptive Batching**
   - Rate limiter grows on success streaks
   - Shrinks on errors or latency spikes
   - Prevents wasted API calls during outages

## Troubleshooting

### Queue Full (503 Service Unavailable)

The processing queue has reached capacity. Solutions:
- Increase `QUEUE_MAX_SIZE`
- Add more workers (`WORKER_COUNT`)
- Check if Grok API is responding (circuit may be open)

### Circuit Breaker Open

Too many Grok API failures detected. The circuit will automatically try to recover after `CIRCUIT_BREAKER_RECOVERY_TIMEOUT` seconds.

Check:
- Grok API key validity
- Network connectivity
- Grok API status

### Rate Limited (429 Too Many Requests)

API rate limit exceeded. The response includes a `Retry-After` header indicating when to retry.

### Database Connection Issues

Verify:
- PostgreSQL is running
- `DATABASE_URL` is correct
- Network connectivity to database

## Testing

```bash
# Run all tests
pytest tests/

# Run with coverage
pytest tests/ --cov=app --cov-report=html

# Run specific test file
pytest tests/test_services.py -v
```

## Data Schema

### CSV Input Format (sample.csv)

| Column | Description |
|--------|-------------|
| `tweet_id` | Unique tweet identifier |
| `author_id` | User or company handle |
| `inbound` | `True` = customer, `False` = company |
| `created_at` | Tweet timestamp |
| `text` | Tweet content |
| `response_tweet_id` | Comma-separated IDs of responses |
| `in_response_to_tweet_id` | Parent tweet ID (empty for roots) |

### Database Schema

- **tweets**: Raw tweet storage with text hash for deduplication
- **tweet_relationships**: Parent-child relationships for thread structure
- **conversations**: Reconstructed threads with metadata
- **insights**: Grok analysis results (sentiment, topics, gaps)
- **analysis_cache**: SHA-256 indexed cache for avoiding duplicate API calls

## License

MIT
