"""데이터셋 검증과 JSON/JSONL/CSV 공통 포맷 변환.

정답(label)은 저장/채점에만 쓰며 inference_sample()에서 명시적으로 제거합니다.
미디어는 업로드로 발급된 asset_id로만 참조하여 임의 파일 경로를 읽지 않습니다.
"""
import csv
import hashlib
import io
import json
from collections import Counter

from .config import MAX_SAMPLES
from .storage import dumps, new_id, now


def parse_dataset(text, filename):
    text = text.lstrip("\ufeff")
    if filename.lower().endswith(".csv"):
        rows = list(csv.DictReader(io.StringIO(text)))
        for row in rows:
            for key in ("choices", "assets", "tags"):
                if row.get(key):
                    row[key] = json.loads(row[key])
        return {"samples": rows}
    if filename.lower().endswith((".jsonl", ".ndjson")):
        return {"samples": [json.loads(line) for line in text.splitlines() if line.strip()]}
    value = json.loads(text)
    return {"samples": value} if isinstance(value, list) else value


def validate_dataset(payload, store):
    """잘못된 정답과 누락된 미디어를 실행 전에 발견합니다. 자동 잘림은 하지 않습니다."""
    if not isinstance(payload, dict):
        raise ValueError("데이터셋은 객체 또는 샘플 배열이어야 합니다.")
    source = payload.get("samples", [])
    if not isinstance(source, list) or not 1 <= len(source) <= MAX_SAMPLES:
        raise ValueError("샘플은 1~10,000개여야 합니다.")
    samples, seen = [], set()
    assets_by_name = {}
    for asset in store.list("assets"):
        assets_by_name.setdefault(asset["name"], []).append(asset["id"])
    for index, item in enumerate(source):
        if not isinstance(item, dict):
            raise ValueError("각 샘플은 JSON 객체여야 합니다.")
        sample_id = str(item.get("id") or "sample-%05d" % (index + 1))
        if sample_id in seen:
            raise ValueError("중복 샘플 ID: " + sample_id)
        seen.add(sample_id)
        question = str(item.get("question") or "").strip()
        state = str(item.get("text") or "")
        if not question or len(question) > 10000 or len(state) > 100000:
            raise ValueError(sample_id + ": 질문 누락 또는 입력 길이 초과")
        choices = item.get("choices")
        if isinstance(choices, dict):
            choices = [{"id": str(k), "label": str(v)} for k, v in choices.items()]
        if isinstance(choices, list):
            choices = [{"id": str(c.get("id", i)), "label": str(c.get("label", ""))} if isinstance(c, dict)
                       else {"id": str(i), "label": str(c)} for i, c in enumerate(choices)]
        if not isinstance(choices, list) or not 2 <= len(choices) <= 20:
            raise ValueError(sample_id + ": 선택지는 2~20개여야 합니다.")
        ids = [c["id"] for c in choices]
        if len(set(ids)) != len(ids) or any(not c["id"] or not c["label"] or len(c["label"]) > 5000 for c in choices):
            raise ValueError(sample_id + ": 선택지 ID 중복 또는 빈 선택지")
        label = item.get("label")
        label = None if label in (None, "") else str(label)
        if label is not None and label not in ids:
            raise ValueError(sample_id + ": label은 선택지 ID와 같아야 합니다.")
        refs = item.get("assets") or []
        if not isinstance(refs, list) or len(refs) > 8:
            raise ValueError(sample_id + ": assets는 최대 8개 ID의 배열이어야 합니다.")
        # 변환 도구는 파일 이름을 남깁니다. 이름이 유일할 때만 업로드 파일과 연결합니다.
        if item.get("media_name"):
            candidates = assets_by_name.get(str(item["media_name"]), [])
            if len(candidates) != 1:
                raise ValueError(sample_id + ": 미디어를 먼저 업로드하세요. 이름은 유일해야 합니다: " + str(item["media_name"]))
            refs = [candidates[0]]
        kinds = set()
        for asset_id in refs:
            asset = store.get("assets", str(asset_id))
            if not asset:
                raise ValueError(sample_id + ": 없는 asset_id: " + str(asset_id))
            kinds.add(asset["kind"])
        modality = "text" if not kinds else (next(iter(kinds)) if len(kinds) == 1 else "mixed")
        tags = item.get("tags") or []
        if not isinstance(tags, list):
            raise ValueError(sample_id + ": tags는 배열이어야 합니다.")
        samples.append({"id": sample_id, "text": state, "question": question, "choices": choices,
                        "label": label, "assets": [str(a) for a in refs], "modality": modality,
                        "tags": [str(t)[:80] for t in tags[:20]], "group": str(item.get("group") or sample_id)})
    return {"id": new_id("ds"), "name": str(payload.get("name") or "새 데이터셋")[:100],
            "description": str(payload.get("description") or "")[:1000],
            "version": str(payload.get("version") or "1"), "split": str(payload.get("split") or "custom"),
            "source": str(payload.get("source") or "사용자 업로드"), "created_at": now(),
            "samples": samples, "counts": dict(Counter(s["modality"] for s in samples)),
            "sha256": hashlib.sha256(dumps(samples).encode()).hexdigest()}


def inference_sample(sample):
    """화이트리스트 방식으로 정답·태그·그룹 정보가 모델 입력에 섞이는 것을 방지합니다."""
    return {key: sample[key] for key in ("id", "text", "question", "choices", "assets", "modality")}


def seed_demo(store):
    if store.get("datasets", "demo-dataset"):
        return
    examples = [
        ("문을 잠가 주세요.", "lock"), ("목적지를 집으로 바꿔 주세요.", "navigation"),
        ("차량의 문을 모두 잠가.", "lock"), ("가까운 충전소로 안내해 줘.", "navigation"),
        ("라디오 소리를 줄여 주세요.", "audio"), ("음악 재생을 멈춰.", "audio"),
    ]
    choices = {"lock": "문 잠금", "navigation": "목적지 변경", "audio": "오디오 제어"}
    data = validate_dataset({"name": "차량 음성 명령 · 텍스트 예제", "split": "demo",
                             "description": "직접 작성한 6개 문장입니다. 공개 벤치마크가 아닙니다.",
                             "samples": [{"id": "intent-%02d" % (i + 1), "text": text,
                                          "question": "사용자가 요청한 기능을 선택하세요.", "choices": choices,
                                          "label": label, "tags": ["한국어", "차량"]}
                                         for i, (text, label) in enumerate(examples)]}, store)
    data["id"] = "demo-dataset"
    store.put("datasets", data)
