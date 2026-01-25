#!/usr/bin/env python3
"""
Load sample.csv data into the database.

Usage:
    python scripts/load_sample_data.py
    python scripts/load_sample_data.py --file /path/to/custom.csv
    python scripts/load_sample_data.py --analyze  # Queue conversations for analysis
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import get_settings
from app.database import get_db_context, init_db
from app.processing import get_processing_queue
from app.services.ingestion import get_ingestion_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def load_csv_file(file_path: str, queue_for_analysis: bool = False) -> None:
    """Load a CSV file into the database."""
    logger.info(f"Loading data from {file_path}")

    # Read CSV content
    with open(file_path, "r", encoding="utf-8") as f:
        csv_content = f.read()

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Ingest data
    ingestion_service = get_ingestion_service()

    async with get_db_context() as db:
        tweets_created, conversations_created, errors = await ingestion_service.ingest_csv(
            db, csv_content
        )

        logger.info(f"Created {tweets_created} tweets")
        logger.info(f"Created {conversations_created} conversations")

        if errors:
            logger.warning(f"Errors ({len(errors)}):")
            for error in errors[:10]:  # Show first 10 errors
                logger.warning(f"  - {error}")
            if len(errors) > 10:
                logger.warning(f"  ... and {len(errors) - 10} more errors")

        # Queue conversations for analysis if requested
        if queue_for_analysis and conversations_created > 0:
            logger.info("Queueing conversations for analysis...")
            queue = get_processing_queue()

            from sqlalchemy import select
            from app.models import Conversation, ConversationStatus

            result = await db.execute(
                select(Conversation.id).where(
                    Conversation.status == ConversationStatus.PENDING
                )
            )
            conversation_ids = [row[0] for row in result]

            queued = 0
            for conv_id in conversation_ids:
                if await queue.enqueue(conv_id):
                    queued += 1
                else:
                    logger.warning("Queue full, stopping queueing")
                    break

            logger.info(f"Queued {queued} conversations for analysis")

    logger.info("Data loading complete!")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Load sample data into database")
    parser.add_argument(
        "--file",
        "-f",
        default="sample.csv",
        help="Path to CSV file (default: sample.csv)",
    )
    parser.add_argument(
        "--analyze",
        "-a",
        action="store_true",
        help="Queue conversations for analysis after loading",
    )
    args = parser.parse_args()

    # Find file
    file_path = Path(args.file)
    if not file_path.is_absolute():
        # Check relative to script and project root
        script_dir = Path(__file__).parent
        project_root = script_dir.parent

        if (project_root / file_path).exists():
            file_path = project_root / file_path
        elif (script_dir / file_path).exists():
            file_path = script_dir / file_path

    if not file_path.exists():
        logger.error(f"File not found: {file_path}")
        sys.exit(1)

    await load_csv_file(str(file_path), queue_for_analysis=args.analyze)


if __name__ == "__main__":
    asyncio.run(main())
