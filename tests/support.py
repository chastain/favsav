"""Shared helpers for the favsav tests."""

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeJellyfin:
    """A tiny stand-in for a Jellyfin server.

    It serves just enough of the API for the tests: the user list, the favorites
    query, the children query and the system info.  Pagination is honoured so that
    the client's paging code is exercised.
    """

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, Any]] = {}
        self.users: List[Dict[str, Any]] = []
        self.children: Dict[str, List[str]] = {}
        self.favourites: Dict[str, List[str]] = {}
        self.system_info: Dict[str, Any] = {"ProgramDataPath": "/var/lib/jellyfin"}
        self.requests: List[str] = []
        self.api_key = "test-key"
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- setup

    def add_user(self, user_id: str, name: str) -> None:
        self.users.append(
            {"Id": user_id, "Name": name, "Policy": {"IsAdministrator": True}}
        )

    def add_item(
        self,
        item_id: str,
        name: str,
        item_type: str,
        path: Optional[str] = None,
        location_type: str = "FileSystem",
        favorite: bool = False,
        extra_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        item = {
            "Id": item_id,
            "Name": name,
            "Type": item_type,
            "Path": path,
            "LocationType": location_type,
            "IsFolder": item_type not in ("Movie", "Episode", "Video", "Audio"),
        }
        if extra_ids:
            item["ExtraIds"] = list(extra_ids)
        self.items[item_id] = item
        if favorite:
            for user in self.users:
                self.favourites.setdefault(item_id, []).append(user["Id"])
        return item

    def favorite(self, item_id: str, user_id: str) -> None:
        self.favourites.setdefault(item_id, []).append(user_id)

    def add_child(self, parent_id: str, child_id: str) -> None:
        self.children.setdefault(parent_id, []).append(child_id)

    # ---------------------------------------------------------------- server

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # noqa: A003
                pass

            def do_GET(self) -> None:  # noqa: N802
                fake.requests.append(self.path)
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                path = parsed.path

                if path == "/System/Info":
                    self._send(200, fake.system_info)
                    return
                if path == "/Users":
                    self._send(200, fake.users)
                    return
                if path == "/Items":
                    self._send(200, fake._items(query))
                    return
                self._send(404, {"error": "not found"})

            def _send(self, code: int, payload: Any) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _items(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
        start = int(query.get("startIndex", ["0"])[0])
        limit = int(query.get("limit", ["100"])[0])
        parent = query.get("parentId", [None])[0]
        ids = query.get("ids", [None])[0]
        user = query.get("userId", [None])[0]

        if ids is not None:
            wanted = [value for value in ids.split(",") if value]
            selected = [self.items[item_id] for item_id in wanted if item_id in self.items]
        elif parent:
            # Virtual items (no path) only expose their children when a user is
            # specified, mirroring Jellyfin's behaviour for virtual seasons.
            parent_item = self.items.get(parent, {})
            if parent_item.get("LocationType") == "Virtual" and not user:
                selected = []
            else:
                selected = [
                    self.items[item_id]
                    for item_id in self.children.get(parent, [])
                    if item_id in self.items
                ]
        else:
            filters = query.get("filters", [""])[0]
            if filters == "IsFavorite":
                selected = [
                    self.items[item_id]
                    for item_id, users in self.favourites.items()
                    if user is None or user in users
                ]
            else:
                selected = list(self.items.values())

        total = len(selected)
        page = selected[start : start + limit]
        return {"Items": page, "TotalRecordCount": total, "StartIndex": start}


def write(path: str, content: str = "x") -> str:
    """Create a file, making its directory, and return the path."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def tree(root: str) -> List[str]:
    """Return every file below ``root``, as paths relative to it, sorted."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            found.append(os.path.relpath(full, root))
    return sorted(found)


class FakeSMTP:
    """A very small SMTP server for the notification tests.

    It speaks just enough SMTP to accept a message, and it can require implicit TLS
    or offer STARTTLS.  It records the message it received so a test can look at it.

    TLS needs a certificate.  One is generated with ``openssl`` at start-up; if
    ``openssl`` is not available, the TLS tests are skipped rather than failed.
    """

    def __init__(self, implicit_tls: bool = False) -> None:
        self.implicit_tls = implicit_tls
        self.messages: List[Dict[str, Any]] = []
        self.port = 0
        self.tls_available = False
        self._directory = ""
        self._certificate: Optional[Tuple[str, str]] = None
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ setup

    def _make_certificate(self) -> Optional[Tuple[str, str]]:
        if not shutil.which("openssl"):
            return None
        directory = tempfile.mkdtemp(prefix="favsav-smtp-")
        key = os.path.join(directory, "key.pem")
        cert = os.path.join(directory, "cert.pem")
        try:
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", key, "-out", cert, "-days", "1",
                    "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        self._directory = directory
        return cert, key

    def _server_context(self) -> Optional[ssl.SSLContext]:
        if self._certificate is None:
            return None
        cert, key = self._certificate
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.tls_available = True
        return context

    def start(self) -> int:
        self._certificate = self._make_certificate()
        self.tls_available = self._certificate is not None
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(5)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._directory:
            shutil.rmtree(self._directory, ignore_errors=True)

    def _serve(self) -> None:
        while True:
            try:
                connection, _address = self._server.accept()  # type: ignore[union-attr]
            except OSError:
                return
            try:
                # The session may upgrade the connection to TLS, which replaces the
                # socket object, so the session hands the final one back.
                connection = self._session(connection)
            except Exception:  # pragma: no cover - the test asserts on the outcome
                pass
            finally:
                try:
                    connection.close()
                except OSError:
                    pass

    def _session(self, connection: socket.socket) -> socket.socket:
        if self.implicit_tls:
            context = self._server_context()
            if context is None:
                return connection
            connection = context.wrap_socket(connection, server_side=True)

        stream = connection.makefile("rwb")

        def send(line: str) -> None:
            stream.write(line.encode("utf-8") + b"\r\n")
            stream.flush()

        send("220 fake.smtp ESMTP")
        envelope: Dict[str, Any] = {"from": "", "to": [], "data": ""}
        authenticated = False

        while True:
            raw = stream.readline()
            if not raw:
                return connection
            line = raw.decode("utf-8", "replace").strip()
            upper = line.upper()

            if upper.startswith("EHLO"):
                send("250-fake.smtp")
                send("250-AUTH LOGIN PLAIN")
                if not self.implicit_tls:
                    send("250 STARTTLS")
                else:
                    send("250 SIZE 10240000")
            elif upper.startswith("HELO"):
                send("250 fake.smtp")
            elif upper.startswith("STARTTLS"):
                context = self._server_context()
                if context is None:
                    return connection
                send("220 ready to start TLS")
                connection = context.wrap_socket(connection, server_side=True)
                stream = connection.makefile("rwb")
            elif upper.startswith("AUTH"):
                authenticated = True
                send("235 authenticated")
            elif upper.startswith("MAIL FROM"):
                envelope["from"] = line.split(":", 1)[1].strip()
                send("250 ok")
            elif upper.startswith("RCPT TO"):
                envelope["to"].append(line.split(":", 1)[1].strip())
                send("250 ok")
            elif upper.startswith("DATA"):
                send("354 send it")
                body = []
                while True:
                    chunk = stream.readline()
                    if not chunk or chunk in (b".\r\n", b".\n"):
                        break
                    body.append(chunk.decode("utf-8", "replace"))
                envelope["data"] = "".join(body)
                envelope["authenticated"] = authenticated
                self.messages.append(envelope)
                send("250 queued")
            elif upper.startswith("QUIT"):
                send("221 bye")
                return connection
            elif upper.startswith("RSET"):
                send("250 ok")
            else:
                send("250 ok")

        return connection

    def last_message(self) -> Dict[str, Any]:
        return self.messages[-1]
