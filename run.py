#!/usr/bin/env python3
"""Windows/macOS 공통 실행 진입점. python run.py --port 8766"""
import argparse
import sys
import threading
import webbrowser

from bench.config import data_dir, load_env
from bench.server import Application, create_server


def main():
    parser = argparse.ArgumentParser(description="멀티모달 벤치마크 로컬 웹앱")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--open", action="store_true", help="기본 웹브라우저 열기")
    args = parser.parse_args()
    load_env()
    app = Application(data_dir())
    try:
        server = create_server(app, args.port)
    except OSError as exc:
        app.runner.close()
        if exc.errno in (48, 98, 10048):
            print("포트가 사용 중입니다. python run.py --port 8767 로 실행하세요.", file=sys.stderr)
        else:
            print("로컬 서버를 열 수 없습니다: " + str(exc), file=sys.stderr)
        return 1
    url = "http://127.0.0.1:%d" % server.server_port
    print("Multimodal Bench: " + url, flush=True)
    print("종료: Ctrl+C · 데이터는 data/ 에 저장됩니다.", flush=True)
    if args.open:
        threading.Timer(.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.runner.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
