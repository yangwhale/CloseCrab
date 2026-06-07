#!/usr/bin/env python3
"""CC API — CloseCrab 通用 HTTP API 服务。

模块化架构，每个 API 独立一个文件：
  cc_api_board.py   实时教学白板 + SVG Canvas
  cc_api_feishu.py  飞书消息 API（以 Chris 身份发消息）

用法:
    python3 tools/cc-api.py --port 8766
"""
import logging
from aiohttp import web

log = logging.getLogger("cc-api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def create_app():
    app = web.Application()

    from cc_api_board import register_routes as board_routes
    board_routes(app)

    from cc_api_feishu import register_routes as feishu_routes
    feishu_routes(app)

    return app


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args()
    app = create_app()
    log.info("CC API starting on %s:%d", args.host, args.port)
    web.run_app(app, host=args.host, port=args.port, print=None)
