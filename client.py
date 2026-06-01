"""
client.py — Authenticated Sayari SDK client factory.

Reads CLIENT_ID and CLIENT_SECRET from the .env file and returns a
ready-to-use Sayari client instance. Imported by resolver.py, fetcher.py,
and rel_fetcher.py whenever a live API call is needed.

All three pipeline scripts use load_or_build_* patterns so this module is
only invoked on a cache miss — never during normal dashboard use.
"""

import os

from dotenv import load_dotenv
from sayari.client import Sayari

load_dotenv()


def get_client() -> Sayari:
    client_id = os.getenv("CLIENT_ID")
    client_secret = os.getenv("CLIENT_SECRET")

    if not client_id or not client_secret:
        raise EnvironmentError(
            "Missing Sayari credentials. Ensure CLIENT_ID and CLIENT_SECRET are set in your .env file."
        )

    return Sayari(client_id=client_id, client_secret=client_secret)
