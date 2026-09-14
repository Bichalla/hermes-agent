"""Native one-command approval surface for an external Kanban worker broker.

The broker validates the live run. This facade retains native button authority
and exposes neither an arbitrary message sender nor a decision injection API.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
import uuid


class KanbanApprovalService:
    """Synchronous request interface, called off the Gateway event-loop thread."""

    api_version = 1

    def __init__(self, gateway):
        self.__gateway = gateway

    def request(self, data: dict, route: dict, deadline: float, cancel) -> str:
        from gateway.config import Platform
        from tools.approval import register_gateway_notify, unregister_gateway_notify
        from tools.approval_gateway_wait import _await_gateway_decision

        gateway = self.__gateway
        loop = gateway._gateway_loop
        adapter = gateway.adapters.get(Platform.DISCORD)
        owner = str(route.get("owner_id", ""))
        command = data.get("command", "")
        description = data.get("description", "")
        if (not isinstance(deadline, (float, int)) or not math.isfinite(deadline)
                or deadline - time.time() > 300
                or route.get("platform") != "discord" or not owner.isdecimal()
                or not str(route.get("chat_id", "")).isdecimal()
                or not isinstance(command, str) or not command or len(command) > 1200
                or not isinstance(description, str) or len(description) > 200
                or "```" in command or "```" in description
                or tuple(data.get("allowed_choices", ())) != ("once", "deny")
                or not data.get("request_id") or not data.get("digest")):
            return "deny"

        def available() -> bool:
            return bool(
                adapter is not None and gateway._running and loop is not None
                and loop.is_running() and not loop.is_closed()
                and {str(uid) for uid in getattr(adapter, "_allowed_user_ids", ())} == {owner}
                and not getattr(adapter, "_allowed_role_ids", ())
                and not adapter._discord_allow_all_users()
                and time.time() < deadline and not cancel()
            )

        try:
            if not available():
                return "deny"
            if asyncio.get_running_loop() is loop:
                return "deny"
        except RuntimeError:
            # Expected in the broker's client thread, where no asyncio loop runs.
            pass
        except Exception:
            return "deny"

        session_key = "kanban-owner:" + uuid.uuid4().hex
        finished = threading.Event()
        cancelled = threading.Event()

        def watch() -> None:
            while not finished.wait(0.2):
                try:
                    valid = available()
                except Exception:
                    valid = False
                if not valid:
                    cancelled.set()
                    unregister_gateway_notify(session_key)
                    return

        def notify(payload: dict) -> None:
            if cancelled.is_set() or not available():
                raise RuntimeError("Kanban approval unavailable")
            reason = f"Kanban {data.get('task_id', '')} / run {data.get('run_id', '')}: {description}"
            if len(reason) > 300:
                raise RuntimeError("Kanban approval display exceeds native limit")
            # A Discord thread is itself a channel. Avoid inherited session routing.
            destination = str(route.get("thread_id") or route["chat_id"])
            if not destination.isdecimal():
                raise RuntimeError("Kanban approval route invalid")

            def build_prompt(_channel):
                import discord
                from plugins.platforms.discord.adapter import ExecApprovalView

                # Generic component admission includes pairing/global allowlists.
                # Native per-view admin gating narrows this request to ONE owner
                # without changing any profile-wide authorization settings.
                view = ExecApprovalView(
                    session_key, allowed_user_ids={owner}, allowed_role_ids=set(),
                    require_admin=True, admin_user_ids={owner},
                    allow_permanent=False, allow_session=False,
                )
                content = (
                    f"<@{owner}> ⚠️ **Command Approval Required**\n\n"
                    f"**Requested command:**\n```bash\n{command}\n```\n**Reason:** {reason}"
                )
                if len(content) > 2000:
                    raise RuntimeError("Kanban approval display exceeds native limit")
                embed = discord.Embed(
                    title="Command Approval Required", description=f"```\n{command}\n```",
                    color=discord.Color.orange(),
                )
                embed.add_field(name="Reason", value=reason, inline=False)
                return {
                    "content": content, "embed": embed, "view": view,
                    "allowed_mentions": discord.AllowedMentions(
                        users=[discord.Object(id=int(owner))], roles=False,
                        everyone=False, replied_user=False,
                    ),
                }, view

            future = asyncio.run_coroutine_threadsafe(
                adapter._send_prompt(destination, {}, build_prompt), loop,
            )
            try:
                sent = future.result(timeout=max(0.01, min(10.0, deadline - time.time())))
                if getattr(sent, "success", None) is not True:
                    raise RuntimeError("Kanban approval delivery failed")
            except Exception:
                future.cancel()
                # Native wait logs exception text; never expose adapter/network secrets.
                raise RuntimeError("Kanban approval delivery failed") from None

        register_gateway_notify(session_key, notify)
        watcher = threading.Thread(target=watch, name="kanban-approval-cancel", daemon=True)
        try:
            watcher.start()
            result = _await_gateway_decision(
                session_key, notify, dict(data), surface="kanban_worker",
            )
            return "once" if (
                result.get("resolved") is True and result.get("choice") == "once"
                and not cancelled.is_set() and available()
            ) else "deny"
        except Exception:
            return "deny"
        finally:
            finished.set()
            unregister_gateway_notify(session_key)
            if watcher.ident is not None:
                watcher.join(timeout=1)
