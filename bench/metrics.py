"""평가 지표 계산. 품질은 첫 반복만, 시간과 사용량은 모든 측정 반복을 사용합니다.

반복 요청을 독립적인 정답 표본으로 부풀리지 않습니다. 분모와 관측 수를
결과에 함께 넣고, 미측정 값·빈 집합을 0으로 표시하지 않습니다.
"""
import math
from collections import defaultdict


def mean(values):
    values = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    return sum(values) / len(values) if values else None


def percentile(values, q):
    values = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not values:
        return None
    position = (len(values) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def quality(rows):
    """선택지 ID 의미가 같은 분류 데이터에서 macro-F1을 사용하세요."""
    labeled = [r for r in rows if r.get("label") is not None]
    valid = [r for r in labeled if r["status"] == "success"]
    eligible = [r for r in labeled if r["status"] != "skipped"]
    correct = sum(r.get("choice") == r["label"] for r in valid)
    labels = sorted({r["label"] for r in valid} | {r["choice"] for r in valid})
    matrix = {a: {b: 0 for b in labels} for a in labels}
    for r in valid:
        matrix[r["label"]][r["choice"]] += 1
    f1s = []
    for label in labels:
        tp = matrix[label][label]
        fp = sum(matrix[other][label] for other in labels if other != label)
        fn = sum(matrix[label][other] for other in labels if other != label)
        f1s.append(2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0)
    probabilistic = [r for r in valid if r.get("probabilities") is not None]
    bins = [{"lower": i / 10, "upper": (i + 1) / 10, "count": 0, "confidence": None, "accuracy": None} for i in range(10)]
    brier = []
    for r in probabilistic:
        p = r["probabilities"]
        brier.append(sum((v - int(k == r["label"])) ** 2 for k, v in p.items()))
    for b in bins:
        bucket = [r for r in probabilistic if min(9, int(max(r["probabilities"].values()) * 10)) == int(round(b["lower"] * 10))]
        b["count"] = len(bucket)
        b["confidence"] = mean([max(r["probabilities"].values()) for r in bucket])
        b["accuracy"] = mean([int(r["choice"] == r["label"]) for r in bucket])
    ece = sum(b["count"] * abs(b["confidence"] - b["accuracy"]) for b in bins if b["count"]) / len(probabilistic) if probabilistic else None
    return {"accuracy": correct / len(valid) if valid else None,
            "end_to_end_accuracy": correct / len(eligible) if eligible else None,
            "macro_f1": mean(f1s), "valid_labeled": len(valid), "eligible_labeled": len(eligible),
            "correct": correct, "brier": mean(brier), "ece": ece,
            "probability_n": len(probabilistic), "calibration_bins": bins, "confusion": matrix}


def summarize(run, results):
    summaries = []
    first_by_model = {}
    for model in run["models"]:
        rows = [r for r in results if r["model_id"] == model["id"]]
        first = [r for r in rows if r["repetition"] == 0]
        first_by_model[model["id"]] = {r["sample_id"]: r for r in first}
        valid = [r for r in rows if r["status"] == "success"]
        attempted = [r for r in rows if r["status"] != "skipped"]
        active_ms = run.get("model_active_ms", {}).get(model["id"])
        prices = [r.get("cost_estimate") for r in rows if r.get("cost_estimate") is not None]
        # 실패 요청의 청구액은 알 수 없으므로 확인된 비용과 관측 수만 표시합니다.
        slices = {}
        for row in first:
            for tag in dict.fromkeys([row["modality"]] + row.get("tags", [])):
                slices.setdefault(tag, []).append(row)
        summaries.append(dict(
            quality(first), model_id=model["id"], name=model["name"], demo=model["provider"] == "demo",
            observed=len(rows), success=len(valid), errors=sum(r["status"] == "error" for r in rows),
            skipped=sum(r["status"] == "skipped" for r in rows),
            failure_rate=sum(r["status"] == "error" for r in attempted) / len(attempted) if attempted else None,
            p50_ms=percentile([r.get("total_ms") for r in valid], .5),
            p95_ms=percentile([r.get("total_ms") for r in valid], .95),
            mean_inference_ms=mean([(r.get("timings") or {}).get("inference_ms") for r in valid]),
            decisions_per_second=len(valid) * 1000 / active_ms if active_ms and not run.get("incomplete_timing") else None,
            mean_input_tokens=mean([r.get("input_tokens") for r in valid]),
            mean_output_tokens=mean([r.get("output_tokens") for r in valid]),
            token_observations=sum(r.get("input_tokens") is not None for r in valid),
            token_sources=sorted({r.get("token_source", "unknown") for r in valid}),
            measured_cost=sum(prices) if prices else None, cost_observations=len(prices),
            cost_per_1000_success=sum(prices) * 1000 / len(valid) if prices and len(prices) == len(valid) else None,
            slices={tag: quality(items) for tag, items in slices.items()},
        ))
    # 모든 모델이 같은 샘플에 성공한 교집합의 정확도를 별도로 계산합니다.
    sets = [{sid for sid, r in group.items() if r["status"] == "success" and r.get("label") is not None}
            for group in first_by_model.values()]
    common = set.intersection(*sets) if sets else set()
    for row in summaries:
        group = first_by_model[row["model_id"]]
        row["common_n"] = len(common)
        row["common_accuracy"] = mean([int(group[sid]["choice"] == group[sid]["label"]) for sid in common])
    return summaries
