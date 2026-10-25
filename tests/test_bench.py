"""채점 오류, 정답 유출, API 규약과 중단/재개를 검증하는 회귀 테스트.

실제 유료 API나 GPU를 호출하지 않습니다. HTTP 요청 파서는 가짜 연결에
직렬화한 요청을 전달해 검증하므로 포트 권한이 없는 환경에서도 실행됩니다.
"""
import base64
import copy
import io
import json
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bench.datasets import inference_sample, parse_dataset, validate_dataset
from bench.metrics import percentile, quality, summarize
from bench.providers import (ProviderError, estimate_cost, normalize_generated, normalize_systemone,
                             predict, validate_endpoint, validate_model)
from bench.server import Application, handler_class
from scripts.convert_dataset import convert


class MemoryConnection:
    """BaseHTTPRequestHandler가 사용하는 최소 소켓 인터페이스만 제공합니다."""
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


def request(app, method, path, payload=None, host="127.0.0.1:8766", csrf=True, origin=None, extra=None):
    body = json.dumps(payload).encode() if payload is not None else b""
    headers = {"Host": host, "Content-Length": str(len(body)), "Content-Type": "application/json"}
    if csrf:
        headers["X-Bench-Token"] = app.csrf
    if origin:
        headers["Origin"] = origin
    headers.update(extra or {})
    data = ("%s %s HTTP/1.0\r\n" % (method, path) +
            "".join("%s: %s\r\n" % (k, v) for k, v in headers.items()) + "\r\n").encode() + body
    connection = MemoryConnection(data)
    handler_class(app)(connection, ("127.0.0.1", 12345), SimpleNamespace(server_port=8766))
    head, output = bytes(connection.output).split(b"\r\n\r\n", 1)
    status = int(head.split(b" ")[1])
    return status, output, head


class BenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.tmp.name))
        self.store = self.app.store
        self.sample = copy.deepcopy(self.store.get("datasets", "demo-dataset")["samples"][0])

    def tearDown(self):
        self.app.runner.close()
        self.app.runner.thread.join(timeout=5)
        self.tmp.cleanup()

    def wait_run(self, run_id, statuses=("completed", "error", "paused"), timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.store.get("runs", run_id)
            if run["status"] in statuses:
                return run
            time.sleep(.01)
        self.fail("실험이 제한 시간 안에 완료되지 않았습니다.")

    def test_label_never_reaches_model(self):
        sent = inference_sample(self.sample)
        self.assertNotIn("label", sent)
        self.assertNotIn("tags", sent)
        self.assertNotIn("group", sent)

    def test_invalid_label_rejected(self):
        self.sample["label"] = "not-a-choice"
        with self.assertRaises(ValueError):
            validate_dataset({"samples": [self.sample]}, self.store)

    def test_duplicate_sample_rejected(self):
        with self.assertRaises(ValueError):
            validate_dataset({"samples": [self.sample, self.sample]}, self.store)

    def test_missing_media_rejected(self):
        self.sample["assets"] = ["../../.env"]
        with self.assertRaises(ValueError):
            validate_dataset({"samples": [self.sample]}, self.store)

    def test_jsonl_and_csv_import(self):
        source = json.dumps(self.sample)
        self.assertEqual(parse_dataset(source + "\n", "d.jsonl")["samples"][0]["id"], self.sample["id"])
        source = 'id,text,question,choices,label\ns,hello,q,"{""a"":""A"",""b"":""B""}",a\n'
        self.assertEqual(parse_dataset(source, "d.csv")["samples"][0]["choices"]["a"], "A")

    def test_demo_cannot_depend_on_label(self):
        model = self.store.get("models", "demo")
        first = predict(model, inference_sample(self.sample), self.store)
        self.sample["label"] = "audio"
        second = predict(model, inference_sample(self.sample), self.store)
        self.assertEqual(first["choice"], second["choice"])
        self.assertEqual(first["probabilities"], second["probabilities"])
        self.assertEqual(first["token_source"], "demo_estimate")
        self.assertIsNone(first["cost_estimate"])

    def system_response(self):
        return {"model": "fixed-rev", "answers": {"q": {"choice": "lock", "probabilities": {
            "lock": .8, "navigation": .1, "audio": .1}}}, "usage": {"input_tokens": 25, "output_tokens": 0}}

    def test_native_probability_normalization(self):
        r = normalize_systemone(self.system_response(), self.sample)
        self.assertEqual(r["input_tokens"], 25)
        self.assertAlmostEqual(sum(r["probabilities"].values()), 1)

    def test_missing_tokens_are_not_zero(self):
        source = self.system_response()
        source.pop("usage")
        r = normalize_systemone(source, self.sample)
        self.assertIsNone(r["input_tokens"])

    def test_common_subset_and_missing_timing(self):
        # 실패가 있는 모델과 성공한 모델의 공통 샘플만 같은 분모로 비교합니다.
        run = {"models": [{"id": x, "name": x, "provider": "systemone"} for x in ("a", "b")],
               "model_active_ms": {"a": 1000, "b": 1000}, "incomplete_timing": True}
        rows = []
        for model in ("a", "b"):
            for sample in ("one", "two"):
                rows.append({"model_id": model, "sample_id": sample, "repetition": 0,
                             "label": "yes", "choice": "yes", "status": "error" if (model, sample) == ("b", "two") else "success",
                             "modality": "text", "tags": ["text", "text"]})
        summary = summarize(run, rows)
        self.assertEqual([row["common_n"] for row in summary], [1, 1])
        self.assertEqual(summary[0]["slices"]["text"]["valid_labeled"], 2)
        self.assertEqual(summary[1]["end_to_end_accuracy"], .5)
        self.assertTrue(all(row["decisions_per_second"] is None for row in summary))

    def test_incomplete_probabilities_rejected(self):
        source = self.system_response()
        del source["answers"]["q"]["probabilities"]["audio"]
        with self.assertRaises(ProviderError):
            normalize_systemone(source, self.sample)

    def test_nan_probability_rejected(self):
        source = self.system_response()
        source["answers"]["q"]["probabilities"]["lock"] = float("nan")
        with self.assertRaises(ProviderError):
            normalize_systemone(source, self.sample)

    def test_truncated_response_rejected(self):
        source = self.system_response()
        source["state_truncated"] = True
        with self.assertRaises(ProviderError):
            normalize_systemone(source, self.sample)

    def test_generated_confidence_not_used(self):
        source = {"model": "model", "message": {"content": '{"choice":"lock","confidence":0.99}'},
                  "prompt_eval_count": 10, "eval_count": 4, "prompt_eval_duration": 1000000, "eval_duration": 2000000}
        r = normalize_generated(source, self.sample, "ollama")
        self.assertIsNone(r["probabilities"])
        self.assertEqual(r["timings"]["inference_ms"], 3)

    def test_cost_requires_price_and_usage(self):
        model = {"provider": "systemone", "input_price": 2, "output_price": 4, "cached_input_price": 1}
        self.assertIsNone(estimate_cost({"input_tokens": None, "output_tokens": 0}, model))
        self.assertAlmostEqual(estimate_cost({"input_tokens": 1000, "output_tokens": 100, "cached_tokens": 100}, model), .0023)
        model["cached_input_price"] = None
        self.assertIsNone(estimate_cost({"input_tokens": 1000, "output_tokens": 0, "cached_tokens": 100}, model))

    def test_endpoint_security(self):
        for url in ["file:///etc/passwd", "http://169.254.169.254/a", "https://u:p@example.com", "https://example.com?a=secret"]:
            with self.assertRaises(ValueError):
                validate_endpoint(url)
        self.assertEqual(validate_endpoint("http://127.0.0.1:9010/v1/systemone"), "http://127.0.0.1:9010/v1/systemone")

    def test_model_does_not_store_secret_value(self):
        model = validate_model(dict(self.store.get("models", "demo"), api_key="SHOULD-NOT-PERSIST"))
        self.assertNotIn("api_key", model)

    def test_http_host_and_origin_checks(self):
        self.assertEqual(request(self.app, "GET", "/api/bootstrap", host="evil.test")[0], 403)
        self.assertEqual(request(self.app, "GET", "/api/bootstrap", origin="https://evil.test")[0], 403)

    def test_http_mutation_requires_session(self):
        self.assertEqual(request(self.app, "POST", "/api/runs", {}, csrf=False)[0], 403)

    def test_http_bootstrap_and_static(self):
        status, body, _ = request(self.app, "GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertIn("csrf", json.loads(body))
        self.assertEqual(request(self.app, "GET", "/")[0], 200)
        self.assertEqual(request(self.app, "GET", "/../run.py")[0], 404)

    def test_wav_upload_and_range(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "none"))
            wav.writeframes(b"\x00\x00" * 160)
        status, body, _ = request(self.app, "POST", "/api/assets", {"name": "clip.wav", "data": base64.b64encode(buffer.getvalue()).decode()})
        self.assertEqual(status, 201)
        asset = json.loads(body)
        self.assertAlmostEqual(asset["duration_seconds"], .01)
        status, body, head = request(self.app, "GET", "/media/" + asset["id"], extra={"Range": "bytes=0-3"})
        self.assertEqual(status, 206)
        self.assertEqual(body, b"RIFF")
        self.assertIn(b"Content-Range", head)

    def test_html_upload_rejected(self):
        status, _, _ = request(self.app, "POST", "/api/assets", {"name": "x.png", "data": base64.b64encode(b"<script>alert(1)</script>").decode()})
        self.assertEqual(status, 400)

    def test_http_run_export(self):
        status, body, _ = request(self.app, "POST", "/api/runs", {"dataset_id": "demo-dataset", "model_ids": ["demo"], "repeats": 2})
        self.assertEqual(status, 201)
        run = self.wait_run(json.loads(body)["id"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["completed"], 12)
        rows = self.store.results(run["id"])
        summary = summarize(run, rows)[0]
        self.assertEqual(summary["valid_labeled"], 6)
        self.assertEqual(summary["success"], 12)
        self.assertGreater(summary["decisions_per_second"], 0)
        self.assertEqual(request(self.app, "GET", "/api/runs/" + run["id"] + "/export?format=csv")[0], 200)

    def test_stop_resume_no_duplicate_results(self):
        run = self.app.runner.create({"dataset_id": "demo-dataset", "model_ids": ["demo"], "repeats": 10})
        deadline = time.monotonic() + 2
        while not self.store.results(run["id"]) and time.monotonic() < deadline:
            time.sleep(.005)
        self.app.runner.stop(run["id"])
        paused = self.wait_run(run["id"], statuses=("paused",))
        self.assertLess(paused["completed"], paused["total"])
        previous = self.store.results(run["id"])[0]
        # 정리 스레드가 stop_events에서 ID를 제거할 때까지 짧게 기다립니다.
        deadline = time.monotonic() + 2
        while run["id"] in self.app.runner.stop_events and time.monotonic() < deadline:
            time.sleep(.005)
        self.app.runner.resume(run["id"])
        finished = self.wait_run(run["id"])
        self.assertEqual(finished["completed"], 60)
        self.assertEqual(self.store.results(run["id"])[0], previous)

    def test_model_snapshot_preserved(self):
        run = self.app.runner.create({"dataset_id": "demo-dataset", "model_ids": ["demo"]})
        model = self.store.get("models", "demo")
        model["name"] = "changed"
        self.store.put("models", model)
        self.assertNotEqual(self.wait_run(run["id"])["models"][0]["name"], "changed")

    def test_warmup_excluded_from_observations(self):
        run = self.app.runner.create({"dataset_id": "demo-dataset", "model_ids": ["demo"], "warmup": 2})
        final = self.wait_run(run["id"])
        self.assertEqual(len(final["warmup_log"]), 2)
        self.assertEqual(len(self.store.results(run["id"])), 6)

    def test_provider_request_contains_no_gold(self):
        model = self.store.get("models", "jev-omni")
        with patch("bench.providers.post_json", return_value=self.system_response()) as transport:
            predict(model, inference_sample(self.sample), self.store)
            body = transport.call_args.args[1]
            self.assertNotIn("label", body)
            self.assertEqual(body["questions"]["q"]["type"], "choice")
            self.assertEqual(body["state"], self.sample["text"])

    def test_mixed_missing_modality_not_silently_ignored(self):
        asset = self.app.upload({"name": "x.png", "data": base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()})
        sample = dict(self.sample, assets=[asset["id"]])
        model = dict(self.store.get("models", "demo"), modalities=["text"])
        with self.assertRaises(ProviderError):
            predict(model, inference_sample(sample), self.store)

    def test_known_metrics(self):
        rows = [{"status": "success", "label": "a", "choice": "a", "probabilities": {"a": .8, "b": .2}},
                {"status": "success", "label": "b", "choice": "a", "probabilities": {"a": .6, "b": .4}},
                {"status": "error", "label": "a"}, {"status": "skipped", "label": "a"}]
        q = quality(rows)
        self.assertEqual(q["accuracy"], .5)
        self.assertAlmostEqual(q["end_to_end_accuracy"], 1 / 3)
        self.assertAlmostEqual(q["brier"], .4)
        self.assertAlmostEqual(q["ece"], .4)
        self.assertEqual(percentile([10, 20, 30], .5), 20)
        self.assertIsNone(percentile([], .95))

    def test_mmau_converter_preserves_answer(self):
        source = Path(self.tmp.name) / "mini.json"
        source.write_text(json.dumps([{"id": "a", "audio_id": "sounds/clip.wav", "question": "q",
                                       "choices": ["rain", "wind"], "answer": "wind"}]))
        target = convert("mmau", source, Path(self.tmp.name) / "out")
        row = json.loads(target.read_text())["samples"][0]
        self.assertEqual(row["label"], "1")
        self.assertEqual(row["media_name"], "clip.wav")

    def test_mvbench_segment_not_ignored(self):
        source = Path(self.tmp.name) / "video.json"
        source.write_text(json.dumps([{"video": "v.mp4", "question": "q", "candidates": ["a", "b"],
                                       "answer": "a", "start": 5, "end": 7}]))
        with self.assertRaises(ValueError):
            convert("mvbench", source, Path(self.tmp.name) / "out")


if __name__ == "__main__":
    unittest.main()
