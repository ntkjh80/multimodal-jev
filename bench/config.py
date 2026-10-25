"""운영체제에 종속되지 않는 경로와 환경변수 설정.

비밀키는 .env 또는 프로세스 환경변수에만 둡니다. 모델 설정에는 키의
값 대신 환경변수 이름을 저장하므로 JSON 결과를 공유해도 키가 섞이지 않습니다.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path=None):
    """외부 패키지 없이 단순 KEY=VALUE .env를 읽습니다. 기존 환경값이 우선입니다."""
    source = Path(path or ROOT / ".env")
    if not source.exists():
        return
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and key.replace("_", "").isalnum() and not key[0].isdigit():
            os.environ.setdefault(key, value.strip("\"'"))


def data_dir():
    """테스트에서는 MMBENCH_DATA_DIR로 임시 폴더를 주입할 수 있습니다."""
    return Path(os.environ.get("MMBENCH_DATA_DIR", str(ROOT / "data"))).resolve()


MAX_ASSET_BYTES = 20 * 1024 * 1024
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_SAMPLES = 10000


def default_models():
    """가격은 사용자가 확인해서 입력합니다. 미입력 가격을 0원으로 처리하지 않습니다."""
    common = {"revision": "", "input_price": None, "output_price": None,
              "cached_input_price": None, "price_date": "", "timeout": 120}
    entries = [
        {"id": "demo", "name": "Demo · 동작 확인용", "provider": "demo", "model": "demo-v1",
         "endpoint": "", "key_env": "", "modalities": ["text", "image", "audio", "video"],
         "description": "해시 기반 모의 응답입니다. 실제 AI 성능이나 실제 토큰 사용량이 아닙니다."},
        {"id": "jev", "name": "Jev 1.14", "provider": "typesafe", "model": "jev-1.14.0",
         "endpoint": "https://api.typesafe.ai/v1/systemone", "key_env": "TYPESAFE_API_KEY",
         "modalities": ["text", "image"], "description": "TypeSafe API · 이미지 입력은 실험 기능"},
        {"id": "clef-omni", "name": "Clef-omni", "provider": "cloudflare", "model": "clef-omni",
         "endpoint": "", "key_env": "CLOUDFLARE_API_TOKEN", "modalities": ["text", "image", "audio", "video"],
         "description": "Cloudflare 계정 ID를 .env에 설정하거나 전체 API 주소를 입력하세요."},
        {"id": "clef-flash", "name": "Clef-flash", "provider": "cloudflare", "model": "clef-flash",
         "endpoint": "", "key_env": "CLOUDFLARE_API_TOKEN", "modalities": ["text", "image"],
         "description": "작은 판단 모델 · 이 앱의 직접 연결은 텍스트/이미지 입력"},
        {"id": "jev-omni", "name": "Jev-Omni · GPU worker", "provider": "systemone", "model": "jev-omni",
         "endpoint": "http://127.0.0.1:9010/v1/systemone", "key_env": "",
         "modalities": ["text", "image", "audio", "video"],
         "description": "동봉한 선택형 CUDA worker 연결 · 영상은 무음 프레임으로 처리"},
        {"id": "ollama", "name": "Ollama · 로컬 기준 모델", "provider": "ollama", "model": "",
         "endpoint": "http://127.0.0.1:11434/api/chat", "key_env": "",
         "modalities": ["text", "image"], "description": "설치한 모델 이름을 입력하세요. 이미지 지원 모델을 선택해야 합니다."},
    ]
    return [dict(common, **entry) for entry in entries]
