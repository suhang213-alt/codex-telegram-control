import io
import base64
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote

import artifact_results as artifacts
import miniapp_server as app


def response_item(role, message_id, content, *, phase="final", attachments=None):
    payload = {"type": "message", "role": role, "id": message_id,
               "phase": phase, "content": content}
    if attachments is not None:
        payload["attachments"] = attachments
    return {"type": "response_item", "payload": payload}


def write_rollout(path, records, *, trailing_newline=True):
    wire = b"\n".join(json.dumps(row, ensure_ascii=False).encode("utf-8") for row in records)
    if trailing_newline:
        wire += b"\n"
    path.write_bytes(wire)


class ArtifactDiscoveryTests(unittest.TestCase):
    def setUp(self):
        artifacts.clear_tickets_for_tests()

    def test_reads_all_assistant_results_and_preserves_every_artifact_kind(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = []
            for index in range(55):
                item = root / f"result-{index}.odd"
                item.write_bytes((f"row-{index}").encode())
                files.append(item)
            large = root / "Большой отчёт (финал).archive"
            with large.open("wb") as stream:
                stream.seek(20_000_123)
                stream.write(b"Z")
            structured = root / "structured.unknown"
            structured.write_bytes(b"structured")
            secret = root / ".env"
            secret.write_text("SHOULD_NOT_BE_READ", encoding="utf-8")
            user_only = root / "user-only.pdf"
            user_only.write_bytes(b"user")

            records = [
                response_item("user", "user-1", [{"type": "input_text", "text": f"[not output](<{user_only}>)"}]),
                {"type": "response_item", "payload": {"type": "function_call_output", "output": f"[tool](<{user_only}>)"}},
            ]
            records.extend(response_item("assistant", f"assistant-{i}", [{"type": "output_text", "text": f"[Файл {i}](<{path}>)"}]) for i, path in enumerate(files))
            records.extend([
                response_item("assistant", "assistant-large", [{"type": "output_text", "text":
                    f"[Большой файл](<{large}>)\n"
                    f"[Дубликат](<{files[0]}>)\n"
                    "[Сайт](https://example.test/report?id=7)\n"
                    "[Page](project-file:deliverable-42)\n"
                    "[Опасная ссылка](javascript:alert(1))\n"
                    f"[Секрет](<{secret}>)"}], attachments=[
                        {"name": "structured.unknown", "path": str(structured), "mimeType": "application/x-test"},
                    ]),
            ])
            rollout = root / "session.jsonl"
            write_rollout(rollout, records)

            result = artifacts.list_thread_artifacts("thread-1", rollout, base_dir=root)
            self.assertTrue(result["complete"])
            rows = result["artifacts"]
            self.assertEqual(len([row for row in rows if row["kind"] == "file"]), 57)
            self.assertEqual(len([row for row in rows if row.get("sourceRef") == str(files[0])]), 1)
            self.assertNotIn(str(user_only), {row.get("sourceRef") for row in rows})
            large_row = next(row for row in rows if row["name"] == large.name)
            self.assertGreater(large_row["size"], 20_000_000)
            self.assertEqual(large_row["sourceMessageId"], "assistant-large")
            structured_row = next(row for row in rows if row["name"] == structured.name)
            self.assertEqual(structured_row["mimeType"], "application/x-test")
            self.assertTrue(any(row["kind"] == "link" and row.get("url") == "https://example.test/report?id=7" for row in rows))
            self.assertTrue(any(row["kind"] == "link" and row.get("url") == "project-file:deliverable-42" for row in rows))
            self.assertTrue(any(row["kind"] == "unavailable" and row.get("reason") == "unsupported_reference" and row.get("sourceRef") == "javascript:alert(1)" for row in rows))
            self.assertTrue(any(row["kind"] == "unavailable" and row.get("reason") == "secret_blocked" and row.get("sourceRef") == str(secret) for row in rows))

    def test_reports_incomplete_rollout_and_missing_files_without_dropping_refs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = root / "missing (old).pdf"
            rollout = root / "session.jsonl"
            write_rollout(rollout, [response_item("assistant", "a", [{"type": "output_text", "text": f"[Старый](<{missing}>)"}])], trailing_newline=False)
            result = artifacts.list_thread_artifacts("thread-2", rollout, base_dir=root)
            self.assertFalse(result["complete"])
            self.assertEqual(result["reason"], "rollout_incomplete")
            self.assertEqual(result["artifacts"], [])

            write_rollout(rollout, [response_item("assistant", "a", [{"type": "output_text", "text": f"[Старый](<{missing}>)"}])])
            result = artifacts.list_thread_artifacts("thread-2", rollout, base_dir=root)
            self.assertTrue(result["complete"])
            self.assertEqual(result["artifacts"][0]["kind"], "unavailable")
            self.assertEqual(result["artifacts"][0]["reason"], "file_missing")

    def test_windows_absolute_markdown_link_with_leading_slash_is_resolved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "DESIGN (финал).md"
            path.write_bytes(b"design")
            codex_link = "/" + str(path).replace("\\", "/")
            rollout = root / "session.jsonl"
            write_rollout(rollout, [response_item(
                "assistant", "answer", [{"type": "output_text", "text": f"[DESIGN](<{codex_link}>)"}]
            )])
            result = artifacts.list_thread_artifacts("leading-drive", rollout, base_dir=root)
            self.assertTrue(result["complete"])
            self.assertEqual(len(result["artifacts"]), 1)
            self.assertEqual(result["artifacts"][0]["kind"], "file")
            self.assertEqual(result["artifacts"][0]["name"], path.name)

    def test_windows_percent_encoded_path_prefers_literal_then_decodes_with_line_suffix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            literal = root / "Отчёт%20финал.xlsx"
            decoded = root / "Отчёт финал.xlsx"
            literal.write_bytes(b"literal-percent")
            decoded.write_bytes(b"decoded-space")

            encoded_reference = "/" + str(literal).replace("\\", "/") + ":12"
            literal_row = artifacts.artifact_from_reference(
                "percent-thread", "literal", encoded_reference, "Отчёт", base_dir=root
            )
            self.assertEqual(literal_row["kind"], "file")
            self.assertEqual(literal_row["name"], literal.name)
            self.assertEqual(Path(artifacts._ARTIFACT_PATHS[literal_row["id"]]).read_bytes(),
                             b"literal-percent")

            literal.unlink()
            decoded_row = artifacts.artifact_from_reference(
                "percent-thread", "decoded", encoded_reference, "Отчёт", base_dir=root
            )
            self.assertEqual(decoded_row["kind"], "file")
            self.assertEqual(decoded_row["name"], decoded.name)
            self.assertEqual(Path(artifacts._ARTIFACT_PATHS[decoded_row["id"]]).read_bytes(),
                             b"decoded-space")

    def test_download_ticket_is_opaque_ranged_and_snapshots_original_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "Документ (1).binary"
            path.write_bytes(b"0123456789")
            rollout = root / "session.jsonl"
            write_rollout(rollout, [response_item("assistant", "answer", [{"type": "output_text", "text": f"[Документ](<{path}>)"}])])
            row = artifacts.list_thread_artifacts("thread-3", rollout, base_dir=root)["artifacts"][0]
            ticket = artifacts.issue_download_ticket(row, snapshot_dir=root / "ticket-cache",
                                                       now=1000, ttl_seconds=60)
            duplicate_ticket = artifacts.issue_download_ticket(row, snapshot_dir=root / "ticket-cache",
                                                                 now=1000, ttl_seconds=60)
            self.assertNotIn(str(path), ticket["url"])
            self.assertEqual(ticket["name"], path.name)
            self.assertEqual(duplicate_ticket["name"], path.name)
            self.assertEqual(len(list((root / "ticket-cache").glob("*.blob"))), 1)
            download = artifacts.open_download_ticket(ticket["url"].rsplit("/", 1)[-1], "bytes=2-5", now=1001)
            try:
                self.assertEqual((download.status, download.start, download.end, download.total), (206, 2, 5, 10))
                self.assertEqual(download.stream.read(download.length), b"2345")
            finally:
                download.close()

            replacement = root / "replacement"
            replacement.write_bytes(b"abcdefghij")
            os.replace(replacement, path)
            original = artifacts.open_download_ticket(ticket["url"].rsplit("/", 1)[-1], None, now=1002)
            try:
                self.assertEqual(original.stream.read(), b"0123456789")
            finally:
                original.close()

    def test_ticket_snapshot_streams_large_and_empty_files_and_range_forms(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "large.weird"
            with path.open("wb") as stream:
                stream.seek(20_000_123)
                stream.write(b"TAIL")
            row = artifacts.artifact_from_reference("large-thread", "m", str(path), "large")
            expected = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1_048_576):
                    expected.update(chunk)
            ticket = artifacts.issue_download_ticket(row, snapshot_dir=root / "cache")
            opened = artifacts.open_download_ticket(ticket["url"].rsplit("/", 1)[-1], None)
            actual = hashlib.sha256()
            try:
                while chunk := opened.stream.read(1_048_576):
                    actual.update(chunk)
            finally:
                opened.close()
            self.assertEqual(actual.digest(), expected.digest())

            small = root / "small.bin"
            small.write_bytes(b"0123456789")
            small_row = artifacts.artifact_from_reference("small-thread", "m", str(small), "small")
            small_ticket = artifacts.issue_download_ticket(small_row, snapshot_dir=root / "cache")
            token = small_ticket["url"].rsplit("/", 1)[-1]
            suffix = artifacts.open_download_ticket(token, "bytes=-4")
            try:
                self.assertEqual(suffix.stream.read(suffix.length), b"6789")
            finally:
                suffix.close()
            opened = artifacts.open_download_ticket(token, "bytes=7-")
            try:
                self.assertEqual(opened.stream.read(opened.length), b"789")
            finally:
                opened.close()
            with self.assertRaisesRegex(artifacts.RangeError, "invalid_range"):
                artifacts.open_download_ticket(token, "bytes=0-1,3-4")

            empty = root / "empty.zero"
            empty.write_bytes(b"")
            empty_row = artifacts.artifact_from_reference("empty-thread", "m", str(empty), "empty")
            empty_ticket = artifacts.issue_download_ticket(empty_row, snapshot_dir=root / "cache")
            empty_download = artifacts.open_download_ticket(empty_ticket["url"].rsplit("/", 1)[-1], None)
            try:
                self.assertEqual((empty_download.status, empty_download.length, empty_download.stream.read()),
                                 (200, 0, b""))
            finally:
                empty_download.close()

    def test_snapshot_rejects_replacement_between_validation_and_open(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "result.bin"
            path.write_bytes(b"first")
            signature = artifacts._stat_signature(path.stat())
            replacement = root / "replacement.bin"
            replacement.write_bytes(b"other")
            os.replace(replacement, path)
            with self.assertRaisesRegex(artifacts.TicketError, "artifact_changed"):
                artifacts._snapshot_for_ticket(path, root / "cache", signature)

    def test_expired_and_invalid_ranges_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x.bin"
            path.write_bytes(b"abc")
            row = artifacts.artifact_from_reference("thread", "m", str(path), "x")
            ticket = artifacts.issue_download_ticket(row, now=10, ttl_seconds=1)
            token = ticket["url"].rsplit("/", 1)[-1]
            with self.assertRaisesRegex(artifacts.TicketError, "ticket_expired"):
                artifacts.open_download_ticket(token, None, now=12)
            ticket = artifacts.issue_download_ticket(row, now=20, ttl_seconds=60)
            with self.assertRaisesRegex(artifacts.RangeError, "invalid_range"):
                artifacts.open_download_ticket(ticket["url"].rsplit("/", 1)[-1], "bytes=99-100", now=21)

    def test_structured_tool_media_is_preserved_but_arbitrary_tool_text_is_not_scanned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / "managed"
            ignored = root / "do-not-include.txt"
            ignored.write_text("ignored", encoding="utf-8")
            png = b"\x89PNG\r\n\x1a\nsynthetic"
            records = [
                {"type": "response_item", "payload": {"type": "function_call_output", "id": "tool-1", "output": [
                    {"type": "input_text", "text": f"[arbitrary tool text](<{ignored}>)"},
                    {"type": "resource_link", "name": "Canvas", "uri": "canvas:sheet-42"},
                    {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(png).decode()},
                ]}},
                {"type": "response_item", "payload": {"type": "image_generation_call", "id": "generated-1", "status": "completed",
                    "result": base64.b64encode(png + b"second").decode()}},
            ]
            rollout = root / "session.jsonl"
            write_rollout(rollout, records)
            result = artifacts.list_thread_artifacts("thread-media", rollout, base_dir=root, snapshot_dir=snapshot)
            self.assertTrue(result["complete"])
            self.assertNotIn(str(ignored), {row.get("sourceRef") for row in result["artifacts"]})
            self.assertTrue(any(row["kind"] == "link" and row.get("url") == "canvas:sheet-42" for row in result["artifacts"]))
            media = [row for row in result["artifacts"] if row["kind"] == "file"]
            self.assertEqual(len(media), 2)
            self.assertEqual({row["mimeType"] for row in media}, {"image/png"})
            self.assertTrue(all(row["size"] >= len(png) for row in media))

    def test_native_adapter_keeps_complete_flag_and_thread_level_resources(self):
        native = {"messages": [{"id": "answer", "role": "assistant", "text":
                  "[Page](page:abc) [Неизвестное](custom-protocol:opaque)"}],
                  "attachments": [{"name": "MCP resource", "uri": "library-file:item-7"}],
                  "complete": False, "reason": "native_cursor_unavailable"}
        result = artifacts.list_native_artifacts("remote-thread", native)
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "native_cursor_unavailable")
        self.assertEqual(len(result["artifacts"]), 3)
        self.assertTrue(any(row["kind"] == "unavailable" and row["reason"] == "unsupported_reference" for row in result["artifacts"]))
        self.assertEqual({row.get("url") for row in result["artifacts"] if row["kind"] == "link"},
                         {"page:abc", "library-file:item-7"})

    def test_native_image_generation_view_and_dynamic_media_use_documented_shapes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generated = root / "Сгенерировано (1).webp"
            viewed = root / "viewed.any"
            generated.write_bytes(b"generated")
            viewed.write_bytes(b"viewed")
            inline_png = base64.b64encode(b"inline-image").decode()
            native = {"messages": [
                {"id": "generated-saved", "role": "assistant", "content": [{
                    "type": "imageGeneration", "savedPath": str(generated),
                    "result": "data:image/png;base64," + inline_png,
                }]},
                {"id": "generated-inline", "role": "assistant", "content": [{
                    "type": "imageGeneration", "result": inline_png,
                }]},
                {"id": "view", "role": "assistant", "content": [{
                    "type": "imageView", "path": str(viewed),
                }]},
                {"id": "dynamic", "role": "assistant", "content": [{
                    "type": "inputImage", "imageUrl": "data:image/png;base64," + inline_png,
                }]},
            ], "complete": True}
            result = artifacts.list_native_artifacts("native-media", native, base_dir=root,
                                                     snapshot_dir=root / "snapshots")
            self.assertTrue(result["complete"])
            rows = result["artifacts"]
            # The same inline bytes emitted by two native items are deduplicated.
            self.assertEqual(len(rows), 3)
            self.assertEqual(sum(row["sourceMessageId"] == "generated-saved" for row in rows), 1)
            self.assertEqual({row["name"] for row in rows if row["sourceMessageId"] in {"generated-saved", "view"}},
                             {generated.name, viewed.name})
            self.assertTrue(all(row["kind"] == "file" for row in rows))


class ArtifactHttpTests(unittest.TestCase):
    def setUp(self):
        artifacts.clear_tickets_for_tests()

    def handler(self, path, *, method="GET", body=b"{}", headers=None):
        handler = object.__new__(app.Handler)
        handler.path = path
        handler.command = method
        handler.headers = headers or {"Content-Type": "application/json", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.respond = Mock()
        handler.authenticated = Mock(return_value=True)
        return handler

    def wire_response(self, path, *, method="GET", headers=None):
        """Exercise the real HTTP parser and serializer without opening sockets."""
        request_lines = [f"{method} {path} HTTP/1.0", "Host: localhost"]
        request_lines.extend(f"{key}: {value}" for key, value in (headers or {}).items())
        connection = Mock()
        connection.makefile.return_value = io.BytesIO(
            ("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii"))
        sent = bytearray()
        connection.sendall.side_effect = sent.extend
        with patch.object(app.Handler, "log_message"):
            app.Handler(connection, ("127.0.0.1", 1), Mock())
        head, body = bytes(sent).split(b"\r\n\r\n", 1)
        status, header_bytes = head.split(b"\r\n", 1)
        return int(status.split()[1]), BytesParser().parsebytes(header_bytes), body

    def test_text_download_is_binary_attachment_without_changing_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            for extension in (".txt", ".md", ".csv", ".html"):
                with self.subTest(extension=extension):
                    path = Path(temp) / ("Отчёт" + extension)
                    original = "Результат: проверка кириллицы.\r\n".encode("utf-8")
                    path.write_bytes(original)
                    row = artifacts.artifact_from_reference("thread", "m", str(path), "")
                    ticket = artifacts.issue_download_ticket(row)
                    status, headers, body = self.wire_response(ticket["url"])
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Content-Type"], "application/octet-stream")
                    self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                    self.assertEqual(headers["Content-Length"], str(len(original)))
                    self.assertEqual(body, original)

    def test_download_names_support_ascii_clients_and_preserve_unicode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Отчёт; 100% [А].md"
            path.write_bytes(b"synthetic")
            row = artifacts.artifact_from_reference("thread", "m", str(path), "")
            ticket = artifacts.issue_download_ticket(row)
            status, headers, body = self.wire_response(ticket["url"])
            self.assertEqual(status, 200)
            disposition = headers["Content-Disposition"]
            self.assertRegex(disposition, r'^attachment; filename="[A-Za-z0-9._-]+\.md"; filename\*=UTF-8\'\'')
            self.assertTrue(disposition.isascii())
            self.assertEqual(unquote(disposition.split("filename*=UTF-8''", 1)[1]), path.name)
            self.assertEqual(body, b"synthetic")

    def test_download_head_preserves_headers_and_range_without_body(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Результат.txt"
            path.write_bytes(b"abcdefghij")
            row = artifacts.artifact_from_reference("thread", "m", str(path), "")
            ticket = artifacts.issue_download_ticket(row)
            for request_headers, expected_status, expected_length in (
                    ({}, 200, "10"), ({"Range": "bytes=3-6"}, 206, "4")):
                with self.subTest(headers=request_headers):
                    get_status, get_headers, get_body = self.wire_response(
                        ticket["url"], headers=request_headers)
                    status, headers, body = self.wire_response(
                        ticket["url"], method="HEAD", headers=request_headers)
                    self.assertEqual(status, expected_status)
                    self.assertEqual(status, get_status)
                    for name in ("Content-Type", "Content-Disposition", "Content-Length",
                                 "Content-Range", "Accept-Ranges", "Cache-Control",
                                 "X-Content-Type-Options", "Access-Control-Allow-Origin"):
                        self.assertEqual(headers[name], get_headers[name], name)
                    self.assertEqual(headers["Content-Length"], expected_length)
                    self.assertEqual(headers["Access-Control-Allow-Origin"], "https://web.telegram.org")
                    self.assertEqual(body, b"")
                    self.assertEqual(len(get_body), int(expected_length))

    def test_download_head_rejects_invalid_ticket_without_body(self):
        status, _, body = self.wire_response("/api/artifact-download/" + "x" * 43, method="HEAD")
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")

    def test_head_does_not_dispatch_other_get_routes(self):
        with patch.object(app.Handler, "do_GET", side_effect=AssertionError("HEAD must not dispatch GET")):
            status, _, body = self.wire_response("/api/threads", method="HEAD")
        self.assertEqual(status, 501)
        self.assertEqual(body, b"")

    def test_list_ticket_and_unauthenticated_range_download(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            file_path = root / "Результат [A].noext"
            file_path.write_bytes(b"abcdefghij")
            rollout = root / "session.jsonl"
            write_rollout(rollout, [response_item("assistant", "answer", [{"type": "output_text", "text": f"[Файл](<{file_path}>)"}])])
            thread = {"id": "thread-1", "kind": "codex", "cwd": str(root)}
            with patch.object(app, "thread_rows", return_value=[thread]), patch.object(app.bridge, "thread_in_scope", return_value=True), patch.object(app, "local_codex_rollout", return_value=rollout), patch.object(app, "ARTIFACT_FILES", root / "ticket-cache"):
                listing = self.handler("/api/threads/thread-1/artifacts")
                listing.do_GET()
                payload = listing.respond.call_args.args[1]
                self.assertTrue(payload["complete"])
                artifact_id = payload["artifacts"][0]["id"]

                ticket_handler = self.handler(f"/api/threads/thread-1/artifacts/{artifact_id}/download-ticket", method="POST")
                ticket_handler.do_POST()
                ticket_payload = ticket_handler.respond.call_args.args[1]
                self.assertRegex(ticket_payload["url"], r"^/api/artifact-download/[A-Za-z0-9_-]+$")

            download = self.handler(ticket_payload["url"], headers={"Range": "bytes=3-6"})
            download.authenticated = Mock(side_effect=AssertionError("ticket GET must not require initData"))
            download.do_GET()
            self.assertEqual(download.send_response.call_args.args[0], 206)
            self.assertEqual(download.wfile.getvalue(), b"defg")
            headers_sent = dict(call.args for call in download.send_header.call_args_list)
            self.assertEqual(headers_sent["Content-Range"], "bytes 3-6/10")
            self.assertIn("UTF-8''", headers_sent["Content-Disposition"])
            self.assertEqual(headers_sent["Access-Control-Allow-Origin"], "https://web.telegram.org")

    def test_ticket_requires_empty_body_owner_and_current_scope(self):
        artifact_id = "a" * 32
        handler = self.handler(f"/api/threads/thread-1/artifacts/{artifact_id}/download-ticket", method="POST", body=b'{"path":"C:/forged"}')
        with patch.object(app, "thread_rows", return_value=[{"id": "thread-1", "kind": "codex", "cwd": "C:/work"}]), patch.object(app.bridge, "thread_in_scope", return_value=True):
            handler.do_POST()
        self.assertEqual(handler.respond.call_args.args[0], 400)

        handler = self.handler(f"/api/threads/thread-1/artifacts/{artifact_id}/download-ticket", method="POST")
        handler.authenticated = Mock(return_value=False)
        handler.do_POST()
        handler.respond.assert_not_called()

        handler = self.handler(f"/api/threads/thread-1/artifacts/{artifact_id}/download-ticket", method="POST")
        with patch.object(app, "thread_rows", return_value=[{"id": "thread-1", "kind": "codex", "cwd": "C:/work"}]), patch.object(app.bridge, "thread_in_scope", return_value=False):
            handler.do_POST()
        self.assertEqual(handler.respond.call_args.args[0], 400)

    def test_native_listing_uses_full_loader_with_remote_host_and_completeness(self):
        thread = {"id": "00000000-0000-4000-8000-000000000001", "kind": "codex",
                  "hostId": "remote-host", "cwd": "C:/remote-work"}
        native = {"messages": [{"id": "answer", "role": "assistant",
                                 "text": "[Page](page:result-1)"}],
                  "complete": False, "reason": "native_output_details_unavailable"}
        with patch.object(app, "thread_rows", return_value=[thread]), \
                patch.object(app.bridge, "thread_in_scope", return_value=True), \
                patch.object(app, "local_codex_rollout", return_value=None), \
                patch("artifact_native_loader.load_native_artifacts", return_value=native) as loader:
            result = app.thread_artifacts(thread["id"])
        loader.assert_called_once_with(thread["id"], host_id="remote-host")
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "native_output_details_unavailable")
        self.assertEqual(result["artifacts"][0]["url"], "page:result-1")


if __name__ == "__main__":
    unittest.main()
