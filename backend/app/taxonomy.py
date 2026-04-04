from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CanonicalCategory:
    key: str
    slug: str
    name: str


@dataclass(frozen=True)
class CategoryResolution:
    key: str
    slug: str
    name: str
    matched: bool
    source_leaf: str


CANONICAL_CATEGORIES: tuple[CanonicalCategory, ...] = (
    CanonicalCategory(key="sneakers", slug="krossovki", name="Кроссовки"),
    CanonicalCategory(key="keds", slug="kedy", name="Кеды"),
    CanonicalCategory(key="dress_loafers", slug="tufli-i-lofery", name="Туфли и лоферы"),
    CanonicalCategory(key="slipons_moccasins", slug="slipony-i-mokasiny", name="Слипоны и мокасины"),
    CanonicalCategory(key="polubotinki", slug="polubotinki", name="Полуботинки"),
    CanonicalCategory(key="sandals", slug="sandalii", name="Сандалии"),
    CanonicalCategory(key="bosonozhki", slug="bosonozhki", name="Босоножки"),
    CanonicalCategory(key="slides", slug="shlyopancy", name="Шлёпанцы"),
    CanonicalCategory(key="boots", slug="botinki-i-sapogi", name="Ботинки и сапоги"),
    CanonicalCategory(key="slippers", slug="tapki", name="Тапки"),
)

CANONICAL_CATEGORY_BY_KEY: dict[str, CanonicalCategory] = {category.key: category for category in CANONICAL_CATEGORIES}
CANONICAL_CATEGORY_NAMES_ORDER: tuple[str, ...] = tuple(category.name for category in CANONICAL_CATEGORIES)
LEGACY_TECHNICAL_CATEGORY_SLUGS = {"shoes", "casual-sneakers", "sneakers-low"}

GENDER_LABELS: dict[str, str] = {
    "men": "Мужской",
    "women": "Женский",
}

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_TEXT_SEPARATORS_RE = re.compile(r"[>/|\\»]+")
_SPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^0-9a-zа-яё\s-]+")

# Ordered by specificity.
_CATEGORY_MATCHERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("slippers", ("тапк", "домашн", "slipper home")),
    ("polubotinki", ("полубот",)),
    ("bosonozhki", ("босонож",)),
    ("sandals", ("сандал", "sandal")),
    ("slipons_moccasins", ("слипон", "мокасин", "slipon", "slip-on", "moccasin")),
    (
        "dress_loafers",
        (
            "туфл",
            "лофер",
            "балетк",
            "оксфорд",
            "эспадрил",
            "дерби",
            "брог",
            "loaf",
            "pump",
            "oxford",
            "espadril",
            "derby",
            "brogue",
            "ballet",
        ),
    ),
    ("slides", ("шлеп", "шлёп", "вьетнам", "сабо", "мюл", "slide", "flip flop", "flipflop", "pool slipper")),
    ("boots", ("ботинк", "сапог", "ботиль", "тильон", "челси", "дутик", "ботфорт", "boot")),
    ("keds", ("кед", "ked", "keds")),
    ("sneakers", ("кроссов", "бутсы", "sneaker", "trainer", "running", "sport", "shoe")),
)


def _strip_markdown_links(value: str) -> str:
    return _MARKDOWN_LINK_RE.sub(r"\1", value)


def _normalize_text(value: str) -> str:
    text = _strip_markdown_links(value.strip())
    text = text.lower()
    text = text.replace("_", " ").replace("-", " ")
    text = _NON_WORD_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text


def _clean_path_part(value: str) -> str:
    cleaned = _normalize_text(value)
    return cleaned


def parse_source_category_path(source_category_path: Optional[str]) -> list[str]:
    raw = (source_category_path or "").strip()
    if not raw:
        return []

    parsed: object
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw

    if isinstance(parsed, list):
        return [part for item in parsed if (part := _clean_path_part(str(item)))]

    if isinstance(parsed, str):
        value = parsed.strip()
    else:
        value = raw

    parts = [part for part in _TEXT_SEPARATORS_RE.split(value) if part and part.strip()]
    if not parts:
        return []
    return [part for part in (_clean_path_part(item) for item in parts) if part]


def normalize_gender_key(raw_gender: Optional[str]) -> Optional[str]:
    raw = _normalize_text(raw_gender or "")
    if not raw:
        return None

    collapsed = raw.replace(" ", "")
    if (
        collapsed.startswith("men")
        or collapsed.startswith("male")
        or collapsed.startswith("man")
        or "муж" in collapsed
        or "парн" in collapsed
    ):
        return "men"
    if (
        collapsed.startswith("women")
        or collapsed.startswith("woman")
        or collapsed.startswith("female")
        or "жен" in collapsed
        or "дев" in collapsed
    ):
        return "women"
    return None


def resolve_gender_key(root_gender: Optional[str], source_category_path: Optional[str] = None) -> Optional[str]:
    normalized = normalize_gender_key(root_gender)
    if normalized:
        return normalized

    for part in parse_source_category_path(source_category_path):
        normalized_part = normalize_gender_key(part)
        if normalized_part:
            return normalized_part
    return None


def _match_category_key(normalized_leaf: str) -> Optional[str]:
    if not normalized_leaf:
        return None
    normalized = normalized_leaf.replace("ё", "е")
    for key, tokens in _CATEGORY_MATCHERS:
        if any(token in normalized for token in tokens):
            return key
    return None


def resolve_canonical_category(
    source_category_path: Optional[str],
    fallback_text: Optional[str] = None,
) -> CategoryResolution:
    parts = parse_source_category_path(source_category_path)
    source_leaf = parts[-1] if parts else _clean_path_part(fallback_text or "")
    matched_key = _match_category_key(source_leaf)
    category_key = matched_key or "sneakers"
    category = CANONICAL_CATEGORY_BY_KEY[category_key]
    return CategoryResolution(
        key=category.key,
        slug=category.slug,
        name=category.name,
        matched=(matched_key is not None),
        source_leaf=source_leaf,
    )
