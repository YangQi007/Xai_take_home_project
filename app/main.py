"""FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.metrics import create_metrics_middleware, router as metrics_router
from app.api.middleware import ErrorHandlingMiddleware, RateLimitMiddleware
from app.api.v1 import router as api_v1_router
from app.config import get_settings
from app.database import close_db, init_db
from app.processing.worker import start_worker_pool, stop_worker_pool
from app.services.grok_client import close_grok_client

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    logger.info("Starting application...")

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Start worker pool
    await start_worker_pool()
    logger.info("Worker pool started")

    yield

    # Shutdown
    logger.info("Shutting down application...")

    # Stop workers
    await stop_worker_pool()
    logger.info("Worker pool stopped")

    # Close Grok client
    await close_grok_client()
    logger.info("Grok client closed")

    # Close database
    await close_db()
    logger.info("Database closed")


# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    description="Production-oriented backend for X conversation analytics with Grok API integration",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Add middleware (order matters - last added is first executed)
app.add_middleware(ErrorHandlingMiddleware)
app.add_middleware(
    RateLimitMiddleware,
    requests_per_minute=settings.rate_limit_requests_per_minute,
    burst=settings.rate_limit_burst,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add metrics middleware
app.middleware("http")(create_metrics_middleware())

# Include routers
app.include_router(api_v1_router)
app.include_router(metrics_router)


@app.get("/", tags=["health"])
async def root():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": "1.0.0",
    }


@app.get("/health", tags=["health"])
async def health_check():
    """Detailed health check."""
    from app.processing import get_circuit_breaker, get_processing_queue

    queue = get_processing_queue()
    circuit = get_circuit_breaker()

    return {
        "status": "healthy",
        "components": {
            "queue": {
                "depth": queue.depth,
                "is_full": queue.is_full,
            },
            "circuit_breaker": {
                "state": circuit.state.value,
            },
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
    )
