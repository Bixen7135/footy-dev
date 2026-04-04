from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Optional


TRAILING_IMAGE_SLASH_RE = re.compile(
    r"(\.(?:jpe?g|png|webp|gif|bmp|avif))(?:/+)(?=$|[?#])",
    flags=re.IGNORECASE,
)
ASSET_CODE_RE = re.compile(r"/load/([^/]+)/", flags=re.IGNORECASE)
ASSET_SIGNATURE_RE = re.compile(r"/load/([^/]+)/([^/]+)/([^/?#]+)$", flags=re.IGNORECASE)
SIZE_PRIORITY = {"big": 4, "medium": 3, "small": 2, "smallest": 1}


@dataclass(frozen=True)
class GalleryImageCandidate:
    url: str
    sort_order: int = 0
    is_primary: bool = False
    payload: Any = None


def normalize_image_url(url: Optional[str]) -> str:
    if not url:
        return ""
    cleaned = url.strip()
    if not cleaned:
        return ""
    return TRAILING_IMAGE_SLASH_RE.sub(r"\1", cleaned)


def _url_path(url: str) -> str:
    return url.split("#", 1)[0].split("?", 1)[0]


def extract_asset_code(url: str) -> Optional[str]:
    match = ASSET_CODE_RE.search(_url_path(url))
    if not match:
        return None
    return match.group(1).strip().lower() or None


def _signature_with_priority(url: str) -> tuple[str, int]:
    path = _url_path(url)
    match = ASSET_SIGNATURE_RE.search(path)
    if not match:
        return url.strip().lower(), 0
    asset_code = match.group(1).strip().lower()
    size_token = match.group(2).strip().lower()
    filename = match.group(3).strip().lower()
    signature = f"{asset_code}/{filename}"
    return signature, SIZE_PRIORITY.get(size_token, 0)


def sanitize_gallery_candidates(
    candidates: Iterable[GalleryImageCandidate],
    limit: int = 30,
) -> list[GalleryImageCandidate]:
    if limit <= 0:
        return []

    prepared: list[tuple[int, GalleryImageCandidate]] = []
    for index, candidate in enumerate(candidates):
        normalized_url = normalize_image_url(candidate.url)
        if not normalized_url:
            continue
        prepared.append(
            (
                index,
                GalleryImageCandidate(
                    url=normalized_url,
                    sort_order=int(candidate.sort_order),
                    is_primary=bool(candidate.is_primary),
                    payload=candidate.payload,
                ),
            )
        )

    if not prepared:
        return []

    prepared.sort(key=lambda pair: (0 if pair[1].is_primary else 1, pair[1].sort_order, pair[0]))

    primary_asset_code: Optional[str] = None
    for _, candidate in prepared:
        primary_asset_code = extract_asset_code(candidate.url)
        if primary_asset_code:
            break

    if primary_asset_code:
        prepared = [
            pair
            for pair in prepared
            if (extract_asset_code(pair[1].url) in {None, primary_asset_code})
        ]
        if not prepared:
            return []

    best_by_signature: dict[str, tuple[tuple[int, int, int, int], GalleryImageCandidate]] = {}
    signature_order: list[str] = []
    for index, candidate in prepared:
        signature, size_priority = _signature_with_priority(candidate.url)
        quality = (
            size_priority,
            1 if candidate.is_primary else 0,
            -candidate.sort_order,
            -index,
        )
        current = best_by_signature.get(signature)
        if current is None:
            best_by_signature[signature] = (quality, candidate)
            signature_order.append(signature)
            continue
        if quality > current[0]:
            best_by_signature[signature] = (quality, candidate)

    sanitized = [best_by_signature[signature][1] for signature in signature_order]
    return sanitized[:limit]


def sanitize_gallery_urls(urls: Iterable[str], limit: int = 30) -> list[str]:
    candidates = [GalleryImageCandidate(url=url, sort_order=index) for index, url in enumerate(urls)]
    return [candidate.url for candidate in sanitize_gallery_candidates(candidates, limit=limit)]
