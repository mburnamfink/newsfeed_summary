"""Pluggable Claude backends for scoring and summarization.

Two backends sit behind one ``complete(system, prompt) -> text`` interface:

- ``api`` — the Anthropic Messages API, billed per token, authenticated with
  ``ANTHROPIC_API_KEY``.
- ``subscription`` — the Claude Agent SDK driving Claude Code, authenticated with
  a Claude Pro/Max subscription (no per-token billing). Requires the Claude Code
  CLI (``npm install -g @anthropic-ai/claude-code``) and a logged-in session or a
  ``CLAUDE_CODE_OAUTH_TOKEN`` from ``claude setup-token``.

Callers stay backend-agnostic: they hand over a system prompt and a user prompt
and parse the returned text themselves (both backends return raw model text).
"""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# The two backends default to different models on purpose: Haiku is the cheap
# per-token workhorse for the API, while subscription quota is shared with
# interactive Claude usage, so it runs Sonnet for better single-pass quality.
DEFAULT_API_MODEL = "claude-haiku-4-5"
DEFAULT_SUBSCRIPTION_MODEL = "claude-sonnet-4-6"

MAX_TOKENS = 2048

# Caps on in-flight requests when callers fan batches out with asyncio.gather.
# The API tolerates plenty of parallelism; the subscription backend spawns a
# Claude Code subprocess per call, so it stays low to bound memory and CPU.
DEFAULT_API_CONCURRENCY = 8
DEFAULT_SUBSCRIPTION_CONCURRENCY = 3


class LLMBackend:
    """One system + user prompt in, response text out.

    Subclasses implement the async ``_acomplete``. The public ``acomplete``
    wraps it in a per-backend concurrency limit, so callers can fan out many
    batches with ``asyncio.gather`` without overrunning the API rate limit (or,
    for the subscription backend, spawning unbounded subprocesses). ``complete``
    is a sync convenience for non-async callers.
    """

    def __init__(self, max_concurrency: int):
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def acomplete(self, system_text: str, prompt: str) -> str:
        async with self._semaphore:
            return await self._acomplete(system_text, prompt)

    async def _acomplete(self, system_text: str, prompt: str) -> str:
        raise NotImplementedError

    def complete(self, system_text: str, prompt: str) -> str:
        return asyncio.run(self.acomplete(system_text, prompt))


class AnthropicBackend(LLMBackend):
    """Anthropic Messages API — per-token billing via ANTHROPIC_API_KEY."""

    def __init__(self, model: str = DEFAULT_API_MODEL, max_concurrency: int = DEFAULT_API_CONCURRENCY):
        from anthropic import AsyncAnthropic

        from .config import ensure_anthropic_key

        ensure_anthropic_key()
        super().__init__(max_concurrency)
        self.model = model
        self.client = AsyncAnthropic()

    async def _acomplete(self, system_text: str, prompt: str) -> str:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


class SubscriptionBackend(LLMBackend):
    """Claude Pro/Max subscription via the Claude Agent SDK (Claude Code).

    Auth is whatever Claude Code is logged in as, or ``CLAUDE_CODE_OAUTH_TOKEN``.
    A set ``ANTHROPIC_API_KEY`` would route Claude Code to per-token API billing
    instead — the SDK inherits this process's environment, so the key is dropped
    here to keep runs on the subscription.
    """

    def __init__(self, model: str = DEFAULT_SUBSCRIPTION_MODEL, max_concurrency: int = DEFAULT_SUBSCRIPTION_CONCURRENCY):
        super().__init__(max_concurrency)
        self.model = model
        os.environ.pop("ANTHROPIC_API_KEY", None)

    async def _acomplete(self, system_text: str, prompt: str) -> str:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        # No tools are allowed, so Claude answers in a single turn and stops;
        # leaving max_turns unset avoids Claude Code flagging an error_max_turns
        # result after a perfectly good response.
        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_text,
            allowed_tools=[],
            setting_sources=[],  # don't inherit the user's CLAUDE.md / project settings
            permission_mode="bypassPermissions",
        )

        parts: list[str] = []
        error = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                if message.error:
                    error = message.error
                parts.extend(b.text for b in message.content if isinstance(b, TextBlock))
            elif isinstance(message, ResultMessage) and message.is_error:
                error = error or message.errors or message.result

        text = "".join(parts)
        if not text and error:
            raise RuntimeError(f"Claude Code run failed: {error}")
        return text


def build_backend(config: dict | None) -> LLMBackend:
    config = config or {}
    backend = str(config.get("backend", "subscription")).lower()
    model = config.get("model")
    max_concurrency = config.get("max_concurrency")

    if backend in ("subscription", "claude-code", "pro", "max"):
        return SubscriptionBackend(
            model or DEFAULT_SUBSCRIPTION_MODEL,
            max_concurrency or DEFAULT_SUBSCRIPTION_CONCURRENCY,
        )
    if backend in ("api", "anthropic"):
        return AnthropicBackend(
            model or DEFAULT_API_MODEL,
            max_concurrency or DEFAULT_API_CONCURRENCY,
        )
    raise ValueError(f"Unknown llm backend {backend!r}; use 'subscription' or 'api'")
