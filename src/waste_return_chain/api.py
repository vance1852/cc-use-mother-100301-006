"""废弃物回运责任项目的 HTTP/JSON 边界。

路由挂载在 ``/waste`` 前缀下；基础服务的既有路由（组织、操作者、场所、
健康检查、审计）原样保留，写入接口继续通过 X-Actor-Id 标识操作者，
所有写操作都要求 request_id 以支持重复回调的幂等核销。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from polar_station_foundation import api as foundation_api
from polar_station_foundation.errors import DomainError, ValidationError

from .service import WasteService
from .storage import WasteDatabase


def _receipt(status_created: int, receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else status_created), receipt.__dict__


def route(service: WasteService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到废弃物服务，未命中则回落基础路由。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    if not parsed.path.startswith("/waste"):
        return foundation_api.route(service, method, path, body, headers)
    query = parse_qs(parsed.query)
    try:
        p = parsed.path

        if method == "POST" and p == "/waste/regulations":
            return _receipt(201, service.publish_regulation(actor_id=actor_id, **body))
        if method == "GET" and p == "/waste/regulations":
            version_id = query.get("version_id", [""])[0]
            if not version_id:
                raise ValidationError("version_id 不能为空")
            return 200, service.get_regulation_version(version_id).__dict__

        if method == "POST" and p == "/waste/lots":
            return _receipt(201, service.register_lot(actor_id=actor_id, **body))
        if method == "POST" and p == "/waste/lot-corrections":
            return _receipt(201, service.correct_lot_quantity(actor_id=actor_id, **body))
        if method == "GET" and p == "/waste/lots":
            return 200, service.get_lot(query.get("lot_id", [""])[0]).__dict__

        if method == "POST" and p == "/waste/containers/seal":
            return _receipt(201, service.seal_container(actor_id=actor_id, **body))
        if method == "POST" and p == "/waste/containers/damaged":
            return _receipt(200, service.mark_container_damaged(actor_id=actor_id, **body))
        if method == "POST" and p == "/waste/containers/repack":
            return _receipt(201, service.repack_container(actor_id=actor_id, **body))
        if method == "POST" and p == "/waste/containers/reject-received":
            return _receipt(200, service.reject_received_container(actor_id=actor_id, **body))
        if method == "GET" and p == "/waste/containers":
            return 200, service.get_container(query.get("container_id", [""])[0]).__dict__
        if method == "GET" and p == "/waste/containers/manifest":
            container_id = query.get("container_id", [""])[0]
            return 200, {"items": [item.__dict__ for item in service.manifest(container_id)]}
        if method == "GET" and p == "/waste/containers/responsibility":
            container_id = query.get("container_id", [""])[0]
            return 200, service.current_responsibility(container_id)
        if method == "GET" and p == "/waste/containers/transfers":
            container_id = query.get("container_id", [""])[0]
            return 200, {"items": [item.__dict__ for item in service.list_transfers(container_id)]}

        if method == "POST" and p == "/waste/storage/check-in":
            return _receipt(201, service.check_in_storage(actor_id=actor_id, **body))
        if method == "POST" and p == "/waste/storage/check-out":
            return _receipt(200, service.check_out_storage(actor_id=actor_id, **body))

        if method == "POST" and p == "/waste/transfers/propose":
            return _receipt(201, service.propose_transfer(actor_id=actor_id, **body))
        if method == "POST" and p == "/waste/transfers/confirm":
            return _receipt(200, service.confirm_transfer(actor_id=actor_id, **body))
        if method == "POST" and p == "/waste/transfers/cancel":
            return _receipt(200, service.cancel_transfer(actor_id=actor_id, **body))
        if method == "POST" and p == "/waste/transfers/reject":
            return _receipt(200, service.reject_transfer(actor_id=actor_id, **body))
        if method == "POST" and p == "/waste/receipts/acknowledge":
            return _receipt(201, service.acknowledge_receipt(actor_id=actor_id, **body))

        if method == "POST" and p == "/waste/disposal/certify":
            return _receipt(201, service.certify_disposal(actor_id=actor_id, **body))

        if method == "GET" and p == "/waste/trace/lot":
            return 200, service.trace_lot(query.get("lot_id", [""])[0])
        if method == "GET" and p == "/waste/trace/container":
            return 200, service.trace_container(query.get("container_id", [""])[0])
        if method == "GET" and p == "/waste/overdue":
            return 200, service.overdue_nodes()
        if method == "GET" and p == "/waste/quantity-variance":
            return 200, {"items": service.quantity_variance()}

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError, KeyError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为废弃物路由调用。"""

    service: WasteService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动废弃物回运责任 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动废弃物回运责任服务")
    parser.add_argument("--database", default="waste_return.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = WasteDatabase(args.database)
    Handler.service = WasteService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
