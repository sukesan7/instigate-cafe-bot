"""Reusable offline test helpers, fake Discord objects, and aiohttp mock router."""

from __future__ import annotations

import http.cookies
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, Self
from unittest.mock import MagicMock

import aiohttp
import discord
import multidict
import yarl

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class OfflineNetworkDisabledError(RuntimeError):
    """Raised when a test attempts unmocked outbound network I/O."""


def load_fixture_text(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text(encoding="utf-8")


def load_fixture_json(filename: str) -> Any:
    return json.loads(load_fixture_text(filename))


class FrozenClock:
    """Deterministic manual Unix timestamp controller for time-dependent tests."""

    def __init__(self, initial: float = 1_760_000_000.0):
        self._now = float(initial)

    def __call__(self) -> float:
        return self._now

    @property
    def unix(self) -> int:
        return int(self._now)

    def set(self, ts: float) -> int:
        self._now = float(ts)
        return int(self._now)

    def advance(self, seconds: float) -> int:
        self._now += float(seconds)
        return int(self._now)


@dataclass
class RecordedMessage:
    content: str | None = None
    embed: discord.Embed | None = None
    embeds: list[discord.Embed] = field(default_factory=list)
    ephemeral: bool = False
    view: Any = None
    file: Any = None


class FakeResponse:
    """Mimics discord.InteractionResponse including defer + single-response guards."""

    def __init__(self, interaction: FakeInteraction):
        self._interaction = interaction
        self._done = False
        self.deferred = False
        self.thinking = False
        self.ephemeral = False
        self.sent_messages: list[RecordedMessage] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        if self._done:
            raise discord.InteractionResponded(self._interaction)  # type: ignore[arg-type]
        self._done = True
        self.deferred = True
        self.thinking = thinking
        self.ephemeral = ephemeral

    async def send_message(
        self,
        content: Any = None,
        *,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        ephemeral: bool = False,
        view: Any = None,
        file: Any = None,
    ) -> None:
        if self._done:
            raise discord.InteractionResponded(self._interaction)  # type: ignore[arg-type]
        self._done = True
        self.ephemeral = ephemeral
        self.sent_messages.append(
            RecordedMessage(
                content=str(content) if content is not None else None,
                embed=embed,
                embeds=list(embeds or ([] if embed is None else [embed])),
                ephemeral=ephemeral,
                view=view,
                file=file,
            )
        )


class FakeFollowup:
    """Mimics discord.Webhook followup requiring an initial response/defer."""

    def __init__(self, response: FakeResponse):
        self._response = response
        self.messages: list[RecordedMessage] = []

    async def send(
        self,
        content: Any = None,
        *,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        ephemeral: bool = False,
        view: Any = None,
        file: Any = None,
        wait: bool = False,
    ) -> RecordedMessage:
        if not self._response.is_done():
            raise RuntimeError("Interaction followup.send() called before response.defer()/send_message()")
        msg = RecordedMessage(
            content=str(content) if content is not None else None,
            embed=embed,
            embeds=list(embeds or ([] if embed is None else [embed])),
            ephemeral=ephemeral,
            view=view,
            file=file,
        )
        self.messages.append(msg)
        return msg


class FakeClient:
    """Minimal bot client exposing `.settings`, `.db`, and `.user` for commands."""

    def __init__(self, *, settings: Any, db: Any | None = None):
        self.settings = settings
        self.db = db
        self.user = SimpleNamespace(id=999000000000000001, display_name="InstigatorTest")
        self.guilds: list[Any] = []


class FakeInteraction:
    """Offline replacement for discord.Interaction usable directly with command callbacks."""

    def __init__(
        self,
        *,
        user: Any,
        client: FakeClient,
        guild: Any | None = None,
        channel: Any | None = None,
        command_name: str = "ic test",
    ):
        self.user = user
        self.client = client
        self.guild = guild if guild is not None else SimpleNamespace(id=client.settings.guild_id, name="Test Guild")
        self.channel = channel if channel is not None else SimpleNamespace(id=client.settings.user_command_channel_id)
        self.command = SimpleNamespace(qualified_name=command_name)
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self.response)

    @property
    def all_replies(self) -> list[RecordedMessage]:
        return list(self.response.sent_messages) + list(self.followup.messages)

    @property
    def last_reply(self) -> RecordedMessage:
        replies = self.all_replies
        if not replies:
            raise AssertionError("No response or followup messages were recorded on FakeInteraction")
        return replies[-1]


def build_fake_member(
    *,
    user_id: int = 800000000000000001,
    display_name: str = "SyntheticMember",
    avatar_url: str = "https://example.invalid/avatars/member.png",
    administrator: bool = False,
    role_ids: list[int] | None = None,
) -> MagicMock:
    """Create an offline mock satisfying `isinstance(member, discord.Member)`."""
    member = MagicMock(spec=discord.Member)
    member.id = int(user_id)
    member.display_name = display_name
    member.name = display_name
    member.mention = f"<@{int(user_id)}>"
    member.display_avatar = SimpleNamespace(url=avatar_url)
    member.guild_permissions = SimpleNamespace(administrator=administrator)
    member.roles = [SimpleNamespace(id=int(rid), name=f"role-{rid}") for rid in (role_ids or [])]
    return member


@dataclass
class RecordedHttpCall:
    method: str
    url: str
    headers: dict[str, str]
    json_body: Any
    params: Any


class FakeAiohttpResponse:
    """Async context-manager response mock compatible with `aiohttp.ClientResponse`."""

    def __init__(
        self,
        *,
        url: str | yarl.URL,
        method: str,
        status: int = 200,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ):
        self.url = yarl.URL(str(url))
        self.method = method
        self.status = int(status)
        self._body = body
        self.headers = multidict.CIMultiDictProxy(multidict.CIMultiDict(headers or {}))
        self.cookies = http.cookies.SimpleCookie()
        self.reason = "OK" if self.status < 400 else "Error"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    async def read(self) -> bytes:
        return self._body

    async def text(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return self._body.decode(encoding, errors=errors)

    async def json(self, *, encoding: str = "utf-8", content_type: str | None = None) -> Any:
        return json.loads(self._body.decode(encoding))

    def raise_for_status(self) -> None:
        if self.status >= 400:
            req_info = aiohttp.RequestInfo(
                url=self.url,
                method=self.method,
                headers=self.headers,
                real_url=self.url,
            )
            raise aiohttp.ClientResponseError(
                request_info=req_info,
                history=(),
                status=self.status,
                message=self.reason,
                headers=self.headers,
            )

    def release(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_for_close(self) -> None:
        return None


@dataclass
class _MockRoute:
    method: str
    matcher: Callable[[str], bool]
    responder: Callable[[RecordedHttpCall], FakeAiohttpResponse]
    repeat: bool = True
    used: int = 0


class AiohttpMockRouter:
    """Lightweight monkeypatch router intercepting `aiohttp.ClientSession._request`."""

    def __init__(self):
        self._routes: list[_MockRoute] = []
        self.calls: list[RecordedHttpCall] = []

    @staticmethod
    def _compile_matcher(
        pattern: str | re.Pattern[str] | Callable[[str], bool], *, prefix: bool = False
    ) -> Callable[[str], bool]:
        if callable(pattern):
            return pattern
        if isinstance(pattern, re.Pattern):
            return lambda u: pattern.search(u) is not None
        if prefix:
            return lambda u: u.startswith(pattern)
        return lambda u: u == pattern

    def add_route(
        self,
        method: str,
        pattern: str | re.Pattern[str] | Callable[[str], bool],
        *,
        status: int = 200,
        json_data: Any = None,
        text: str | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        prefix: bool = False,
        repeat: bool = True,
        handler: Callable[[RecordedHttpCall], FakeAiohttpResponse] | None = None,
    ) -> None:
        method_up = method.upper()
        matcher = self._compile_matcher(pattern, prefix=prefix)

        if handler is not None:
            responder = handler
        else:
            if body is not None:
                raw_bytes = body
                default_headers: dict[str, str] = {}
            elif json_data is not None:
                raw_bytes = json.dumps(json_data).encode("utf-8")
                default_headers = {"Content-Type": "application/json"}
            else:
                raw_bytes = (text or "").encode("utf-8")
                default_headers = {"Content-Type": "text/html; charset=utf-8"}

            merged_headers = {**default_headers, **(headers or {})}

            def responder(call: RecordedHttpCall, _s=status, _b=raw_bytes, _h=merged_headers) -> FakeAiohttpResponse:
                return FakeAiohttpResponse(
                    url=call.url,
                    method=call.method,
                    status=_s,
                    body=_b,
                    headers=_h,
                )

        self._routes.append(
            _MockRoute(method=method_up, matcher=matcher, responder=responder, repeat=repeat)
        )

    def get(self, pattern: str | re.Pattern[str] | Callable[[str], bool], **kwargs: Any) -> None:
        self.add_route("GET", pattern, **kwargs)

    def post(self, pattern: str | re.Pattern[str] | Callable[[str], bool], **kwargs: Any) -> None:
        self.add_route("POST", pattern, **kwargs)

    def dispatch(
        self, method: str, url: str, headers: dict[str, str], json_body: Any, params: Any
    ) -> FakeAiohttpResponse:
        call = RecordedHttpCall(
            method=method.upper(),
            url=url,
            headers=headers,
            json_body=json_body,
            params=params,
        )
        self.calls.append(call)

        for route in self._routes:
            if not route.repeat and route.used > 0:
                continue
            if route.method in (call.method, "*") and route.matcher(url):
                route.used += 1
                return route.responder(call)

        raise OfflineNetworkDisabledError(
            f"Unmocked aiohttp {call.method} {url!r} — register a mock route in tests"
        )
