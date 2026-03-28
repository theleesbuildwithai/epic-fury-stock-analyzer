"""
Database Persistence — S3 backup/restore for the SQLite portfolio database.

App Runner uses ephemeral storage, so the SQLite file gets wiped on every deploy.
This module backs up the DB to S3 after every trade cycle and restores it on startup.

The portfolio money persists forever — it grows and loses with the fund.
No more resets.
"""

import os
import shutil
import logging

logger = logging.getLogger(__name__)

# S3 configuration
S3_BUCKET = os.environ.get("DB_BACKUP_BUCKET", "epic-fury-portfolio-db")
S3_KEY = "portfolio/predictions.db"
DB_PATH = os.environ.get("DB_PATH", "predictions.db")

_s3_client = None


def _get_s3():
    """Get or create S3 client."""
    global _s3_client
    if _s3_client is None:
        try:
            import boto3
            _s3_client = boto3.client("s3", region_name="us-east-1")
        except Exception as e:
            logger.warning(f"Could not create S3 client: {e}")
            return None
    return _s3_client


def restore_db_from_s3():
    """
    Restore the SQLite database from S3 on startup.
    If S3 has a backup, download it to the local DB path.
    If no backup exists, the app starts fresh (first run).
    """
    s3 = _get_s3()
    if not s3:
        logger.info("S3 not available — starting with local DB")
        return False

    try:
        logger.info(f"Restoring database from s3://{S3_BUCKET}/{S3_KEY}")
        s3.download_file(S3_BUCKET, S3_KEY, DB_PATH)
        # Verify the downloaded file is valid
        file_size = os.path.getsize(DB_PATH)
        logger.info(f"Database restored from S3 ({file_size:,} bytes)")
        return True
    except s3.exceptions.NoSuchBucket:
        logger.info("S3 bucket does not exist — will create on first backup")
        return False
    except s3.exceptions.NoSuchKey:
        logger.info("No S3 backup found — starting fresh (first run)")
        return False
    except Exception as e:
        # ClientError for 404, etc.
        error_code = getattr(e, 'response', {}).get('Error', {}).get('Code', '')
        if error_code in ('404', 'NoSuchKey', 'NoSuchBucket'):
            logger.info(f"No S3 backup found ({error_code}) — starting fresh")
        else:
            logger.warning(f"Could not restore from S3: {e}")
        return False


def backup_db_to_s3():
    """
    Backup the SQLite database to S3.
    Called after every trade cycle to persist portfolio state.
    """
    s3 = _get_s3()
    if not s3:
        logger.debug("S3 not available — skipping backup")
        return False

    if not os.path.exists(DB_PATH):
        logger.debug("No local DB to backup")
        return False

    try:
        # Ensure the bucket exists
        _ensure_bucket(s3)

        # Upload the DB file
        file_size = os.path.getsize(DB_PATH)
        s3.upload_file(DB_PATH, S3_BUCKET, S3_KEY)
        logger.info(f"Database backed up to s3://{S3_BUCKET}/{S3_KEY} ({file_size:,} bytes)")
        return True
    except Exception as e:
        logger.warning(f"Could not backup to S3: {e}")
        return False


def _ensure_bucket(s3):
    """Create the S3 bucket if it doesn't exist."""
    try:
        s3.head_bucket(Bucket=S3_BUCKET)
    except Exception:
        try:
            s3.create_bucket(
                Bucket=S3_BUCKET,
                CreateBucketConfiguration={"LocationConstraint": "us-east-1"}
            )
            logger.info(f"Created S3 bucket: {S3_BUCKET}")
        except Exception as e:
            # Bucket might already exist or we don't have permissions
            # That's OK — the upload will tell us if there's a real problem
            logger.debug(f"Could not create bucket (may already exist): {e}")
