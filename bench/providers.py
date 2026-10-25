"""모델별 HTTP 규약을 공통 선택형 결과로 바꾸는 어댑터.

정답은 이 모듈에 전달되지 않습니다. 보고되지 않은 토큰·GPU 시간·확률은
None으로 남기며, JSON 문자열 길이를 실제 생성 토큰 수로 오인하지 않습니다.
"""
import base64
import hashlib
import ipaddress
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .storage import dumps

PROVIDERS = {"demo", "typesafe", "cloudflare", "systemone", "openai", "ollama"}


class ProviderError(Exception):
    """인증 정보나 원문이 포함되지 않는 사용자 표시용 오류."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("모델 API의 리다이렉트는 허용하지 않습니다. 최종 주소를 입력하세요.")


def validate_endpoint(value):
    """사용자가 직접 설정한 주소만 사용합니다. 평문 HTTP는 로컬 루프백만 허용합니다."""
    p = urllib.parse.urlsplit(value)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.fragment or p.query:
        raise ValueError("API 주소는 인증정보·쿼리가 없는 http(s) 전체 경로여야 합니다.")
    if p.scheme == "http" and p.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("원격 API에는 HTTPS를 사용하세요. HTTP는 localhost만 허용합니다.")
    try:
        address = ipaddress.ip_address(p.hostname)
    except ValueError:
        address = None
    if address and (address.is_link_local or address.is_unspecified or address.is_multicast):
        raise ValueError("허용되지 않는 API 주소입니다.")
    return value


def validate_model(payload):
    """설정에는 비밀키 자체를 받지 않습니다. 환경변수 이름만 허용합니다."""
    provider = payload.get("provider")
    if provider not in PROVIDERS:
        raise ValueError("지원하지 않는 provider입니다.")
    env = str(payload.get("key_env") or "")
    if env and not re.fullmatch(r"[A-Z][A-Z0-9_]*", env):
        raise ValueError("키 환경변수 이름은 영문 대문자·숫자·밑줄만 사용하세요.")
    modes = payload.get("modalities", ["text"])
    if not isinstance(modes, list) or not modes or not set(modes) <= {"text", "image", "audio", "video"}:
        raise ValueError("입력 형식 설정을 확인하세요.")
    if provider in ("typesafe", "ollama") and not set(modes) <= {"text", "image"}:
        raise ValueError("이 어댑터는 텍스트·이미지만 직접 지원합니다.")
    if provider == "openai" and "video" in modes:
        raise ValueError("OpenAI 호환 어댑터의 직접 영상 입력은 지원하지 않습니다.")
    endpoint = str(payload.get("endpoint") or "").strip()
    if endpoint:
        validate_endpoint(endpoint)
    if provider not in ("demo", "cloudflare") and not endpoint:
        raise ValueError("전체 API 주소를 입력하세요.")
    result = {key: str(payload.get(key) or "")[:1000] for key in
              ("id", "name", "model", "revision", "description", "price_date")}
    if not result["name"]:
        raise ValueError("모델 이름을 입력하세요.")
    result.update(provider=provider, endpoint=endpoint, key_env=env, modalities=modes,
                  timeout=max(1, min(600, int(payload.get("timeout", 120)))))
    for key in ("input_price", "output_price", "cached_input_price"):
        value = payload.get(key)
        if value in (None, ""):
            result[key] = None
        else:
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise ValueError("토큰 가격은 0 이상의 유한한 숫자여야 합니다.")
            result[key] = value
    return result


def endpoint_for(model):
    value = model.get("endpoint")
    if model["provider"] == "cloudflare" and not value:
        account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        if not re.fullmatch(r"[A-Za-z0-9]{16,64}", account):
            raise ProviderError(".env에 CLOUDFLARE_ACCOUNT_ID를 설정하세요.")
        value = "https://api.cloudflare.com/client/v4/accounts/%s/ai/run/@cf/cloudflare/%s" % (
            account, urllib.parse.quote(model["model"], safe=""))
    if not value:
        raise ProviderError("API 주소를 설정하세요.")
    return validate_endpoint(value)


def readiness(model):
    if model["provider"] == "demo":
        return "demo"
    if not model.get("model"):
        return "model_missing"
    if model.get("key_env") and not os.environ.get(model["key_env"]):
        return "key_missing"
    try:
        endpoint_for(model)
    except (ValueError, ProviderError):
        return "endpoint_missing"
    return "configured"  # 연결 성공을 뜻하지 않습니다. API는 실행 시에만 호출합니다.


def public_model(model):
    return dict(model, readiness=readiness(model))


def post_json(url, body, headers, timeout):
    """숨은 재시도를 하지 않습니다. 한 관측치가 한 번의 API 시도와 대응합니다."""
    request = urllib.request.Request(url, data=dumps(body).encode("utf-8"),
                                     headers=dict({"Content-Type": "application/json"}, **headers), method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ProviderError("모델 응답 크기 제한을 초과했습니다.")
            return json.loads(data)
    except urllib.error.HTTPError as exc:
        raise ProviderError("모델 API HTTP %s · 키/권한/입력 제한을 확인하세요." % exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderError("모델 서버 연결 실패 또는 시간 초과") from None
    except (ValueError, UnicodeError):
        raise ProviderError("모델 API가 유효한 JSON을 반환하지 않았습니다.") from None


def nonnegative(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def probabilities(value, ids):
    """불완전한 분포를 채우지 않습니다. 반올림 오차만 정규화합니다."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(ids):
        raise ProviderError("확률분포의 선택지가 요청과 일치하지 않습니다.")
    if any(nonnegative(v) is None or v > 1 for v in value.values()):
        raise ProviderError("확률은 0~1의 유한한 숫자여야 합니다.")
    total = sum(value.values())
    if total <= 0 or abs(total - 1) > 0.01:
        raise ProviderError("확률 합이 1이 아닙니다.")
    return {k: v / total for k, v in value.items()}


def normalize_systemone(response, sample):
    if not isinstance(response, dict):
        raise ProviderError("SystemOne 응답 객체가 없습니다.")
    if response.get("success") is False:
        raise ProviderError("모델 제공자가 요청 실패를 반환했습니다.")
    data = response.get("result", response)
    if not isinstance(data, dict):
        raise ProviderError("결과 객체가 없습니다.")
    if data.get("state_truncated") or data.get("truncated") or (data.get("usage") or {}).get("truncated"):
        raise ProviderError("입력이 잘렸다고 보고되어 평가에서 제외했습니다.")
    answer = (data.get("answers") or {}).get("q")
    if not isinstance(answer, dict):
        raise ProviderError("SystemOne answers.q가 없습니다.")
    ids = [c["id"] for c in sample["choices"]]
    probs = probabilities(answer.get("probabilities"), ids)
    choice = answer.get("choice")
    if choice not in ids:
        raise ProviderError("모델이 허용된 선택지 ID를 반환하지 않았습니다.")
    usage = data.get("usage") or {}
    return {"choice": choice, "probabilities": probs, "probability_kind": "native" if probs else None,
            "provider_confidence": nonnegative(answer.get("confidence")),
            "input_tokens": nonnegative(usage.get("input_tokens")),
            "output_tokens": nonnegative(usage.get("output_tokens")),
            "cached_tokens": nonnegative(usage.get("cached_input_tokens")),
            "token_source": usage.get("source", "provider_reported"),
            "token_details": {k: usage[k] for k in ("text_tokens", "image_tokens", "audio_tokens", "video_tokens") if k in usage},
            "provider_model": data.get("model"), "timings": data.get("timings") or {},
            "input_manifest": data.get("input_manifest") or {},
            "truncation_check": "provider_flags_checked",
            "raw": data}


def normalize_generated(response, sample, provider):
    """생성문에 포함된 자기보고 확률을 사용하지 않고 선택 ID만 평가합니다."""
    if provider == "ollama":
        content = (response.get("message") or {}).get("content", "")
        usage = {"input_tokens": response.get("prompt_eval_count"), "output_tokens": response.get("eval_count"),
                 "cached_tokens": response.get("prompt_eval_cached_count")}
        timing = {k + "_ms": response[k] / 1e6 for k in ("load_duration", "prompt_eval_duration", "eval_duration")
                  if nonnegative(response.get(k)) is not None}
        if "prompt_eval_duration_ms" in timing and "eval_duration_ms" in timing:
            timing["inference_ms"] = timing["prompt_eval_duration_ms"] + timing["eval_duration_ms"]
    else:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("생성 결과가 없습니다.") from None
        source = response.get("usage") or {}
        usage = {"input_tokens": source.get("prompt_tokens"), "output_tokens": source.get("completion_tokens"),
                 "cached_tokens": (source.get("prompt_tokens_details") or {}).get("cached_tokens")}
        timing = {}
    try:
        parsed = json.loads(content)
        choice = parsed["choice"]
    except (ValueError, KeyError, TypeError):
        raise ProviderError('모델은 {"choice":"선택지ID"} 형태의 JSON을 반환해야 합니다.') from None
    if choice not in [c["id"] for c in sample["choices"]]:
        raise ProviderError("반환한 선택지 ID가 허용 범위 밖입니다.")
    return dict({k: nonnegative(v) for k, v in usage.items()}, choice=choice, probabilities=None,
                probability_kind=None, provider_confidence=None, token_source="provider_reported", token_details={},
                provider_model=response.get("model"), timings=timing, input_manifest={},
                truncation_check="not_reported", raw=response)


def estimate_cost(result, model):
    """토큰 단가 기반 추정치입니다. 실제 청구액과 로컬 GPU 운영비는 별개입니다."""
    if model["provider"] == "demo":
        return None
    count, out = result.get("input_tokens"), result.get("output_tokens")
    ip, op = model.get("input_price"), model.get("output_price")
    if count is None or ip is None or out is None or (out and op is None):
        return None
    cached = result.get("cached_tokens") or 0
    if cached > count:
        return None
    cp = model.get("cached_input_price")
    if cached and cp is None:
        return None
    return ((count - cached) * ip + cached * (cp or 0) + out * (op or 0)) / 1e6


def predict(model, sample, store):
    """입력 변환, HTTP 호출, 결과 정규화를 수행합니다. 정답은 받지 않습니다."""
    started = time.perf_counter()
    assets = [store.get("assets", asset_id) for asset_id in sample["assets"]]
    if any(a is None for a in assets):
        raise ProviderError("참조하는 미디어가 없습니다.")
    kinds = {a["kind"] for a in assets} or {"text"}
    if not kinds <= set(model["modalities"]):
        raise ProviderError("모델이 해당 입력 형식을 지원하지 않습니다.")
    ids = [c["id"] for c in sample["choices"]]
    if model["provider"] == "demo":
        # 정답을 보지 않고 입력 해시에 따라 고정된 모의 예측을 만듭니다.
        h = hashlib.sha256(dumps(sample).encode()).digest()
        weights = [1 + h[i] for i in range(len(ids))]
        total = sum(weights)
        probs = dict(zip(ids, (v / total for v in weights)))
        time.sleep(0.015)
        return {"choice": max(probs, key=probs.get), "probabilities": probs, "probability_kind": "synthetic",
                "input_tokens": max(1, len(dumps(sample)) // 4), "output_tokens": 0, "cached_tokens": None,
                "token_source": "demo_estimate", "token_details": {}, "provider_model": "demo-v1",
                "provider_confidence": None, "timings": {}, "input_manifest": {"synthetic": True},
                "truncation_check": "not_applicable", "raw": {"demo": True},
                "preprocess_ms": 0.0, "request_ms": (time.perf_counter() - started) * 1000, "cost_estimate": None}
    if readiness(model) != "configured":
        raise ProviderError("모델 이름, API 주소, .env의 키 설정을 확인하세요.")
    headers = {}
    if model.get("key_env"):
        headers["Authorization"] = "Bearer " + os.environ[model["key_env"]]
    media = []
    for asset in assets:
        encoded = base64.b64encode((store.directory / "assets" / asset["id"]).read_bytes()).decode()
        media.append((asset, encoded, "data:%s;base64,%s" % (asset["mime"], encoded)))
    question = {"type": "choice", "instructions": sample["question"],
                "criteria": {c["id"]: c["label"] for c in sample["choices"]}}
    provider = model["provider"]
    if provider in ("typesafe", "systemone", "cloudflare"):
        body = {"model": model["model"], "state": sample["text"], "questions": {"q": question}}
        if provider == "typesafe" and media:
            headers["TypeSafe-Danger-Zone-Images"] = "true"
            body["state"] = {"text": sample["text"], "images": [a["id"] for a, _, _ in media]}
            body["assets"] = {a["id"]: {"type": "image", "format": "png" if a["mime"] == "image/png" else "jpg",
                                      "data_base64": encoded} for a, encoded, _ in media}
            if any(a["mime"] not in ("image/png", "image/jpeg") for a, _, _ in media):
                raise ProviderError("Jev 이미지에는 PNG 또는 JPEG를 사용하세요.")
        else:
            for kind, field in (("image", "images"), ("audio", "audio"), ("video", "videos")):
                values = [uri for a, _, uri in media if a["kind"] == kind]
                if values:
                    body[field] = values
    else:
        prompt = dumps({"state": sample["text"], "question": sample["question"], "choices": question["criteria"]})
        system = 'Return only a JSON object {"choice":"ID"}. Use exactly one supplied choice ID. Do not explain.'
        if provider == "ollama":
            body = {"model": model["model"], "stream": False, "format": "json",
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": prompt, "images": [b for _, b, _ in media]}],
                    "options": {"temperature": 0, "num_predict": 128}}
        else:
            content = [{"type": "text", "text": prompt}]
            for a, encoded, uri in media:
                if a["kind"] == "image":
                    content.append({"type": "image_url", "image_url": {"url": uri}})
                elif a["kind"] == "audio" and a["mime"] in ("audio/wav", "audio/mpeg"):
                    content.append({"type": "input_audio", "input_audio": {"data": encoded, "format": "wav" if a["mime"] == "audio/wav" else "mp3"}})
                else:
                    raise ProviderError("이 어댑터가 지원하지 않는 미디어 형식입니다.")
            body = {"model": model["model"], "stream": False, "temperature": 0, "max_tokens": 128,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
    pre_ms = (time.perf_counter() - started) * 1000
    sent = time.perf_counter()
    raw = post_json(endpoint_for(model), body, headers, model.get("timeout", 120))
    request_ms = (time.perf_counter() - sent) * 1000
    result = normalize_systemone(raw, sample) if provider in ("typesafe", "systemone", "cloudflare") else normalize_generated(raw, sample, provider)
    result.update(preprocess_ms=pre_ms, request_ms=request_ms)
    result["cost_estimate"] = estimate_cost(result, model)
    return result
