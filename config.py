import os
import sys
from dotenv import load_dotenv

load_dotenv()

raw_api_id = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()

# Fallback parser if python-dotenv failed to load
if not raw_api_id or not API_HASH:
    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip("'").strip('"')
        raw_api_id = os.getenv("TELEGRAM_API_ID")
        API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()

if not raw_api_id or not API_HASH:
    sys.exit("[Error] Missing TELEGRAM_API_ID or TELEGRAM_API_HASH in .env file.")

try:
    API_ID = int(raw_api_id.strip())
except ValueError:
    sys.exit("[Error] TELEGRAM_API_ID in .env must be an integer.")