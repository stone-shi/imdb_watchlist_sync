import json
import math
import os

import requests
from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = "data"
INDEX_FILE = os.path.join(CACHE_DIR, "embedding_index.json")

API_URL = os.environ.get("EMBEDDING_API_URL")
API_KEY = os.environ.get("EMBEDDING_API_KEY")
MODEL = os.environ.get("EMBEDDING_MODEL")


def is_configured() -> bool:
    return bool(API_URL and MODEL)


def embed_texts(texts: list) -> list:
    """Call the embeddings API for a batch of texts. Returns one vector per input text, in order."""
    headers = {}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    resp = requests.post(
        API_URL.rstrip("/") + "/embeddings",
        headers=headers,
        json={"model": MODEL, "input": texts},
        timeout=30,
    )
    resp.raise_for_status()
    return [d["embedding"] for d in resp.json()["data"]]


def cosine_similarity(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def load_index() -> dict:
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)
    if os.path.exists(INDEX_FILE):
        try:
            with open(INDEX_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_index(index: dict):
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)
    with open(INDEX_FILE, "w") as f:
        json.dump(index, f)


def get_embeddings(items: list) -> dict:
    """Return {imdb_id: embedding} for every item, computing and persisting (in INDEX_FILE) any
    embeddings that are missing or were computed with a different model/title. Each item must
    have 'imdb' and 'title' keys."""
    index = load_index()
    to_embed = [
        item
        for item in items
        if item.get("imdb")
        and not (
            index.get(item["imdb"])
            and index[item["imdb"]].get("model") == MODEL
            and index[item["imdb"]].get("title") == item["title"]
        )
    ]

    if to_embed:
        vectors = embed_texts([item["title"] for item in to_embed])
        for item, vector in zip(to_embed, vectors):
            index[item["imdb"]] = {"title": item["title"], "model": MODEL, "embedding": vector}
        save_index(index)

    return {
        item["imdb"]: index[item["imdb"]]["embedding"]
        for item in items
        if item.get("imdb") in index
    }
