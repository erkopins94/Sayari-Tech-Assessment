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
