from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from .config import AppConfig, ConfigError, HttpConfig, load_config

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_INVALID = 2
EXIT_UNREACHABLE = 3
EXIT_REMOTE = 4
EXIT_AUTH = 5
EXIT_RATE_LIMITED = 6


class CliError(Exception):
    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _base_url(config: HttpConfig) -> str:
    host = config.host.strip()
    if host in ("", "0.0.0.0", "*"):
        host = "127.0.0.1"
    elif host == "::":
        host = "[::1]"
    return "http://{}:{}".format(host, config.port)


def _exit_code_for_status(status: int) -> int:
    if status in (401, 403):
        return EXIT_AUTH
    if status == 404:
        return EXIT_NOT_FOUND
    if status == 400:
        return EXIT_INVALID
    if status == 429:
        return EXIT_RATE_LIMITED
    return EXIT_REMOTE


class ApiClient:
    def __init__(
        self, base_url: str, token: Optional[str], timeout: float = 10.0
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _raw(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Tuple[int, bytes, str]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.token:
            headers["Authorization"] = "Bearer {}".format(self.token)
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return (
                    int(response.status),
                    response.read(),
                    response.headers.get("Content-Type", ""),
                )
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read(), exc.headers.get("Content-Type", "")
        except urllib.error.URLError as exc:
            raise CliError(
                "daemon_unreachable",
                "无法连接 daemon {}: {}".format(self.base_url, exc.reason),
                EXIT_UNREACHABLE,
            )

    def _json(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        status, body, _ = self._raw(method, path, payload)
        if status < 200 or status >= 300:
            self._raise_http(status, body)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CliError("invalid_response", "daemon 返回了非 JSON 响应", EXIT_REMOTE)
        if not isinstance(data, dict):
            raise CliError("invalid_response", "daemon 返回的 JSON 顶层不是对象", EXIT_REMOTE)
        return data

    def _raise_http(self, status: int, body: bytes) -> None:
        code = "http_{}".format(status)
        message = "HTTP {}".format(status)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            code = str(payload.get("code") or payload.get("error") or code)
            message = str(payload.get("message") or payload.get("error") or message)
        raise CliError(code, message, _exit_code_for_status(status))

    def status(self) -> Dict[str, Any]:
        return self._json("GET", "/status")

    def health(self) -> Dict[str, Any]:
        return self._json("GET", "/health")

    def incidents(self, open_only: bool, limit: int) -> List[Dict[str, Any]]:
        query = "?open={}&limit={}".format(1 if open_only else 0, limit)
        data = self._json("GET", "/incidents" + query)
        incidents = data.get("incidents", [])
        if not isinstance(incidents, list):
            raise CliError("invalid_response", "incidents 不是数组", EXIT_REMOTE)
        return incidents

    def incident(self, reference: str) -> Dict[str, Any]:
        return self._json("GET", "/incidents/{}".format(quote(reference, safe="")))

    def decide(
        self,
        reference: str,
        action: str,
        note: Optional[str],
        actor: str = "cli",
        short_code: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._json(
            "POST",
            "/incidents/{}/decide".format(quote(reference, safe="")),
            {
                "action": action,
                "note": note,
                "actor": actor,
                "short_code": short_code,
                "idempotency_key": idempotency_key or uuid.uuid4().hex,
            },
        )

    def label(
        self, reference: str, label: str, note: Optional[str], actor: str = "cli"
    ) -> Dict[str, Any]:
        return self._json(
            "POST",
            "/incidents/{}/label".format(quote(reference, safe="")),
            {"label": label, "note": note, "actor": actor},
        )

    def snapshot(self) -> bytes:
        status, body, _ = self._raw("GET", "/snapshot")
        if status < 200 or status >= 300:
            self._raise_http(status, body)
        return body


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _print_status(data: Dict[str, Any]) -> None:
    print("daemon: {}".format(data.get("daemon", "unknown")))
    printer = data.get("printer")
    if isinstance(printer, dict):
        for key in (
            "gcode_state",
            "progress_percent",
            "layer_num",
            "total_layers",
            "nozzle_temperature_c",
            "bed_temperature_c",
        ):
            if key in printer and printer[key] is not None:
                print("{}: {}".format(key, printer[key]))


def _print_incidents(incidents: List[Dict[str, Any]]) -> None:
    if not incidents:
        print("no incidents")
        return
    for item in incidents:
        confidence = item.get("confidence")
        confidence_text = "{:.0%}".format(float(confidence)) if confidence is not None else "?"
        print(
            "{short} {severity} {state} {kind} conf={confidence}".format(
                short=item.get("short_code", "????"),
                severity=item.get("severity", "?"),
                state=item.get("state", "?"),
                kind=item.get("kind", "?"),
                confidence=confidence_text,
            )
        )


def _print_incident(detail: Dict[str, Any]) -> None:
    incident = detail.get("incident", detail)
    if not isinstance(incident, dict):
        _print_json(detail)
        return
    print(
        "{short} {severity} {state} {kind} conf={confidence}".format(
            short=incident.get("short_code", "????"),
            severity=incident.get("severity", "?"),
            state=incident.get("state", "?"),
            kind=incident.get("kind", "?"),
            confidence=incident.get("confidence", "?"),
        )
    )
    if incident.get("outcome"):
        print("outcome: {}".format(incident["outcome"]))
    if incident.get("filmstrip_dir"):
        print("filmstrip: {}".format(incident["filmstrip_dir"]))
    events = detail.get("events", [])
    if isinstance(events, list) and events:
        print("events: {}".format(len(events)))
    labels = detail.get("labels", [])
    if isinstance(labels, list) and labels:
        print("labels: {}".format(len(labels)))


def _cmd_status(client: ApiClient, args: argparse.Namespace) -> int:
    data = client.status()
    if args.json:
        _print_json(data)
    else:
        _print_status(data)
    return EXIT_OK


def _cmd_incidents(client: ApiClient, args: argparse.Namespace) -> int:
    incidents = client.incidents(open_only=args.open, limit=args.limit)
    if args.json:
        _print_json({"incidents": incidents})
    else:
        _print_incidents(incidents)
    return EXIT_OK


def _cmd_incident_show(client: ApiClient, args: argparse.Namespace) -> int:
    detail = client.incident(args.reference)
    if args.json:
        _print_json(detail)
    else:
        _print_incident(detail)
    return EXIT_OK


def _cmd_incident_decide(client: ApiClient, args: argparse.Namespace) -> int:
    result = client.decide(
        args.reference,
        args.action,
        args.note,
        short_code=args.code,
        idempotency_key=args.idempotency_key,
    )
    if args.json:
        _print_json(result)
    else:
        print(result.get("message") or result.get("code", "ok"))
    return EXIT_OK


def _cmd_incident_label(client: ApiClient, args: argparse.Namespace) -> int:
    result = client.label(args.reference, args.label, args.note)
    if args.json:
        _print_json(result)
    else:
        print(result.get("code", "ok"))
    return EXIT_OK


def _cmd_snapshot(client: ApiClient, args: argparse.Namespace) -> int:
    body = client.snapshot()
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(body)
    if args.json:
        _print_json({"path": str(output), "bytes": len(body)})
    else:
        print(str(output))
    return EXIT_OK


def _cmd_health(client: ApiClient, args: argparse.Namespace) -> int:
    data = client.health()
    if args.json:
        _print_json(data)
    else:
        print("ok" if data.get("ok") else "unhealthy")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smart-p1sc", description="Smart P1SC 本地异常监控 CLI"
    )
    parser.add_argument("--config", help="配置文件路径（默认 config.json）")
    parser.add_argument("--json", action="store_true", help="输出稳定 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查看 daemon 与打印机状态")
    sub.add_parser("health", help="检查本地 API 是否存活")

    incidents = sub.add_parser("incidents", help="列出异常事件")
    incidents.add_argument("--open", action="store_true", help="只显示未结束事件")
    incidents.add_argument("--limit", type=int, default=20, help="最多返回条数")

    incident = sub.add_parser("incident", help="查看或处置单个异常事件")
    incident_sub = incident.add_subparsers(dest="incident_command", required=True)

    show = incident_sub.add_parser("show", help="查看事件详情")
    show.add_argument("reference", help="事件 ID 或 4 位短码")

    decide = incident_sub.add_parser("decide", help="继续/停止/误报")
    decide.add_argument("reference", help="事件 ID 或 4 位短码")
    decide.add_argument(
        "--action",
        required=True,
        choices=["resume", "stop", "false_positive"],
        help="处置动作",
    )
    decide.add_argument("--code", required=True, help="事件 4 位短码，用于双重校验")
    decide.add_argument("--idempotency-key", help="幂等键（重试时复用）")
    decide.add_argument("--note", help="可选备注")

    label = incident_sub.add_parser("label", help="写入人工标签")
    label.add_argument("reference", help="事件 ID 或 4 位短码")
    label.add_argument(
        "--label",
        required=True,
        choices=["true_positive", "false_positive", "unknown"],
        help="标签",
    )
    label.add_argument("--note", help="可选备注")

    snapshot = sub.add_parser("snapshot", help="保存当前摄像头帧")
    snapshot.add_argument("--output", default="snapshot.jpg", help="输出文件路径")

    return parser


def _build_client(config: AppConfig) -> ApiClient:
    token = config.http.api_token()
    if not token:
        raise CliError(
            "missing_token",
            "缺少本地 API token：请设置环境变量 {}".format(config.http.token_env),
            EXIT_AUTH,
        )
    return ApiClient(_base_url(config.http), token)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        client = _build_client(config)
        handlers = {
            "status": _cmd_status,
            "health": _cmd_health,
            "incidents": _cmd_incidents,
            "incident_show": _cmd_incident_show,
            "incident_decide": _cmd_incident_decide,
            "incident_label": _cmd_incident_label,
            "snapshot": _cmd_snapshot,
        }
        if args.command == "incident":
            key = "incident_" + args.incident_command
        else:
            key = args.command
        return handlers[key](client, args)
    except ConfigError as exc:
        print("error: config: {}".format(exc), file=sys.stderr)
        return EXIT_INVALID
    except CliError as exc:
        print("error: {}: {}".format(exc.code, exc.message), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
