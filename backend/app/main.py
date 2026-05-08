"""Entry point: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`"""

import logging

from .api import make_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

app = make_app()
