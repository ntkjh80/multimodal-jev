"""Jev-Omni 체크포인트를 로컬 SystemOne HTTP 서버로 연결하는 선택형 GPU worker.

웹앱과 별도 환경에서 실행합니다. 실행 시 --revision에 검토한 체크포인트
커밋을 명시해야 하며, 최초 실행은 모델을 다운로드합니다. 기본 웹앱은
이 파일이나 CUDA 패키지를 import하지 않습니다.
"""
import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def load_local_checkpoint(snapshot):
    """이미 내려받은 고정 revision만 읽고 공식 결정 헤드/백본 구성을 재사용합니다.

    공식 load_jev_omni는 내부에서 다시 snapshot_download를 호출하므로 로컬
    경로를 모델 ID로 넘길 수 없습니다. 여기서는 원격 다운로드를 반복하지 않고
    고정한 디렉터리의 설정·가중치·전처리기를 명시적으로 조립합니다.
    """
    import torch
    import transformers
    from jev_omni import JevOmni, _Head256, _find_backbone

    path = Path(snapshot)
    config = transformers.AutoConfig.from_pretrained(path, local_files_only=True)
    model_class = getattr(transformers, config.architectures[0])
    model = model_class.from_pretrained(path, dtype=torch.bfloat16, device_map="cuda",
                                       local_files_only=True).eval()
    decision = json.loads((path / "decision_config.json").read_text())
    head = _Head256(decision["hidden_size"]).to("cuda").eval()
    head.load_state_dict(torch.load(path / "head.pt", map_location="cuda", weights_only=True))
    processor = transformers.AutoProcessor.from_pretrained(path, local_files_only=True)
    return JevOmni(model, head, processor, _find_backbone(model)[1])


def classify(classifier, state, question, labels, mode, path, frame_count, manifest):
    """입력 변환을 명시해 실제 디코딩 프레임과 전처리 조건을 기록합니다.

    생성 루프를 실행하지 않습니다. 공식 decision head에 마지막 은닉 상태를
    한 번 전달하고 후보 수 밖의 슬롯을 마스킹한 뒤 확률을 계산합니다.
    """
    import torch
    from PIL import Image
    from jev_omni import _prompt

    content = []
    if mode == "image":
        with Image.open(path) as picture:
            content.append({"type": "image", "image": picture.convert("RGB")})
    elif mode == "video":
        import cv2
        cap = cv2.VideoCapture(str(path))
        try:
            total, fps = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), cap.get(cv2.CAP_PROP_FPS)
            if total < 1:
                raise ValueError("영상 프레임 수를 읽을 수 없습니다.")
            wanted = sorted({round((total - 1) * (k + .5) / frame_count) for k in range(frame_count)})
            decoded, current = [], 0
            for index in wanted:
                while current < index and cap.grab():
                    current += 1
                ok, frame = cap.read()
                if not ok or current != index:
                    # 일부 프레임만 성공한 영상을 조용히 채점하지 않습니다.
                    raise ValueError("선택한 영상 프레임을 모두 디코딩하지 못했습니다.")
                decoded.append(index)
                current += 1
                content.append({"type": "image", "image": Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))})
            manifest.update(video_frames=len(decoded), requested_video_frames=frame_count,
                            frame_indices=decoded, frame_seconds=[x / fps for x in decoded] if fps > 0 else None)
        finally:
            cap.release()
    elif mode == "audio":
        converted = path.parent / "converted.wav"
        # -y는 이 요청의 임시 폴더 안에서만 적용합니다. 대화형 덮어쓰기 질문을 방지합니다.
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(path),
                        "-ac", "1", "-ar", "16000", str(converted)], check=True, timeout=60,
                       capture_output=True)
        content.append({"type": "audio", "audio": str(converted)})
    content.append({"type": "text", "text": _prompt(state, question, labels)})
    inputs = classifier.processor.apply_chat_template(
        [{"role": "user", "content": content}], add_generation_prompt=True, tokenize=True,
        return_dict=True, return_tensors="pt", enable_thinking=False)
    inputs = {key: value.to("cuda", dtype=torch.bfloat16) if torch.is_floating_point(value)
              else value.to("cuda") for key, value in inputs.items()}
    classifier._capture.clear()
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
        classifier.model(**inputs, use_cache=False, **classifier._extra)
        logits = classifier.head(classifier._capture["hidden"], torch.tensor([len(labels)], device="cuda"))
        values = logits[0, :len(labels)].softmax(-1).float().cpu().tolist()
    return values


class MeteredProcessor:
    """공식 전처리기 호출 후 패딩 제외 시퀀스 길이와 전처리 시간을 관측합니다."""
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.tokens = None
        self.elapsed_ms = None

    def __getattr__(self, key):
        return getattr(self.wrapped, key)

    def apply_chat_template(self, *args, **kwargs):
        start = time.perf_counter()
        result = self.wrapped.apply_chat_template(*args, **kwargs)
        self.elapsed_ms = (time.perf_counter() - start) * 1000
        mask = result.get("attention_mask")
        self.tokens = int(mask.sum().item()) if mask is not None else int(result["input_ids"].shape[-1])
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True, help="검토한 Hugging Face 40자리 커밋 SHA")
    parser.add_argument("--port", type=int, default=9010)
    parser.add_argument("--video-frames", type=int, default=16)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-fA-F0-9]{40}", args.revision):
        parser.error("--revision은 40자리 커밋 SHA여야 합니다.")
    if not 1 <= args.video_frames <= 64:
        parser.error("--video-frames는 1~64입니다.")
    # 모델 저장소의 Python 코드를 실행하므로 revision을 고정한 뒤 사용합니다.
    import torch
    from huggingface_hub import snapshot_download
    if not torch.cuda.is_available():
        parser.error("이 worker에는 CUDA GPU가 필요합니다. 기본 웹앱은 GPU 없이 실행됩니다.")
    snapshot = snapshot_download("akhilaaa3/Jev-Omni", revision=args.revision)
    sys.path.insert(0, snapshot)
    classifier = load_local_checkpoint(snapshot)
    meter = MeteredProcessor(classifier.processor)
    classifier.processor = meter
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def reply(self, value, status=200):
            data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            # 브라우저의 직접 교차 출처 호출은 거부합니다. 웹앱 백엔드가 호출합니다.
            if self.headers.get("Origin") or self.path != "/v1/systemone":
                return self.reply({"error": "허용되지 않는 요청"}, 403)
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 32 * 1024 * 1024:
                    raise ValueError("요청 크기 제한")
                body = json.loads(self.rfile.read(length))
                questions = body["questions"]
                if set(questions) != {"q"} or questions["q"].get("type") != "choice":
                    raise ValueError("이 worker는 요청당 choice 질문 하나를 처리합니다.")
                q = questions["q"]
                ids, labels = list(q["criteria"]), list(q["criteria"].values())
                if not 2 <= len(labels) <= 20 or len(set(labels)) != len(labels):
                    raise ValueError("서로 다른 선택지 설명 2~20개를 사용하세요.")
                entries = [(kind, item) for field, kind in (("images", "image"), ("audio", "audio"), ("videos", "video"))
                           for item in body.get(field, [])]
                if len(entries) > 1:
                    raise ValueError("이 worker는 미디어 파일 하나를 처리합니다.")
                with tempfile.TemporaryDirectory(prefix="jev-omni-") as tmp:
                    mode, path = "text", None
                    manifest = {"revision": args.revision, "video_audio_used": False}
                    if entries:
                        mode, uri = entries[0]
                        match = re.fullmatch(r"data:([a-z0-9.+/-]+);base64,([A-Za-z0-9+/=\r\n]+)", uri)
                        if not match:
                            raise ValueError("base64 data URI만 허용합니다.")
                        raw = base64.b64decode(match.group(2), validate=True)
                        path = Path(tmp) / "input"
                        path.write_bytes(raw)
                        if mode == "audio":
                            # 공식 로더가 앞 30초만 쓰므로 초과 입력은 평가 전에 거부합니다.
                            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                                    "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                                                   check=True, capture_output=True, text=True, timeout=30)
                            duration = float(probe.stdout.strip())
                            if duration > 30:
                                raise ValueError("30초 초과 오디오는 먼저 정답 구간에 맞게 분할하세요.")
                            manifest.update(audio_seconds=duration, audio_sample_rate=16000, audio_channels=1)
                    # GPU는 비동기 실행이므로 synchronize 전후의 시간을 측정합니다.
                    queued = time.perf_counter()
                    with lock:
                        queue_ms = (time.perf_counter() - queued) * 1000
                        meter.tokens, meter.elapsed_ms = None, None
                        torch.cuda.reset_peak_memory_stats()
                        torch.cuda.synchronize()
                        start = time.perf_counter()
                        values = classify(classifier, str(body["state"]), q["instructions"],
                                          labels, mode, path, args.video_frames, manifest)
                        torch.cuda.synchronize()
                        elapsed = (time.perf_counter() - start) * 1000
                        answer = {"type": "choice", "choice": ids[max(range(len(values)), key=values.__getitem__)],
                                  "probabilities": dict(zip(ids, values)), "confidence": max(values)}
                        response = {"model": "akhilaaa3/Jev-Omni@" + args.revision, "answers": {"q": answer},
                                    "usage": {"input_tokens": meter.tokens, "output_tokens": 0, "source": "local_sequence_count"},
                                    "input_manifest": manifest,
                                    "timings": {"worker_total_ms": elapsed, "processor_ms": meter.elapsed_ms,
                                                "queue_ms": queue_ms, "peak_allocated_bytes": torch.cuda.max_memory_allocated()}}
                    self.reply(response)
            except (ValueError, KeyError, FileNotFoundError) as exc:
                self.reply({"error": str(exc)}, 400)
            except Exception:
                self.reply({"error": "worker 실행 실패: CUDA, FFmpeg, 체크포인트 의존성을 확인하세요."}, 500)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Jev-Omni worker: http://127.0.0.1:%d/v1/systemone" % args.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
