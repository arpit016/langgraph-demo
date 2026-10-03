import os

from dotenv import load_dotenv
from hindsight_client import Hindsight

load_dotenv()

client = Hindsight(base_url=os.environ.get("HINDSIGHT_URL", "http://localhost:8888"))


def bank_id(user_id: str) -> str:
    return f"tripmate-{user_id}"


def retain(user_id: str, content: str) -> None:
    client.retain(bank_id=bank_id(user_id), content=content)


def recall(user_id: str, query: str):
    return client.recall(bank_id=bank_id(user_id), query=query)
