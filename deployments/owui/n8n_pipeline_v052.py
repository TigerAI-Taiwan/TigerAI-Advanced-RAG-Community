"""
title: n8n Pipeline (Fixed Auth, Hardened)
author: TigerAI Morris Lu (inspired by Cole Medin & owndev)
version: 0.5.4
date: 2026-05-25
description: v0.5.4 - add 'app_name' valve; pipe reports its app identity, backend resolves the
             collection from WebApp config (single source). collection_name kept as optional override.
             v0.5.3 - add 'collection_name' valve (superseded by app_name as primary).
             v0.5.2 - hardening from code review:
             - safe last-message content extraction (multimodal list / empty dict / non-string)
             - empty-list response guarded; accept 2xx (incl. 204 empty)
             - decrypt() returns "" + warns on no-key/decrypt-fail (never sends a bad token)
             - return type hint fixed to Optional[Union[str, dict]]
             - removed body["messages"].append (OWUI uses the return value; avoids double message)
             - Authorization header match is case-insensitive
             - default request_timeout raised 60 -> 300s (RAG/LLM round-trips)
             v0.5.1 base: print version at startup. v0.5.0 base: configurable auth header.
"""

from typing import Optional, Callable, Awaitable, Any, Dict, Union
from pydantic import BaseModel, Field, GetCoreSchemaHandler
from cryptography.fernet import Fernet, InvalidToken
import time
import aiohttp
import os
import base64
import hashlib
import logging
import json
from pydantic_core import core_schema

PIPELINE_VERSION = "0.5.4"
PIPELINE_DATE = "2026-05-25"

_log = logging.getLogger("n8n_pipe")


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
        # v0.5.2: value IS encrypted but we cannot decrypt -> return "" (never leak/forward ciphertext as a token).
        key = cls._get_encryption_key()
        if not key:
            _log.warning(
                "[n8n_pipe] token is encrypted but WEBUI_SECRET_KEY is not set -> returning empty token"
            )
            return ""
        try:
            encrypted_part = value[len("encrypted:") :]
            f = Fernet(key)
            return f.decrypt(encrypted_part.encode()).decode()
        except (InvalidToken, Exception):
            _log.warning("[n8n_pipe] token decrypt failed -> returning empty token")
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
            description="The token value. ('Bearer ' is auto-added only when the header is Authorization and the value lacks it.)",
        )
        input_field: str = Field(default="chatInput")
        response_field: str = Field(default="output")
        app_name: str = Field(
            default="",
            description="This app's name (e.g., tigerai_hr_book2). Sent to n8n; the backend resolves which collection/brain this app uses (single source = WebApp Tab04). Preferred over collection_name.",
        )
        collection_name: str = Field(
            default="",
            description="(Optional override) Qdrant collection. Normally leave empty and use app_name; the backend resolves the collection from the app config.",
        )
        # v1.0.27 2026-06-01: backend_url valve — 後端 createOwuiPipeline 會寫進來,query payload 要帶給
        # n8n,#05-RAG-Core 才能 Fetch Backend Config。沒宣告 OWUI 會丟棄 → n8n fail-loud "Invalid URL"。
        backend_url: str = Field(
            default="",
            description="Internal URL n8n uses to call back the TigerAI backend (e.g., http://tigerai-backend:3060). Auto-filled by the backend; leave as-is.",
        )
        emit_interval: float = Field(
            default=2.0, description="Interval in seconds between status emissions"
        )
        enable_status_indicator: bool = Field(
            default=True, description="Enable or disable status indicator emissions"
        )
        request_timeout: int = Field(
            default=300,
            description="Timeout in seconds for the n8n request (RAG/LLM round-trips can be slow; raise to 600 for slow local models).",
        )

    def __init__(self):
        self.type = "pipe"
        self.id = "n8n_pipe"
        self.name = "N8N Pipe"
        self.valves = self.Valves()
        self.last_emit_time = 0
        self.log = _log
        self.log.setLevel(os.getenv("SRC_LOG_LEVELS", "INFO").upper())
        self.log.info(
            f"[n8n_pipe] Loaded version={PIPELINE_VERSION} date={PIPELINE_DATE}"
        )

    def _extract_event_info(self, event_emitter) -> tuple[Optional[str], Optional[str]]:
        if (
            not event_emitter
            or not hasattr(event_emitter, "__closure__")
            or not event_emitter.__closure__
        ):
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

    @staticmethod
    def _extract_text(content: Any) -> str:
        """v0.5.2: safely turn a message 'content' into text.
        Handles plain str, OWUI multimodal list of parts, or None/other."""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict):
                    # OpenAI-style multimodal: {"type":"text","text":"..."}
                    if p.get("type") == "text" and isinstance(p.get("text"), str):
                        parts.append(p["text"])
                    elif isinstance(p.get("content"), str):
                        parts.append(p["content"])
                elif isinstance(p, str):
                    parts.append(p)
            return "\n".join(s for s in parts if s).strip()
        if content is None:
            return ""
        return str(content).strip()

    async def _emit_status(
        self,
        __event_emitter__: Callable[[dict], Awaitable[None]],
        level: str,
        message: str,
        done: bool,
    ):
        current_time = time.time()
        if (
            __event_emitter__
            and self.valves.enable_status_indicator
            and (
                current_time - self.last_emit_time >= self.valves.emit_interval or done
            )
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
            self.last_emit_time = current_time

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}

        token = self.valves.n8n_bearer_token.get_decrypted()
        header_key = self.valves.n8n_auth_header

        if token and header_key:
            # v0.5.2: case-insensitive match so 'authorization' also auto-prefixes Bearer.
            if header_key.lower() == "authorization" and not token.startswith("Bearer "):
                headers[header_key] = f"Bearer {token}"
            else:
                headers[header_key] = token

        return headers

    async def pipe(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __event_call__: Callable[[dict], Awaitable[dict]] = None,
    ) -> Optional[Union[str, dict]]:

        await self._emit_status(__event_emitter__, "info", "Calling N8N...", False)

        chat_id, _ = self._extract_event_info(__event_emitter__)
        messages = body.get("messages", []) if isinstance(body, dict) else []
        n8n_response = None

        if not messages:
            error_msg = "No messages found"
            await self._emit_status(__event_emitter__, "error", error_msg, True)
            return {"error": error_msg}

        # v0.5.2: safe extraction (last message may be a non-dict, missing 'content', or multimodal).
        last_msg = messages[-1] if isinstance(messages[-1], dict) else {}
        question = self._extract_text(last_msg.get("content"))
        if not question:
            error_msg = "No text content found in the last message"
            await self._emit_status(__event_emitter__, "error", error_msg, True)
            return {"error": error_msg}

        try:
            headers = self._get_headers()
            payload = {"sessionId": str(chat_id) if chat_id else "unknown"}
            payload[self.valves.input_field] = question
            # v0.5.4: report this app's identity so the backend resolves the right collection (single source).
            if self.valves.app_name:
                payload["app_name"] = self.valves.app_name
            # v1.0.27: forward backend_url so #05-RAG-Core can Fetch Backend Config (fail-loud if missing)
            if self.valves.backend_url:
                payload["backend_url"] = self.valves.backend_url
            if self.valves.collection_name:  # optional explicit override
                payload["collection_name"] = self.valves.collection_name

            self.log.info(
                f"[v{PIPELINE_VERSION}] POST {self.valves.n8n_url} with Headers: {list(headers.keys())}"
            )

            aio_timeout = aiohttp.ClientTimeout(total=self.valves.request_timeout)
            async with aiohttp.ClientSession(timeout=aio_timeout) as session:
                async with session.post(
                    self.valves.n8n_url,
                    json=payload,
                    headers=headers,
                ) as response:

                    response_text = await response.text()

                    # v0.5.2: accept any 2xx (n8n may return 200/201/202/204).
                    if not (200 <= response.status < 300):
                        if response.status == 404 and "<html" in response_text:
                            clean_error = (
                                "N8N Endpoint Not Found (404). Server returned HTML."
                            )
                        else:
                            clean_error = response_text[:200]
                        error_msg = f"N8N Error {response.status}: {clean_error}"
                        self.log.error(error_msg)
                        raise Exception(error_msg)

                    # v0.5.2: 204 / empty body -> nothing to parse.
                    if response.status == 204 or not response_text.strip():
                        n8n_response = "No content returned."
                    else:
                        try:
                            response_data = json.loads(response_text)
                        except json.JSONDecodeError:
                            response_data = None
                            n8n_response = response_text

                        if isinstance(response_data, list):
                            if not response_data:  # v0.5.2: empty list guard
                                n8n_response = "No content returned."
                            else:
                                item = response_data[0]
                                n8n_response = (
                                    item.get(self.valves.response_field, str(item))
                                    if isinstance(item, dict)
                                    else str(item)
                                )
                        elif isinstance(response_data, dict):
                            n8n_response = response_data.get(
                                self.valves.response_field, str(response_data)
                            )
                        elif response_data is not None:
                            n8n_response = str(response_data)

                    if not n8n_response:
                        n8n_response = "No content returned."

            # v0.5.2: do NOT append to body["messages"] — OWUI renders the return value as the
            # assistant message; appending here risks a duplicate / inconsistent state.

        except Exception as e:
            await self._emit_status(
                __event_emitter__, "error", f"Failed: {str(e)}", True
            )
            return {"error": str(e)}

        await self._emit_status(__event_emitter__, "info", "Complete", True)
        return n8n_response
