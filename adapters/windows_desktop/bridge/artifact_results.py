"""Server-derived artifact discovery and short-lived local download tickets.

Only user-visible assistant messages are inspected.  Tool output and user input
are deliberately outside this module's trust boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import stat as stat_module
import threading
import time
from urllib.parse import unquote, urlsplit


TICKET_TTL_SECONDS = 600
_TICKETS: dict[str, dict] = {}
_TICKET_LOCK = threading.Lock()
_ARTIFACT_PATHS: dict[str, str] = {}
_SNAPSHOT_LOCK = threading.Lock()
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_KNOWN_LINK_SCHEMES = frozenset({"http", "https", "page", "project-file", "library-file", "visualize", "canvas", "artifact"})
_SECRET_NAMES = frozenset({
    ".env", "state.bin", "credentials.json", "credential.json", "secrets.json",
    "secret.json", "service_account.json", "service-account.json", "id_rsa",
    "id_ed25519", "known_hosts", "authorized_keys", "настройки ии.json",
})
_SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".azure", ".kube"})


class TicketError(ValueError):
    pass


class RangeError(ValueError):
    def __init__(self, code: str, total: int | None = None):
        super().__init__(code)
        self.total = total


@dataclass
class Download:
    stream: object
    status: int
    start: int
    end: int
    total: int
    name: str
    mime_type: str

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def close(self) -> None:
        self.stream.close()


def clear_tickets_for_tests() -> None:
    with _TICKET_LOCK:
        _TICKETS.clear()
        _ARTIFACT_PATHS.clear()


def _artifact_id(thread_id: str, normalized: str) -> str:
    return hashlib.sha256((thread_id + "\0" + normalized).encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _secret_path(path: Path) -> bool:
    names = [part.casefold() for part in path.parts]
    if any(part in _SECRET_DIRS for part in names):
        return True
    name = path.name.casefold()
    if name in _SECRET_NAMES or name.startswith(".env."):
        return True
    if name.endswith((".pem", ".key", ".p12", ".pfx", ".dpapi")):
        return True
    return bool(re.search(r"(?:^|[-_.])(credential|credentials|service[-_]?account|bot[-_]?token|api[-_]?key)(?:[-_.]|$)", name))


def _clean_reference(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    return value or None


def _as_path(reference: str, base_dir: Path | None) -> tuple[Path | None, str | None]:
    raw = reference
    decode_fallback = True
    # Codex file links on Windows may render an absolute drive path as /C:/… .
    if re.match(r"^/[A-Za-z]:[\\/]", reference):
        reference = reference[1:]
    if reference.casefold().startswith("file://"):
        decode_fallback = False
        parsed = urlsplit(reference)
        if parsed.netloc and parsed.netloc.casefold() not in {"", "localhost"}:
            return None, "unsupported_reference"
        reference = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:/", reference):
            reference = reference[1:]
    elif reference.casefold().startswith("sandbox:"):
        decode_fallback = False
        reference = unquote(reference.split(":", 1)[1])
    if reference.startswith(("\\\\", "//", "\\?\\", "\\.\\")):
        return None, "unsupported_reference"
    try:
        references = [reference]
        decoded = unquote(reference) if decode_fallback else reference
        if decoded != reference:
            references.append(decoded)
        relative_without_workspace = False
        for value in references:
            candidate = Path(value)
            if not candidate.is_absolute() and not _WINDOWS_ABSOLUTE.match(value):
                if base_dir is None:
                    relative_without_workspace = True
                    continue
                candidate = base_dir / candidate
            # Exact path wins, including a real filename containing "%20".
            # Only then interpret a trailing :line marker from Codex links.
            variants = [candidate]
            stripped = re.sub(r":\d+$", "", str(candidate))
            if stripped != str(candidate):
                variants.append(Path(stripped))
            for variant in variants:
                if variant.exists():
                    return variant.resolve(strict=True), None
        if relative_without_workspace:
            return None, "relative_without_workspace"
        raise FileNotFoundError(reference)
    except (OSError, RuntimeError, ValueError):
        # Preserve the original server-derived reference even when unavailable.
        return None, "file_missing" if (Path(raw).suffix or "/" in raw or "\\" in raw) else "unsupported_reference"


def artifact_from_reference(thread_id: str, message_id: str, reference, label="", *, base_dir: Path | str | None = None,
                            mime_type: str | None = None, declared_size=None) -> dict | None:
    ref = _clean_reference(reference)
    if ref is None:
        return None
    label = str(label or "").strip()
    base = Path(base_dir) if base_dir is not None else None
    windows_path = bool(_WINDOWS_ABSOLUTE.match(ref) or ref.startswith(("\\\\", "//", "\\?\\", "\\.\\")))
    scheme = "" if windows_path else urlsplit(ref).scheme.casefold()
    if scheme in {"http", "https"}:
        parsed = urlsplit(ref)
        kind, reason = ("link", None) if parsed.netloc else ("unavailable", "invalid_reference")
        name = label or Path(unquote(parsed.path)).name or parsed.netloc or "Ссылка"
        normalized = ref
        result = {"id": _artifact_id(thread_id, normalized), "name": name, "kind": kind,
                  "sourceMessageId": message_id, "sourceRef": ref}
        if kind == "link":
            result["url"] = ref
        else:
            result["reason"] = reason
        return result
    if scheme in _KNOWN_LINK_SCHEMES:
        name = label or ref.split(":", 1)[-1].rsplit("/", 1)[-1] or scheme
        return {"id": _artifact_id(thread_id, ref), "name": name, "kind": "link",
                "sourceMessageId": message_id, "sourceRef": ref, "url": ref}
    if scheme and scheme not in {"file", "sandbox"}:
        return {"id": _artifact_id(thread_id, ref), "name": label or ref[:150], "kind": "unavailable",
                "sourceMessageId": message_id, "sourceRef": ref, "reason": "unsupported_reference"}
    path, error = _as_path(ref, base)
    if path is None:
        name = label or Path(ref.replace("\\", "/")).name or ref[:150]
        return {"id": _artifact_id(thread_id, ref), "name": name, "kind": "unavailable",
                "sourceMessageId": message_id, "sourceRef": ref, "reason": error or "file_missing"}
    normalized = os.path.normcase(str(path))
    if _secret_path(path):
        return {"id": _artifact_id(thread_id, normalized), "name": path.name, "kind": "unavailable",
                "sourceMessageId": message_id, "sourceRef": ref, "reason": "secret_blocked"}
    try:
        stat = path.stat()
    except OSError:
        stat = None
    if stat is None or not path.is_file():
        return {"id": _artifact_id(thread_id, normalized), "name": path.name, "kind": "unavailable",
                "sourceMessageId": message_id, "sourceRef": ref, "reason": "not_a_file"}
    guessed = mime_type if isinstance(mime_type, str) and mime_type else mimetypes.guess_type(path.name)[0]
    artifact_id = _artifact_id(thread_id, normalized)
    with _TICKET_LOCK:
        _ARTIFACT_PATHS[artifact_id] = str(path)
    return {"id": artifact_id, "name": path.name, "kind": "file",
            "size": stat.st_size, "mimeType": guessed or "application/octet-stream",
            "sourceMessageId": message_id, "sourceRef": ref}


def _markdown_links(text: str):
    """Yield (label, destination) for inline Markdown links/images."""
    index, length = 0, len(text)
    while index < length:
        bang = text[index] == "!" and index + 1 < length and text[index + 1] == "["
        if text[index] != "[" and not bang:
            index += 1
            continue
        open_index = index + 1 if bang else index
        close = open_index + 1
        while close < length:
            if text[close] == "]" and text[close - 1] != "\\":
                break
            close += 1
        if close >= length or close + 1 >= length or text[close + 1] != "(":
            index = open_index + 1
            continue
        label = text[open_index + 1:close]
        pos = close + 2
        while pos < length and text[pos].isspace():
            pos += 1
        if pos < length and text[pos] == "<":
            end = text.find(">", pos + 1)
            if end < 0:
                index = close + 1
                continue
            destination = text[pos + 1:end]
            tail = end + 1
            while tail < length and text[tail] != ")":
                tail += 1
        else:
            start, depth, tail = pos, 0, pos
            while tail < length:
                char = text[tail]
                if char == "(" and (tail == start or text[tail - 1] != "\\"):
                    depth += 1
                elif char == ")" and (tail == start or text[tail - 1] != "\\"):
                    if depth == 0:
                        break
                    depth -= 1
                elif char.isspace() and depth == 0:
                    break
                tail += 1
            destination = text[start:tail]
            while tail < length and text[tail] != ")":
                tail += 1
        if tail < length and destination:
            yield label, destination.replace("\\)", ")").replace("\\(", "(")
            index = tail + 1
        else:
            index = close + 1


def _explicit_references(item: dict, *, allow_text=True):
    if isinstance(item.get("text"), str):
        if allow_text:
            yield from ((ref, label, None, None) for label, ref in _markdown_links(item["text"]))
    for part in item.get("content") or []:
        if not isinstance(part, dict):
            continue
        if allow_text and part.get("type") in {"input_text", "output_text", "text"} and isinstance(part.get("text"), str):
            yield from ((ref, label, None, None) for label, ref in _markdown_links(part["text"]))
        part_type = str(part.get("type") or "").casefold()
        if part_type == "resource" and isinstance(part.get("resource"), dict):
            resource = part["resource"]
            ref = resource.get("uri")
            if ref and not resource.get("blob"):
                yield ref, resource.get("name") or resource.get("title") or "Ресурс", resource.get("mimeType") or resource.get("mime_type"), resource.get("size")
        known_reference_type = part_type in {
            "resource_link", "file", "image", "image_url", "audio", "video",
            "localimage", "localaudio", "localvideo", "attachment", "inputimage",
            "inputaudio", "input_image", "input_audio", "imagegeneration", "imageview",
        }
        has_explicit_reference = any(isinstance(part.get(key), str) and part.get(key) for key in (
            "savedPath", "path", "url", "uri", "sourceRef", "image_url", "imageUrl",
            "audio_url", "audioUrl", "video_url", "videoUrl",
        ))
        if known_reference_type or has_explicit_reference:
            ref = (part.get("savedPath") or part.get("path") or part.get("url") or part.get("uri") or part.get("sourceRef") or
                   part.get("image_url") or part.get("imageUrl") or part.get("audio_url") or part.get("audioUrl") or
                   part.get("video_url") or part.get("videoUrl"))
            if isinstance(ref, str) and ref.casefold().startswith("data:"):
                # Inline media is materialized below. Treating a data URL as a
                # path would create a false unsupported-reference row.
                ref = None
            if ref:
                yield ref, part.get("name") or part.get("title") or "", part.get("mimeType") or part.get("mime_type"), part.get("size")
            elif part.get("file_id") or part.get("fileId"):
                value = part.get("file_id") or part.get("fileId")
                yield "file_id:" + str(value), part.get("name") or "Файл", part.get("mimeType") or part.get("mime_type"), part.get("size")
    for attachment in item.get("attachments") or item.get("files") or []:
        if not isinstance(attachment, dict):
            continue
        ref = attachment.get("path") or attachment.get("url") or attachment.get("uri") or attachment.get("sourceRef")
        if ref:
            yield ref, attachment.get("name") or attachment.get("title") or "", attachment.get("mimeType") or attachment.get("mime_type"), attachment.get("size")


def _inline_values(item: dict):
    for part in item.get("content") or []:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "").casefold()
        if part_type == "resource" and isinstance(part.get("resource"), dict):
            resource = part["resource"]
            if isinstance(resource.get("blob"), str):
                yield resource["blob"], resource.get("name") or resource.get("title") or "Ресурс", resource.get("mimeType") or resource.get("mime_type")
        data = part.get("data")
        if isinstance(data, str) and part_type in {"image", "audio", "video", "file", "attachment"}:
            yield data, part.get("name") or part.get("title") or part_type, part.get("mimeType") or part.get("mime_type")
        result = part.get("result")
        if (part_type == "imagegeneration" and isinstance(result, str) and result
                and not (part.get("savedPath") or part.get("path"))):
            yield result, part.get("name") or "Сгенерированное изображение", part.get("mimeType") or "image/png"
        for key, default_mime in (("image_url", "image/png"), ("imageUrl", "image/png"),
                                  ("audio_url", "audio/mpeg"), ("audioUrl", "audio/mpeg"),
                                  ("video_url", "video/mp4"), ("videoUrl", "video/mp4")):
            value = part.get(key)
            if isinstance(value, str) and value.casefold().startswith("data:"):
                lowered = key.casefold()
                default_name = "Изображение" if "image" in lowered else ("Видео" if "video" in lowered else "Аудио")
                yield value, part.get("name") or default_name, part.get("mimeType") or default_mime


def _materialize_inline(thread_id: str, message_id: str, data: str, label: str, mime_type: str | None,
                        snapshot_dir: Path | str | None) -> dict:
    header, encoded = "", data
    if data.casefold().startswith("data:"):
        if "," not in data:
            encoded = ""
        else:
            header, encoded = data.split(",", 1)
            if ";base64" not in header.casefold():
                encoded = ""
            detected = header[5:].split(";", 1)[0]
            if detected:
                mime_type = detected
    digest = hashlib.sha256()
    for offset in range(0, len(encoded), 1_048_576):
        digest.update(encoded[offset:offset + 1_048_576].encode("ascii", "replace"))
    identity = "inline:" + digest.hexdigest()
    artifact_id = _artifact_id(thread_id, identity)
    clean_name = Path(str(label or "").replace("\\", "/")).name
    extension = mimetypes.guess_extension(mime_type or "") or ".bin"
    if not clean_name or "." not in clean_name:
        clean_name = (clean_name or "Артефакт") + extension
    base = {"id": artifact_id, "name": clean_name, "sourceMessageId": message_id, "sourceRef": identity}
    if snapshot_dir is None or not encoded:
        return {**base, "kind": "unavailable", "reason": "inline_artifact_unavailable"}
    target_dir = Path(snapshot_dir)
    target = target_dir / (digest.hexdigest() + extension)
    try:
        with _SNAPSHOT_LOCK:
            target_dir.mkdir(parents=True, exist_ok=True)
            if not target.is_file():
                temp = target.with_suffix(target.suffix + ".tmp-" + secrets.token_hex(4))
                try:
                    with temp.open("xb") as output:
                        for offset in range(0, len(encoded), 1_048_576):
                            chunk = encoded[offset:offset + 1_048_576]
                            # Non-final base64 chunks must end on a quartet.
                            if offset + len(chunk) < len(encoded):
                                trim = len(chunk) % 4
                                if trim:
                                    chunk = chunk[:-trim]
                            output.write(base64.b64decode(chunk, validate=True))
                    temp.replace(target)
                finally:
                    if temp.exists():
                        temp.unlink()
        stat = target.stat()
    except (OSError, ValueError, binascii.Error):
        return {**base, "kind": "unavailable", "reason": "inline_artifact_invalid"}
    with _TICKET_LOCK:
        _ARTIFACT_PATHS[artifact_id] = str(target.resolve(strict=True))
    return {**base, "kind": "file", "size": stat.st_size,
            "mimeType": mime_type or "application/octet-stream"}


def _collect_message(thread_id: str, item: dict, base_dir, found: list, seen: set, *, snapshot_dir=None,
                     allow_text=True, force_assistant=False) -> None:
    if not force_assistant and item.get("role") not in {"assistant", "agent", "agentMessage"}:
        return
    message_id = str(item.get("id") or item.get("itemId") or "unknown")
    for reference, label, mime_type, declared_size in _explicit_references(item, allow_text=allow_text):
        artifact = artifact_from_reference(thread_id, message_id, reference, label, base_dir=base_dir,
                                           mime_type=mime_type, declared_size=declared_size)
        if artifact is None or artifact["id"] in seen:
            continue
        seen.add(artifact["id"])
        found.append(artifact)
    for data, label, mime_type in _inline_values(item):
        artifact = _materialize_inline(thread_id, message_id, data, label, mime_type, snapshot_dir)
        if artifact["id"] in seen:
            continue
        seen.add(artifact["id"])
        found.append(artifact)


def list_message_artifacts(thread_id: str, messages, *, base_dir=None, snapshot_dir=None, complete=False,
                           reason="native_history_partial") -> dict:
    found, seen = [], set()
    if not isinstance(messages, list):
        return {"artifacts": [], "complete": False, "reason": reason}
    for item in messages:
        if isinstance(item, dict):
            _collect_message(thread_id, item, base_dir, found, seen, snapshot_dir=snapshot_dir)
    result = {"artifacts": found, "complete": bool(complete)}
    if not complete:
        result["reason"] = reason
    return result


def list_native_artifacts(thread_id: str, native_result: dict, *, base_dir=None, snapshot_dir=None) -> dict:
    if not isinstance(native_result, dict):
        return {"artifacts": [], "complete": False, "reason": "native_history_unavailable"}
    messages = list(native_result.get("messages") or [])
    attachments = native_result.get("attachments") or []
    if attachments:
        messages.append({"id": "native:" + thread_id, "role": "assistant", "attachments": attachments})
    complete = native_result.get("complete") is True
    reason = native_result.get("reason") if isinstance(native_result.get("reason"), str) else "native_history_partial"
    return list_message_artifacts(thread_id, messages, base_dir=base_dir, snapshot_dir=snapshot_dir,
                                  complete=complete, reason=reason)


def list_thread_artifacts(thread_id: str, rollout_path: Path | str | None, *, base_dir=None, snapshot_dir=None) -> dict:
    if rollout_path is None:
        return {"artifacts": [], "complete": False, "reason": "rollout_unavailable"}
    path = Path(rollout_path)
    found, seen, complete, reason = [], set(), True, None
    try:
        with path.open("rb") as stream:
            while True:
                raw = stream.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    complete, reason = False, "rollout_incomplete"
                    break
                try:
                    record = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    complete, reason = False, "rollout_parse_error"
                    continue
                if record.get("type") != "response_item":
                    continue
                item = record.get("payload") or {}
                item_type = item.get("type")
                if item_type == "message" and item.get("role") == "assistant":
                    if item.get("phase") not in {None, "commentary", "final", "final_answer"}:
                        continue
                    _collect_message(thread_id, item, base_dir, found, seen, snapshot_dir=snapshot_dir)
                elif item_type in {"function_call_output", "custom_tool_call_output"} and isinstance(item.get("output"), list):
                    # Structured media/resources are user-visible artifacts;
                    # arbitrary textual tool output is intentionally ignored.
                    _collect_message(thread_id, {"id": item.get("id"), "content": item["output"]}, base_dir,
                                     found, seen, snapshot_dir=snapshot_dir, allow_text=False, force_assistant=True)
                elif item_type == "image_generation_call" and isinstance(item.get("result"), str):
                    _collect_message(thread_id, {"id": item.get("id"), "content": [{"type": "image", "data": item["result"], "mimeType": "image/png", "name": "Сгенерированное изображение.png"}]},
                                     base_dir, found, seen, snapshot_dir=snapshot_dir, allow_text=False,
                                     force_assistant=True)
    except OSError:
        return {"artifacts": [], "complete": False, "reason": "rollout_unavailable"}
    result = {"artifacts": found, "complete": complete}
    if not complete:
        result["reason"] = reason
    return result


def find_artifact(result: dict, artifact_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{32}", str(artifact_id or "")):
        return None
    return next((row for row in result.get("artifacts", []) if row.get("id") == artifact_id), None)


def _stat_signature(stat) -> tuple:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _snapshot_for_ticket(path: Path, snapshot_dir: Path | str, expected_signature: tuple) -> tuple[Path, os.stat_result]:
    """Copy one stable source version without loading it into memory."""
    target_dir = Path(snapshot_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    temp = target_dir / ("ticket-" + secrets.token_hex(16) + ".tmp")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source, temp.open("xb") as output:
            before = os.fstat(source.fileno())
            if not stat_module.S_ISREG(before.st_mode):
                raise TicketError("artifact_unavailable")
            if _stat_signature(before) != tuple(expected_signature):
                raise TicketError("artifact_changed")
            copied = 0
            while True:
                chunk = source.read(1_048_576)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            after = os.fstat(source.fileno())
        if _stat_signature(before) != _stat_signature(after) or copied != before.st_size:
            raise TicketError("artifact_changed")
        # Content-addressing reuses exact duplicate bytes while preserving the
        # user-facing original name only in the ticket metadata.
        snapshot = target_dir / (digest.hexdigest() + ".blob")
        with _SNAPSHOT_LOCK:
            if snapshot.exists():
                existing = hashlib.sha256()
                with snapshot.open("rb") as cached:
                    while True:
                        chunk = cached.read(1_048_576)
                        if not chunk:
                            break
                        existing.update(chunk)
                if existing.digest() != digest.digest() or snapshot.stat().st_size != copied:
                    raise TicketError("artifact_changed")
                temp.unlink()
            else:
                temp.replace(snapshot)
        return snapshot.resolve(strict=True), snapshot.stat()
    except TicketError:
        raise
    except OSError:
        raise TicketError("artifact_unavailable") from None
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def issue_download_ticket(artifact: dict, *, snapshot_dir: Path | str | None = None,
                          now: float | None = None, ttl_seconds: int = TICKET_TTL_SECONDS) -> dict:
    if not isinstance(artifact, dict) or artifact.get("kind") != "file":
        raise TicketError("artifact_not_downloadable")
    with _TICKET_LOCK:
        registered_path = _ARTIFACT_PATHS.get(str(artifact.get("id") or ""))
    path, error = _as_path(registered_path or str(artifact.get("sourceRef") or ""), None)
    if path is None or error or _secret_path(path):
        raise TicketError("artifact_unavailable")
    try:
        stat = path.stat()
    except OSError:
        raise TicketError("artifact_unavailable") from None
    if not path.is_file():
        raise TicketError("artifact_unavailable")
    if snapshot_dir is not None:
        path, stat = _snapshot_for_ticket(path, snapshot_dir, _stat_signature(stat))
    stamp = time.time() if now is None else float(now)
    token = secrets.token_urlsafe(32)
    record = {"path": str(path), "name": artifact.get("name") or path.name,
              "mimeType": artifact.get("mimeType") or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
              "signature": _stat_signature(stat), "expires": stamp + max(1, min(int(ttl_seconds), 3600)),
              "artifactId": artifact.get("id")}
    with _TICKET_LOCK:
        expired = [key for key, value in _TICKETS.items() if value["expires"] < stamp]
        for key in expired:
            _TICKETS.pop(key, None)
        _TICKETS[token] = record
    return {"url": "/api/artifact-download/" + token, "name": record["name"], "expiresAt": int(record["expires"])}


def _range(value: str | None, total: int) -> tuple[int, int, int]:
    if not value:
        return 200, 0, total - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not (match[1] or match[2]) or total <= 0:
        raise RangeError("invalid_range", total)
    if match[1]:
        start = int(match[1])
        end = int(match[2]) if match[2] else total - 1
    else:
        suffix = int(match[2])
        if suffix <= 0:
            raise RangeError("invalid_range", total)
        start, end = max(0, total - suffix), total - 1
    if start >= total or end < start:
        raise RangeError("invalid_range", total)
    return 206, start, min(end, total - 1)


def open_download_ticket(token: str, range_header: str | None, *, now: float | None = None) -> Download:
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", str(token or "")):
        raise TicketError("ticket_invalid")
    stamp = time.time() if now is None else float(now)
    with _TICKET_LOCK:
        record = dict(_TICKETS.get(token) or {})
    if not record:
        raise TicketError("ticket_unknown")
    if record["expires"] < stamp:
        with _TICKET_LOCK:
            _TICKETS.pop(token, None)
        raise TicketError("ticket_expired")
    try:
        stream = open(record["path"], "rb")
        stat = os.fstat(stream.fileno())
    except OSError:
        raise TicketError("artifact_missing") from None
    if _stat_signature(stat) != tuple(record["signature"]):
        stream.close()
        raise TicketError("artifact_changed")
    try:
        status, start, end = _range(range_header, stat.st_size)
    except Exception:
        stream.close()
        raise
    stream.seek(start)
    return Download(stream=stream, status=status, start=start, end=end, total=stat.st_size,
                    name=record["name"], mime_type=record["mimeType"])
