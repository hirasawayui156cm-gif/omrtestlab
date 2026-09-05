"""启动入口：python run.py 或直接运行，默认端口 8000。"""
import os
import socket
import sys
import threading
import time

import uvicorn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def _pick_port(prefer: int) -> int:
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", prefer))
        s.close()
        return prefer
    except OSError:
        return prefer + 1


def main():
    import argparse
    ap = argparse.ArgumentParser(description="OMR 多链路聚合测试台")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址")
    ap.add_argument("--port", type=int, default=8000, help="端口")
    args = ap.parse_args()

    port = _pick_port(args.port)
    print("=" * 60)
    print("  OMR 多链路聚合测试台  OMR Link Aggregation Test Lab")
    print("=" * 60)
    print(f"  请在浏览器打开:  http://127.0.0.1:{port}")
    print("  停止服务: Ctrl+C")
    print("=" * 60)
    # 让 FastAPI / WS 事件循环在子线程准备就绪
    def _warmup():
        time.sleep(0.5)
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        except OSError:
            pass
    threading.Thread(target=_warmup, daemon=True).start()
    uvicorn.run("app.server:app", host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
