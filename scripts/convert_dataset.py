"""MMBench TSV, MMAU JSON, MVBench JSON을 앱 공통 포맷으로 변환합니다.

원본은 다운로드하지 않습니다. 미디어는 별도로 준비해 업로드해야 합니다.
시간 구간을 가진 MVBench는 구간을 무시하지 않고 명시적으로 오류를 냅니다.
"""
import argparse
import base64
import csv
import json
from pathlib import Path


def convert(kind, source, out):
    out.mkdir(parents=True, exist_ok=True)
    media = out / "media"
    media.mkdir(exist_ok=True)
    if kind == "mmbench":
        with source.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        by_id = {str(row.get("index")): row for row in rows}
    else:
        rows = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(rows, list):
            raise ValueError("JSON 최상위 값은 배열이어야 합니다.")
        by_id = {}
    samples = []
    for i, row in enumerate(rows):
        identity = str(row.get("id", row.get("index", i)))
        name = ""
        if kind == "mmbench":
            choices = {letter: str(row[letter]) for letter in "ABCDEFGHIJKLMNOPQRST" if row.get(letter) not in (None, "", "nan")}
            answer = row.get("answer") or None
            encoded, seen = row.get("image", ""), set()
            while encoded in by_id:
                if encoded in seen:
                    raise ValueError("이미지 참조가 순환합니다.")
                seen.add(encoded)
                encoded = by_id[encoded].get("image", "")
            if encoded:
                raw = base64.b64decode(encoded, validate=True)
                name = "mmbench-%06d%s" % (i, ".png" if raw.startswith(b"\x89PNG") else ".jpg")
                (media / name).write_bytes(raw)
            text = row.get("hint") or ""
        else:
            options = row.get("choices", row.get("candidates"))
            if not isinstance(options, list) or len(options) < 2:
                raise ValueError("행 %d: choices/candidates 배열이 필요합니다." % i)
            choices = {str(n): str(value) for n, value in enumerate(options)}
            answer_value = row.get("answer")
            if answer_value is None:
                answer = None
            elif answer_value in options:
                answer = str(options.index(answer_value))
            else:
                raise ValueError("행 %d: 정답 문자열이 선택지에 없습니다. 원본 스키마를 확인하세요." % i)
            filename = row.get("audio_id") if kind == "mmau" else row.get("video")
            if not filename:
                raise ValueError("행 %d: 미디어 파일 이름이 없습니다." % i)
            name = Path(str(filename).replace("\\", "/")).name
            text = ""
            if kind == "mvbench" and (row.get("start") is not None or row.get("end") is not None):
                raise ValueError("시간 구간이 있습니다. start/end에 맞게 영상을 자르고 별도 파일로 준비하세요.")
        samples.append({"id": identity, "text": text, "question": row["question"], "choices": choices,
                        "label": answer, "media_name": name,
                        "tags": [str(row[k]) for k in ("category", "subcategory", "task", "difficulty") if row.get(k)]})
    result = {"name": kind.upper() + " · 로컬 변환", "source": str(source.name),
              "split": "원본 분할을 기입하세요", "version": "1", "samples": samples}
    path = out / "dataset.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["mmbench", "mmau", "mvbench"], required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(convert(args.format, args.input, args.output))
