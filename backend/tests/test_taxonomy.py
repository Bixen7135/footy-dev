import json

from app.taxonomy import normalize_gender_key, resolve_canonical_category, resolve_gender_key


def test_resolve_canonical_category_with_brand_prefix_and_noise():
    path = json.dumps(["women", "shoes", "Hilfiger Кроссовки повседневные"], ensure_ascii=False)
    resolved = resolve_canonical_category(path)
    assert resolved.key == "sneakers"
    assert resolved.matched is True


def test_resolve_canonical_category_fallback_to_sneakers():
    path = json.dumps(["women", "shoes", "Timberland"], ensure_ascii=False)
    resolved = resolve_canonical_category(path)
    assert resolved.key == "sneakers"
    assert resolved.matched is False


def test_gender_normalization_aliases():
    assert normalize_gender_key("Мужской") == "men"
    assert normalize_gender_key("female") == "women"
    assert resolve_gender_key(None, json.dumps(["women", "shoes", "Кеды"], ensure_ascii=False)) == "women"
