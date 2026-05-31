"""
India Energy Vessel Tracker — entrypoint.

Usage:
    python main.py

Prerequisites:
    pip install -r requirements.txt
    cp .env.example .env
    # Edit .env and set AISSTREAM_API_KEY (free at https://aisstream.io)
"""
import logging
import os
import sys

from dotenv import load_dotenv

# Load .env before anything else
load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Validate required environment variables
# ---------------------------------------------------------------------------
API_KEY = os.getenv("AISSTREAM_API_KEY", "").strip()
if not API_KEY:
    log.error("AISSTREAM_API_KEY is not set.")
    log.error("Register for a free key at https://aisstream.io")
    log.error("Then set it in your .env file: AISSTREAM_API_KEY=your_key_here")
    sys.exit(1)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# ---------------------------------------------------------------------------
# Start server
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError:
        log.error("uvicorn not installed. Run: pip install -r requirements.txt")
        sys.exit(1)

    log.info("=" * 60)
    log.info("  India Energy Vessel Tracker")
    log.info("  Dashboard → http://%s:%d/", HOST if HOST != "0.0.0.0" else "localhost", PORT)
    log.info("  Health   → http://localhost:%d/api/health", PORT)
    log.info("=" * 60)

    uvicorn.run(
        "api.server:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level=LOG_LEVEL.lower(),
        access_log=True,
    )
