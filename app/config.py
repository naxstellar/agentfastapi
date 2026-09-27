from pathlib import Path

from dotenv import load_dotenv
import os

ENV_PATH = Path(__file__).resolve().parent.parent / "env" / ".env"
load_dotenv(ENV_PATH)

AUTH0_DOMAIN = os.environ["AUTH0_DOMAIN"]
AUTH0_CLIENT_ID = os.environ["AUTH0_CLIENT_ID"]
AUTH0_CLIENT_SECRET = os.environ["AUTH0_CLIENT_SECRET"]
AUTH0_AUDIENCE = os.environ["AUTH0_AUDIENCE"]
AUTH0_CALLBACK_URL = os.environ["AUTH0_CALLBACK_URL"]
APP_BASE_URL = os.environ["APP_BASE_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
SESSION_SECRET_KEY = os.environ["SESSION_SECRET_KEY"]

AUTH0_ISSUER = f"https://{AUTH0_DOMAIN}/"
AUTH0_JWKS_URL = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
