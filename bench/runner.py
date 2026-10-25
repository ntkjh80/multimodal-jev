"""중단 후 재개할 수 있는 실험 실행기.

실험을 하나씩 실행하고 실험 안에서도 모델을 순차 실행합니다. 동일 GPU의
모델 간 자원 경쟁을 피하며, 동시 요청 수는 개별 모델 안에서 적용합니다.
"""
import platform
import queue
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .datasets import inference_sample
from .providers import ProviderError, predict
from .storage import new_id, now


class Runner:
    def __init__(self, store):
        self.store = store
        self.queue = queue.Queue()
        self.lock = threading.RLock()
        self.stop_events = {}
        self.closed = False
        self.thread = threading.Thread(target=self._loop, daemon=True, name="bench-scheduler")
        self.thread.start()

    def create(self, payload):
        dataset = self.store.get("datasets", str(payload.get("dataset_id", "")))
        if not dataset:
            raise ValueError("데이터셋을 선택하세요.")
        selected = payload.get("model_ids", [])
        if not isinstance(selected, list) or not 1 <= len(selected) <= 8 or len(set(selected)) != len(selected):
            raise ValueError("모델은 중복 없이 1~8개를 선택하세요.")
        models = [self.store.get("models", str(mid)) for mid in selected]
        if any(m is None for m in models):
            raise ValueError("없는 모델입니다.")
        concurrency, repeats = int(payload.get("concurrency", 1)), int(payload.get("repeats", 1))
        warmup = int(payload.get("warmup", 0))
        if not 1 <= concurrency <= 16 or not 1 <= repeats <= 10 or not 0 <= warmup <= 5:
            raise ValueError("동시 요청 1~16, 반복 1~10, 워밍업 0~5를 사용하세요.")
        mode = payload.get("modality", "all")
        samples = [s for s in dataset["samples"] if mode == "all" or s["modality"] == mode]
        seed = int(payload.get("seed", 42))
        random.Random(seed).shuffle(samples)
        limit = int(payload.get("limit", 0))
        if limit < 0:
            raise ValueError("샘플 제한은 0 이상이어야 합니다.")
        if limit:
            samples = samples[:limit]
        if not samples:
            raise ValueError("선택한 조건의 샘플이 없습니다.")
        if len(samples) * len(models) * repeats > 100000:
            raise ValueError("한 실험은 100,000개 관측치까지 지원합니다.")
        run = {"id": new_id("run"), "name": str(payload.get("name") or dataset["name"])[:120],
               "status": "queued", "created_at": now(), "updated_at": now(), "dataset": dataset,
               "models": models, "sample_ids": [s["id"] for s in samples],
               "assets": [self.store.get("assets", aid) for aid in sorted({a for s in samples for a in s["assets"]})],
               "config": {"concurrency": concurrency, "repeats": repeats, "warmup": warmup, "seed": seed,
                          "limit": limit, "modality": mode, "questions_per_request": 1, "automatic_retries": 0},
               "environment": {"python": platform.python_version(), "os": platform.platform(),
                               "hardware_note": str(payload.get("hardware_note") or ""),
                               "cache_policy": "provider_default; app_prediction_cache_disabled"},
               "total": len(samples) * len(models) * repeats, "completed": 0, "model_active_ms": {},
               "warmup_log": [], "segments": [], "error": None}
        self.store.put("runs", run)
        self.resume(run["id"])
        return self.store.get("runs", run["id"])

    def resume(self, run_id):
        with self.lock:
            run = self.store.get("runs", run_id)
            if not run:
                raise ValueError("없는 실험입니다.")
            if run_id in self.stop_events:
                raise ValueError("이미 실행 또는 대기 중인 실험입니다.")
            if run["status"] == "completed":
                raise ValueError("완료된 실험입니다. 재측정은 새 실험으로 실행하세요.")
            if run["status"] in ("interrupted", "error"):
                # 강제 종료 구간의 처리량은 벽시계 시간을 복원할 수 없으므로 미측정입니다.
                run["incomplete_timing"] = True
            self.stop_events[run_id] = threading.Event()
            run.update(status="queued", updated_at=now(), error=None)
            self.store.put("runs", run)
            self.queue.put(run_id)

    def stop(self, run_id):
        with self.lock:
            if run_id in self.stop_events:
                self.stop_events[run_id].set()
                run = self.store.get("runs", run_id)
                run.update(status="stopping", updated_at=now())
                self.store.put("runs", run)

    def close(self):
        self.closed = True
        for event in list(self.stop_events.values()):
            event.set()
        self.queue.put(None)

    def _loop(self):
        while not self.closed:
            run_id = self.queue.get()
            if run_id is None:
                return
            try:
                self._run(run_id)
            except Exception:
                # 키나 원문이 오류 문자열에 포함되지 않도록 일반화합니다.
                run = self.store.get("runs", run_id)
                run.update(status="error", error="실험 관리 중 오류가 발생했습니다. 저장된 결과는 유지됩니다.", updated_at=now())
                self.store.put("runs", run)
            finally:
                with self.lock:
                    self.stop_events.pop(run_id, None)

    def _observe(self, run, model, sample, repetition):
        start = time.perf_counter()
        row = {"run_id": run["id"], "model_id": model["id"], "sample_id": sample["id"],
               "repetition": repetition, "label": sample["label"], "modality": sample["modality"],
               "tags": sample["tags"], "group": sample["group"], "created_at": now(),
               "status": "success", "error": None, "demo": model["provider"] == "demo"}
        kinds = {self.store.get("assets", a)["kind"] for a in sample["assets"]} or {"text"}
        if not kinds <= set(model["modalities"]):
            row.update(status="skipped", error="모델의 입력 지원 범위 밖입니다.", total_ms=None)
            return row
        try:
            row.update(predict(model, inference_sample(sample), self.store))
        except (ProviderError, ValueError) as exc:
            row.update(status="error", error=str(exc))
        except Exception:
            row.update(status="error", error="응답 처리 오류. 모델 규약과 입력 형식을 확인하세요.")
        row["total_ms"] = (time.perf_counter() - start) * 1000
        return row

    def _run(self, run_id):
        run = self.store.get("runs", run_id)
        stop = self.stop_events[run_id]
        run.update(status="running", updated_at=now())
        self.store.put("runs", run)
        lookup = {s["id"]: s for s in run["dataset"]["samples"]}
        samples = [lookup[sid] for sid in run["sample_ids"]]
        completed = {(r["model_id"], r["sample_id"], r["repetition"]) for r in self.store.results(run_id)}
        run["completed"] = len(completed)
        for model in run["models"]:
            if stop.is_set():
                break
            pending = [(s, rep) for rep in range(run["config"]["repeats"]) for s in samples
                       if (model["id"], s["id"], rep) not in completed]
            if not pending:
                continue
            # 워밍업도 유료 API를 호출할 수 있으므로 사용량을 별도로 기록합니다.
            for i in range(run["config"]["warmup"]):
                if stop.is_set():
                    break
                warm = self._observe(run, model, pending[0][0], -1)
                warm.pop("raw", None)
                run["warmup_log"].append(warm)
                self.store.put("runs", run)
            if stop.is_set():
                break
            segment_start, timestamp = time.perf_counter(), now()
            iterator = iter(pending)
            with ThreadPoolExecutor(max_workers=run["config"]["concurrency"]) as pool:
                futures = set()
                exhausted = False
                while futures or not exhausted:
                    while not exhausted and not stop.is_set() and len(futures) < run["config"]["concurrency"]:
                        try:
                            sample, rep = next(iterator)
                        except StopIteration:
                            exhausted = True
                            break
                        futures.add(pool.submit(self._observe, run, model, sample, rep))
                    if not futures:
                        break
                    done, futures = wait(futures, timeout=.25, return_when=FIRST_COMPLETED)
                    for future in done:
                        self.store.result(future.result())
                        run["completed"] += 1
                        run["updated_at"] = now()
                        run["status"] = "stopping" if stop.is_set() else "running"
                        self.store.put("runs", run)
            elapsed = (time.perf_counter() - segment_start) * 1000
            run["model_active_ms"][model["id"]] = run["model_active_ms"].get(model["id"], 0) + elapsed
            run["segments"].append({"model_id": model["id"], "started_at": timestamp, "elapsed_ms": elapsed})
            self.store.put("runs", run)
        run.update(status="paused" if stop.is_set() else "completed", updated_at=now())
        self.store.put("runs", run)
