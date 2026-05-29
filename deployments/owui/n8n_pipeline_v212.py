"""
title: n8n Pipeline v2 (Streaming + Redis, Concurrency-Hardened)
author: TigerAI Morris Lu
version: 2.13.0
date: 2026-05-25
description: Streaming + Redis + session lock.
             v2.13.0 - add 'app_name' (+ optional 'collection_name') valve; pipe reports its app
               identity so the backend resolves the collection (single source = Tab04). This lets
               MANY apps share ONE streaming webhook (parity with non-streaming v0.5.4).
             v2.12.0 concurrency hardening (code review):
             - Lock ownership: per-acquire UUID token + compare-and-delete (Lua) on release
               (a slow request can no longer delete a newer request's lock).
             - Lock logic consolidated into _acquire_lock/_release_lock (no duplicate inline loop).
             - Safe last-message content extraction (multimodal list / missing / non-string).
             - Batch response empty-list guarded; accept any 2xx (incl. 204 empty body).
             - NDJSON stream: flush trailing buffer (last chunk without newline no longer dropped).
             - decrypt() returns "" + warns on no-key/decrypt-fail (never forwards a bad token).
             - Authorization header match is case-insensitive.
             - Redis reconnect backoff (30s) to avoid retry storms / log noise.
"""

from typing import Optional, Callable, Awaitable, Any, Dict, AsyncGenerator, List
from pydantic import BaseModel, Field, GetCoreSchemaHandler
from cryptography.fernet import Fernet, InvalidToken
import asyncio
import time
import uuid
import aiohttp
import os
import base64
import hashlib
import logging
import json
from pydantic_core import core_schema

PIPELINE_VERSION = "2.13.0"
PIPELINE_DATE = "2026-05-25"

_log = logging.getLogger("n8n_pipe_v2")

# Lua: delete the lock only if we still own it (compare-and-delete).
_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class EncryptedStr(str):
    """A string type that automatically handles encryption/decryption"""

    @classmethod
    def _get_encryption_key(cls) -> Optional[bytes]:
        secret = os.getenv("WEBUI_SECRET_KEY")
        if not secret:
            return None
        hashed_key = hashlib.sha256(secret.encode()).digest()
        return base64.urlsafe_b64encode(hashed_key)

    @classmethod
    def encrypt(cls, value: str) -> str:
        if not value or value.startswith("encrypted:"):
            return value
        key = cls._get_encryption_key()
        if not key:
            return value
        f = Fernet(key)
        encrypted = f.encrypt(value.encode())
        return f"encrypted:{encrypted.decode()}"

    @classmethod
    def decrypt(cls, value: str) -> str:
        # Plain (unencrypted) values pass through unchanged.
        if not value or not value.startswith("encrypted:"):
            return value
        # v2.12.0: encrypted but undecryptable -> "" (never forward ciphertext as a token).
        key = cls._get_encryption_key()
        if not key:
            _log.warning("[n8n_pipe_v2] encrypted token but WEBUI_SECRET_KEY unset -> empty token")
            return ""
        try:
            encrypted_part = value[len("encrypted:"):]
            return Fernet(key).decrypt(encrypted_part.encode()).decode()
        except (InvalidToken, Exception):
            _log.warning("[n8n_pipe_v2] token decrypt failed -> empty token")
            return ""

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.union_schema(
            [
                core_schema.is_instance_schema(cls),
                core_schema.chain_schema(
                    [
                        core_schema.str_schema(),
                        core_schema.no_info_plain_validator_function(
                            lambda value: cls(cls.encrypt(value) if value else value)
                        ),
                    ]
                ),
            ],
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda instance: str(instance)
            ),
        )

    def get_decrypted(self) -> str:
        return self.decrypt(self)


class Pipe:
    class Valves(BaseModel):
        n8n_url: str = Field(
            default="https://n8n.[your domain].com/webhook/[your webhook URL]"
        )
        n8n_auth_header: str = Field(
            default="Authorization",
            description="The header name for authentication (e.g., 'Authorization', 'x-api-key').",
        )
        n8n_bearer_token: EncryptedStr = Field(
            default="",
            description="The token value. 'Bearer ' prefix is auto-added for Authorization header.",
        )
        input_field: str = Field(default="chatInput")
        response_field: str = Field(default="output")
        app_name: str = Field(
            default="",
            description="This app's name (e.g., tigerai_hr_book2). Sent to n8n; the backend resolves which collection this app uses (single source = WebApp Tab04). Lets many apps share one streaming webhook.",
        )
        collection_name: str = Field(
            default="",
            description="(Optional override) Qdrant collection. Normally leave empty and use app_name; the backend resolves the collection from the app config.",
        )
        emit_interval: float = Field(
            default=2.0, description="Interval in seconds between status emissions"
        )
        enable_status_indicator: bool = Field(
            default=True, description="Enable or disable status indicator emissions"
        )
        request_timeout: int = Field(
            default=600, description="Timeout in seconds for the n8n request"
        )
        # --- Streaming ---
        enable_streaming: bool = Field(
            default=True,
            description="Enable SSE streaming mode. Only activates when n8n returns text/event-stream.",
        )
        # --- Redis ---
        redis_url: str = Field(
            default="redis://redis:6379/3",
            description="Redis connection URL for session and cache management.",
        )
        enable_redis_cache: bool = Field(
            default=True,
            description="Enable Redis for session context caching.",
        )
        redis_session_ttl: int = Field(
            default=3600,
            description="Session cache TTL in seconds (default: 1 hour).",
        )
        session_lock_timeout: int = Field(
            default=120,
            description="Max seconds to wait for a session lock before giving up. Prevents concurrent requests in the same chat from overwriting each other.",
        )
        # --- OWUI Task Settings ---
        owui_base_url: str = Field(
            default=os.getenv("WEBUI_URL", "http://localhost:8080"),
            description="OWUI API URL, defaults to WEBUI_URL env var. Usually no change needed.",
        )
        owui_api_key: EncryptedStr = Field(
            default=os.getenv("OPENWEBUI_API_KEY", ""),
            description="OWUI Admin API Key, defaults to OPENWEBUI_API_KEY env var. Usually no change needed.",
        )
        task_model: str = Field(
            default="n8n_v2",
            description="Model used for OWUI internal tasks (title, tags, follow-ups). Use 'n8n_v2' to route through this pipeline (filtered), or an Ollama model ID like 'qwen2.5:3b' for local processing.",
        )
        enable_title_generation: bool = Field(
            default=True,
            description="Auto-generate chat title after first message.",
        )
        enable_tags_generation: bool = Field(
            default=False,
            description="Auto-generate conversation tags.",
        )
        enable_follow_up_generation: bool = Field(
            default=False,
            description="Auto-generate follow-up question suggestions below each response.",
        )

    def __init__(self):
        self.type = "pipe"
        self.id = "n8n_pipe_v2"
        self.name = "N8N Pipe V2"
        self.valves = self.Valves()
        self._last_emit = {}  # v2.12.0: per-chat emit throttle (was a single shared timestamp)
        self.log = _log
        self.log.setLevel(os.getenv("SRC_LOG_LEVELS", "INFO").upper())
        self._redis = None
        self._redis_last_fail = 0.0  # v2.12.0: reconnect backoff
        self.log.info(f"[n8n_pipe_v2] Loaded version={PIPELINE_VERSION} date={PIPELINE_DATE}")

    # --- Safe content extraction (v2.12.0) ---
    @staticmethod
    def _extract_text_content(content: Any) -> str:
        """Turn a message 'content' into a safe text string.
        Handles plain str, OWUI multimodal list of parts, None/other."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for p in content:
                if isinstance(p, dict):
                    if p.get("type") == "text" and isinstance(p.get("text"), str):
                        parts.append(p["text"])
                    elif isinstance(p.get("content"), str):
                        parts.append(p["content"])
                elif isinstance(p, str):
                    parts.append(p)
            return "\n".join(s for s in parts if s)
        if content is None:
            return ""
        return str(content)

    # --- OWUI Task Config Sync ---
    async def _sync_owui_task_config(self):
        """Sync task model settings to OWUI when valves are saved."""
        api_key = self.valves.owui_api_key.get_decrypted()
        if not api_key or not self.valves.owui_base_url:
            self.log.info("[OWUI] No API key set, skipping task config sync.")
            return
        try:
            url = f"{self.valves.owui_base_url.rstrip('/')}/api/v1/tasks/config/update"
            payload = {
                "TASK_MODEL": self.valves.task_model,
                "TASK_MODEL_EXTERNAL": "",
                "ENABLE_TITLE_GENERATION": self.valves.enable_title_generation,
                "ENABLE_TAGS_GENERATION": self.valves.enable_tags_generation,
                "ENABLE_FOLLOW_UP_GENERATION": self.valves.enable_follow_up_generation,
                "ENABLE_SEARCH_QUERY_GENERATION": False,
                "ENABLE_RETRIEVAL_QUERY_GENERATION": False,
                "ENABLE_AUTOCOMPLETE_GENERATION": False,
                "AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH": -1,
                "TITLE_GENERATION_PROMPT_TEMPLATE": "",
                "IMAGE_PROMPT_GENERATION_PROMPT_TEMPLATE": "",
                "TAGS_GENERATION_PROMPT_TEMPLATE": "",
                "FOLLOW_UP_GENERATION_PROMPT_TEMPLATE": "",
                "QUERY_GENERATION_PROMPT_TEMPLATE": "",
                "TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE": "",
                "VOICE_MODE_PROMPT_TEMPLATE": "",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        self.log.info(f"[OWUI] Task config synced: task_model={self.valves.task_model}")
                    else:
                        body = await resp.text()
                        self.log.warning(f"[OWUI] Task config sync failed ({resp.status}): {body[:200]}")
        except Exception as e:
            self.log.warning(f"[OWUI] Task config sync error: {e}")

    async def on_valves_updated(self):
        """Called by OWUI when valve settings are saved. Syncs task config automatically."""
        await self._sync_owui_task_config()

    # --- Redis Helper (v2.12.0: reconnect backoff) ---
    async def _get_redis(self):
        if not self.valves.enable_redis_cache:
            return None
        if self._redis is None:
            # Don't hammer a down Redis: wait 30s between reconnect attempts.
            if time.time() - self._redis_last_fail < 30:
                return None
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(
                    self.valves.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=3,
                )
                await self._redis.ping()
                self.log.info(f"[Redis] Connected to {self.valves.redis_url}")
            except Exception as e:
                self._redis = None
                self._redis_last_fail = time.time()
                self.log.warning(f"[Redis] Connection failed: {e}. Running without cache (retry in 30s).")
        return self._redis

    async def _get_session_context(self, chat_id: str) -> list:
        r = await self._get_redis()
        if not r or not chat_id:
            return []
        try:
            data = await r.get(f"n8n:session:{chat_id}")
            if data:
                return json.loads(data)
        except Exception as e:
            self.log.warning(f"[Redis] Read error: {e}")
        return []

    async def _save_session_context(self, chat_id: str, messages: list):
        r = await self._get_redis()
        if not r or not chat_id:
            return
        try:
            trimmed = messages[-20:] if len(messages) > 20 else messages
            await r.set(f"n8n:session:{chat_id}", json.dumps(trimmed, ensure_ascii=False), ex=self.valves.redis_session_ttl)
        except Exception as e:
            self.log.warning(f"[Redis] Write error: {e}")

    async def _track_request(self, chat_id: str, question: str, response_time: float):
        r = await self._get_redis()
        if not r:
            return
        try:
            await r.incr("n8n:metrics:total_requests")
            await r.set("n8n:metrics:last_request", json.dumps({
                "chat_id": chat_id,
                "question_preview": question[:100],
                "response_time_ms": round(response_time * 1000),
                "timestamp": time.time(),
            }, ensure_ascii=False), ex=86400)
            await r.lpush("n8n:metrics:response_times", round(response_time * 1000))
            await r.ltrim("n8n:metrics:response_times", 0, 99)
        except Exception:
            pass

    # --- Session Lock (v2.12.0: UUID token + compare-and-delete) ---
    async def _acquire_lock(self, chat_id: str, __event_emitter__=None):
        """Acquire per-session lock.
        Returns: token(str) if acquired | None if no Redis/chat (proceed lock-free) | False if timed out."""
        r = await self._get_redis()
        if not r or not chat_id:
            return None
        token = uuid.uuid4().hex
        lock_key = f"n8n:lock:{chat_id}"
        timeout = self.valves.session_lock_timeout
        waited = 0
        while waited < timeout:
            if await r.set(lock_key, token, nx=True, ex=timeout):
                self.log.info(f"[Lock] Acquired for {chat_id} (token={token[:8]})")
                return token
            if waited == 0:
                await self._emit_status(__event_emitter__, "info", "Waiting for previous request to complete...", False, chat_id)
            await asyncio.sleep(1)
            waited += 1
        self.log.warning(f"[Lock] Timeout after {timeout}s for {chat_id}")
        return False

    async def _release_lock(self, chat_id: str, token):
        """Release lock ONLY if we still own it (compare-and-delete)."""
        if not token or not chat_id:  # None/False/empty -> nothing we own
            return
        r = await self._get_redis()
        if not r:
            return
        try:
            await r.eval(_RELEASE_LUA, 1, f"n8n:lock:{chat_id}", token)
            self.log.info(f"[Lock] Released for {chat_id} (token={token[:8]})")
        except Exception as e:
            self.log.warning(f"[Lock] Release error: {e}")

    async def _wait_lock_clear(self, chat_id: str):
        """OWUI task path: wait until the main response lock is gone (do not acquire)."""
        r = await self._get_redis()
        if not r or not chat_id:
            return
        lock_key = f"n8n:lock:{chat_id}"
        waited = 0
        while waited < self.valves.session_lock_timeout:
            try:
                if not await r.exists(lock_key):
                    return
            except Exception:
                return
            await asyncio.sleep(1)
            waited += 1

    # --- Event Info ---
    def _extract_event_info(self, event_emitter) -> tuple[Optional[str], Optional[str]]:
        if not event_emitter or not hasattr(event_emitter, '__closure__') or not event_emitter.__closure__:
            return None, None
        try:
            for cell in event_emitter.__closure__:
                try:
                    contents = cell.cell_contents
                except ValueError:
                    continue
                if isinstance(contents, dict):
                    return contents.get("chat_id"), contents.get("message_id")
        except Exception:
            pass
        return None, None

    # --- Status Emit ---
    async def _emit_status(
        self,
        __event_emitter__: Callable[[dict], Awaitable[None]],
        level: str,
        message: str,
        done: bool,
        chat_id: Optional[str] = None,
    ):
        # v2.12.0: throttle per chat_id so one chat's emits don't suppress another's.
        current_time = time.time()
        last = self._last_emit.get(chat_id, 0)
        if (
            __event_emitter__
            and self.valves.enable_status_indicator
            and (current_time - last >= self.valves.emit_interval or done)
        ):
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "status": "complete" if done else "in_progress",
                        "level": level,
                        "description": message,
                        "done": done,
                    },
                }
            )
            self._last_emit[chat_id] = current_time

    # --- Headers ---
    def _get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = self.valves.n8n_bearer_token.get_decrypted()
        header_key = self.valves.n8n_auth_header
        if token and header_key:
            # v2.12.0: case-insensitive Authorization match.
            if header_key.lower() == "authorization" and not token.startswith("Bearer "):
                headers[header_key] = f"Bearer {token}"
            else:
                headers[header_key] = token
        return headers

    # --- NDJSON Stream line -> text chunks (v2.12.0: factored for reuse + final flush) ---
    def _extract_stream_line(self, line: str) -> List[str]:
        out: List[str] = []
        line = line.strip()
        if not line:
            return out
        try:
            parsed = json.loads(line)
            if parsed.get("type") == "item":
                c = parsed.get("content", "")
                if c:
                    out.append(c)
            # "end" is per-node, NOT end of workflow -> ignore; stream ends on HTTP close.
        except json.JSONDecodeError:
            # Fallback: SSE "data: ..." format
            if line.startswith("data: "):
                data = line[6:]
                if data == "[DONE]":
                    return out
                try:
                    p = json.loads(data)
                    if isinstance(p, dict):
                        choices = p.get("choices", [])
                        if choices:
                            c = choices[0].get("delta", {}).get("content", "")
                            if c:
                                out.append(c)
                        elif self.valves.response_field in p:
                            out.append(p[self.valves.response_field])
                except json.JSONDecodeError:
                    if data.strip():
                        out.append(data)
        return out

    async def _parse_ndjson_stream(self, response) -> AsyncGenerator[str, None]:
        buffer = ""
        async for chunk in response.content.iter_any():
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                for c in self._extract_stream_line(line):
                    yield c
        # v2.12.0: flush trailing buffer (final chunk may have no newline).
        if buffer.strip():
            for c in self._extract_stream_line(buffer):
                yield c

    # --- Main Pipe ---
    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __event_call__: Callable[[dict], Awaitable[dict]] = None,
    ) -> AsyncGenerator[str, None]:
        chat_id, _ = self._extract_event_info(__event_emitter__)
        messages = body.get("messages", []) if isinstance(body, dict) else []
        # v2.12.0: safe extraction (last message may be non-dict / missing content / multimodal).
        last_msg = messages[-1] if (messages and isinstance(messages[-1], dict)) else {}
        question = self._extract_text_content(last_msg.get("content"))

        is_owui_task = question.startswith("### Task:")

        # OWUI internal tasks (title/tags/follow-ups): never touch n8n.
        if is_owui_task:
            async def _owui_task():
                await self._wait_lock_clear(chat_id)

                def _extract_user_prompt():
                    import re
                    m = re.search(r'USER:\s*(.+)', question)
                    if m:
                        return m.group(1).strip()[:12]
                    for msg in messages[:-1]:
                        if isinstance(msg, dict) and msg.get("role") == "user":
                            txt = self._extract_text_content(msg.get("content"))
                            if txt and not txt.startswith("### Task:"):
                                return txt[:12]
                    return "Chat"

                if "follow_ups" in question or "follow-up" in question.lower():
                    yield '{ "follow_ups": [] }'
                elif "title" in question.lower():
                    user_prompt = _extract_user_prompt().replace('"', '').replace('\n', ' ')
                    yield f'{user_prompt}'
                elif "tags" in question.lower():
                    yield '{ "tags": ["General"] }'
                else:
                    yield "{}"
            return _owui_task()

        async def _run():
            if not messages:
                await self._emit_status(__event_emitter__, "error", "No messages found", True, chat_id)
                yield "Error: No messages found"
                return
            if not question:
                await self._emit_status(__event_emitter__, "error", "No text content in last message", True, chat_id)
                yield "Error: No text content in the last message"
                return

            # --- Session Lock (consolidated helper) ---
            lock_token = await self._acquire_lock(chat_id, __event_emitter__)
            if lock_token is False:
                await self._emit_status(__event_emitter__, "error", "Timed out waiting, please try again later", True, chat_id)
                yield "Error: Previous request still processing, please try again later."
                return

            start_time = time.time()
            session_context = await self._get_session_context(chat_id)

            headers = self._get_headers()
            if self.valves.enable_streaming:
                headers["Accept"] = "text/event-stream, application/x-ndjson"

            payload = {"sessionId": str(chat_id) if chat_id else "unknown"}
            payload[self.valves.input_field] = question
            # v2.13.0: report this app's identity so the backend resolves the right collection
            # (single source = Tab04). Lets many apps share one streaming webhook.
            if self.valves.app_name:
                payload["app_name"] = self.valves.app_name
            if self.valves.collection_name:  # optional explicit override
                payload["collection_name"] = self.valves.collection_name
            if session_context:
                payload["context"] = session_context

            self.log.info(f"[v{PIPELINE_VERSION}] POST {self.valves.n8n_url} | streaming={self.valves.enable_streaming} | chat_id={chat_id}")
            await self._emit_status(__event_emitter__, "info", "Calling N8N...", False, chat_id)

            aio_timeout = aiohttp.ClientTimeout(total=self.valves.request_timeout)
            aio_session = aiohttp.ClientSession(timeout=aio_timeout)
            response = None
            full_response = []

            try:
                response = await aio_session.post(self.valves.n8n_url, json=payload, headers=headers)

                # v2.12.0: accept any 2xx.
                if not (200 <= response.status < 300):
                    error_body = await response.text()
                    clean_error = "N8N Endpoint Not Found (404)" if response.status == 404 and "<html" in error_body else error_body[:200]
                    error_msg = f"N8N Error {response.status}: {clean_error}"
                    self.log.error(error_msg)
                    await self._emit_status(__event_emitter__, "error", error_msg, True, chat_id)
                    yield f"Error: {error_msg}"
                    return

                content_type = response.headers.get("Content-Type", "")
                transfer_enc = response.headers.get("Transfer-Encoding", "")
                is_streaming = (
                    "text/event-stream" in content_type
                    or "ndjson" in content_type
                    or (self.valves.enable_streaming and "chunked" in transfer_enc)
                )
                self.log.info(f"[Response] Content-Type={content_type!r} | Transfer-Encoding={transfer_enc!r} | streaming={is_streaming}")

                if self.valves.enable_streaming and is_streaming:
                    # --- STREAMING PATH ---
                    await self._emit_status(__event_emitter__, "info", "Streaming...", False, chat_id)
                    async for text in self._parse_ndjson_stream(response):
                        full_response.append(text)
                        yield text
                else:
                    # --- BATCH PATH ---
                    await self._emit_status(__event_emitter__, "info", "AI Agent processing...", False, chat_id)
                    response_text = await response.text()
                    # v2.12.0: 204 / empty body guard.
                    if response.status == 204 or not response_text.strip():
                        n8n_response = "No content returned."
                    else:
                        try:
                            data = json.loads(response_text)
                            if isinstance(data, list):
                                if not data:  # v2.12.0: empty list guard
                                    n8n_response = "No content returned."
                                else:
                                    item = data[0]
                                    n8n_response = item.get(self.valves.response_field, str(item)) if isinstance(item, dict) else str(item)
                            elif isinstance(data, dict):
                                n8n_response = data.get(self.valves.response_field, str(data))
                            else:
                                n8n_response = str(data)
                        except json.JSONDecodeError:
                            n8n_response = response_text
                        n8n_response = n8n_response or "No content returned."
                    full_response.append(n8n_response)
                    yield n8n_response

            except Exception as e:
                err = f"Error: {str(e)}"
                self.log.error(f"[Pipe] {err}")
                await self._emit_status(__event_emitter__, "error", str(e), True, chat_id)
                yield err

            finally:
                if response is not None:
                    response.release()
                await aio_session.close()
                elapsed = time.time() - start_time
                complete_text = "".join(full_response)
                if complete_text and not complete_text.startswith("Error:"):
                    await self._save_session_context(chat_id, messages + [
                        {"role": "assistant", "content": complete_text}
                    ])
                    await self._track_request(chat_id, question, elapsed)
                    await self._emit_status(__event_emitter__, "info", f"Complete ({elapsed:.1f}s)", True, chat_id)
                # v2.12.0: release only our own lock (compare-and-delete).
                await self._release_lock(chat_id, lock_token)

        return _run()
