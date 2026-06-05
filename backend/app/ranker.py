from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import get_settings

# Stable feature order for train/inference compatibility.
FEATURE_KEYS = [
    "base_score",
    "source_weight",
    "affinity_score",
    "similarity_score",
    "popularity_score",
    "recency_bonus",
    "penalties",
    "product_price",
    "product_price_bucket",
    "product_is_featured",
    "same_category_as_current",
    "same_brand_as_current",
    "tag_overlap",
    "actor_view_count",
    "actor_purchase_count",
    "product_view_7d",
    "product_purchase_30d",
    "context_home",
    "context_pdp",
    "context_cart",
    "context_account",
    "is_anonymous",
]


class RankerArtifacts:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.root = Path(self.settings.ranker_artifacts_dir).resolve()
        self.model_path = self.root / self.settings.ranker_model_filename
        self.meta_path = self.root / self.settings.ranker_meta_filename
        self.dataset_path = self.root / self.settings.ranker_dataset_filename

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def write_dataset(self, rows: list[dict[str, Any]]) -> Path:
        self.ensure_root()
        with self.dataset_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return self.dataset_path

    def read_dataset(self) -> list[dict[str, Any]]:
        if not self.dataset_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.dataset_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
        return rows

    def write_meta(self, meta: dict[str, Any]) -> None:
        self.ensure_root()
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def read_meta(self) -> dict[str, Any]:
        if not self.meta_path.exists():
            return {
                "is_ready": False,
                "model_version": None,
                "trained_at": None,
                "rows": 0,
                "features": FEATURE_KEYS,
                "fallback_reason": "model_not_trained",
            }
        try:
            payload = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {
                "is_ready": False,
                "model_version": None,
                "trained_at": None,
                "rows": 0,
                "features": FEATURE_KEYS,
                "fallback_reason": "invalid_meta_json",
            }
        if not isinstance(payload, dict):
            return {
                "is_ready": False,
                "model_version": None,
                "trained_at": None,
                "rows": 0,
                "features": FEATURE_KEYS,
                "fallback_reason": "invalid_meta_payload",
            }
        payload.setdefault("features", FEATURE_KEYS)
        payload.setdefault("rows", 0)
        payload.setdefault("is_ready", False)
        return payload

    def train(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        self.ensure_root()
        trained_at = datetime.utcnow().isoformat()
        model_version = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        min_rows = int(self.settings.ranker_min_rows)

        if len(rows) < min_rows:
            meta = {
                "is_ready": False,
                "model_version": model_version,
                "trained_at": trained_at,
                "rows": len(rows),
                "features": FEATURE_KEYS,
                "fallback_reason": f"insufficient_rows:{len(rows)}<{min_rows}",
                "model_path": str(self.model_path),
                "dataset_path": str(self.dataset_path),
            }
            self.write_meta(meta)
            return meta

        try:
            import lightgbm as lgb
            import numpy as np
        except Exception as exc:
            meta = {
                "is_ready": False,
                "model_version": model_version,
                "trained_at": trained_at,
                "rows": len(rows),
                "features": FEATURE_KEYS,
                "fallback_reason": f"lightgbm_unavailable:{exc.__class__.__name__}",
                "model_path": str(self.model_path),
                "dataset_path": str(self.dataset_path),
            }
            self.write_meta(meta)
            return meta

        x = np.array(
            [
                [float(row.get(feature, 0.0) or 0.0) for feature in FEATURE_KEYS]
                for row in rows
            ],
            dtype=float,
        )
        y = np.array([float(row.get("label", 0.0) or 0.0) for row in rows], dtype=float)

        try:
            train_dataset = lgb.Dataset(x, label=y, feature_name=FEATURE_KEYS)
            booster = lgb.train(
                {
                    "objective": "regression",
                    "metric": "rmse",
                    "verbosity": -1,
                    "learning_rate": 0.05,
                    "num_leaves": 31,
                    "min_data_in_leaf": 5,
                    "feature_fraction": 0.9,
                    "bagging_fraction": 0.9,
                    "bagging_freq": 1,
                    "seed": 42,
                },
                train_dataset,
                num_boost_round=80,
            )
            booster.save_model(str(self.model_path))
            split_idx = max(1, int(len(rows) * 0.8))
            eval_x = x[split_idx:] if split_idx < len(rows) else x
            eval_y = y[split_idx:] if split_idx < len(rows) else y
            preds_train = booster.predict(x)
            preds_eval = booster.predict(eval_x) if len(eval_x) else preds_train
            train_rmse = float(np.sqrt(np.mean((preds_train - y) ** 2))) if len(y) else None
            validation_rmse = float(np.sqrt(np.mean((preds_eval - eval_y) ** 2))) if len(eval_y) else None
            ndcg_at_10 = None
            if len(eval_y):
                order = np.argsort(-preds_eval)
                k = min(10, len(order))
                ranked_labels = eval_y[order][:k]
                discounts = 1.0 / np.log2(np.arange(2, k + 2))
                dcg = float(np.sum(ranked_labels * discounts))
                ideal_labels = np.sort(eval_y)[::-1][:k]
                idcg = float(np.sum(ideal_labels * discounts))
                ndcg_at_10 = float(dcg / idcg) if idcg > 1e-9 else 0.0
            meta = {
                "is_ready": True,
                "model_version": model_version,
                "trained_at": trained_at,
                "rows": len(rows),
                "features": FEATURE_KEYS,
                "train_rmse": train_rmse,
                "validation_rmse": validation_rmse,
                "validation_ndcg_at_10": ndcg_at_10,
                "fallback_reason": None,
                "model_path": str(self.model_path),
                "dataset_path": str(self.dataset_path),
            }
            self.write_meta(meta)
            return meta
        except Exception as exc:
            meta = {
                "is_ready": False,
                "model_version": model_version,
                "trained_at": trained_at,
                "rows": len(rows),
                "features": FEATURE_KEYS,
                "fallback_reason": f"train_failed:{exc.__class__.__name__}",
                "model_path": str(self.model_path),
                "dataset_path": str(self.dataset_path),
            }
            self.write_meta(meta)
            return meta

    def predict(self, feature_rows: list[dict[str, Any]]) -> tuple[dict[int, float], str | None]:
        meta = self.read_meta()
        if not feature_rows:
            return {}, "empty_candidates"

        if not meta.get("is_ready"):
            return {}, str(meta.get("fallback_reason") or "model_not_ready")
        if not self.model_path.exists():
            return {}, "model_file_missing"

        try:
            import lightgbm as lgb
            import numpy as np
        except Exception as exc:
            return {}, f"lightgbm_unavailable:{exc.__class__.__name__}"

        features = meta.get("features") or FEATURE_KEYS
        try:
            matrix = np.array(
                [
                    [float(row.get(feature, 0.0) or 0.0) for feature in features]
                    for row in feature_rows
                ],
                dtype=float,
            )
            booster = lgb.Booster(model_file=str(self.model_path))
            predictions = booster.predict(matrix)
        except Exception as exc:
            return {}, f"predict_failed:{exc.__class__.__name__}"

        by_product: dict[int, float] = {}
        for row, value in zip(feature_rows, predictions):
            product_id = int(row["product_id"])
            by_product[product_id] = float(value)
        return by_product, None
