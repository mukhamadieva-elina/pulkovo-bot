"""Сменный отбор: полный каталог, лексический контрольный вариант, локальная модель."""

import json
import math
import re
from collections import Counter


def words(text):
    return re.findall(r"[a-zа-яё0-9]+", text.lower().replace("ё", "е"))


def card_text(card):
    return " ".join([card["topic"], card["title"], " ".join(json.loads(card["question_variants"])), card["conditions"], card["answer"]])


class FullSearch:
    name = "full"

    def select(self, query, cards, limit=8):
        return cards


class LexicalSearch:
    name = "lexical"

    def select(self, query, cards, limit=8):
        terms = set(words(query))
        def score(card):
            tokens = Counter(words(card_text(card)))
            return sum(math.log1p(tokens[t]) for t in terms) / math.sqrt(max(1, sum(tokens.values())))
        return sorted(cards, key=score, reverse=True)[:limit]


class LocalSearch:
    name = "local"
    model_name = "intfloat/multilingual-e5-small"

    def __init__(self, db):
        self.db = db
        self.model = None

    def select(self, query, cards, limit=8):
        # Опциональная зависимость: базовый бот не загружает модель и не требует PyTorch.
        if self.model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError("Установите requirements-local.txt для локального поиска.") from exc
            self.model = SentenceTransformer(self.model_name, device="cpu")
        if not cards:
            return []
        query_vector = self.model.encode("query: " + query, normalize_embeddings=True).tolist()
        scored = []
        for card in cards:
            cached = self.db.one("SELECT * FROM embeddings WHERE card_id=? AND card_version=? AND model=?", (card["id"], card["version"], self.model_name))
            if cached:
                vector = json.loads(cached["vector"])
            else:
                vector = self.model.encode("passage: " + card_text(card), normalize_embeddings=True).tolist()
                self.db.execute("INSERT INTO embeddings VALUES (?,?,?,?) ON CONFLICT(card_id,model) DO UPDATE SET card_version=excluded.card_version,vector=excluded.vector", (card["id"], card["version"], self.model_name, json.dumps(vector)))
            scored.append((sum(a*b for a,b in zip(query_vector, vector)), card))
        semantic = [c for _, c in sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]]
        # Добавляем лексических кандидатов, чтобы не терять точные названия и номера.
        result = {c["id"]: c for c in semantic}
        result.update({c["id"]: c for c in LexicalSearch().select(query, cards, max(2, limit//2))})
        return list(result.values())


def searcher(name, db):
    if name == "full":
        return FullSearch()
    if name == "lexical":
        return LexicalSearch()
    if name == "local":
        return LocalSearch(db)
    raise ValueError("Неизвестный способ поиска.")
