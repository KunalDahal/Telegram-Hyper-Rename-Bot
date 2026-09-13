import asyncio
import os
import re
import shutil
import time
import logging
from copy import deepcopy
from html import escape, unescape

from pyrogram import Client, enums
from PIL import Image

from src.core.user_setting import DEFAULT_CAPTION_TEMPLATE
from src.services.downloader import Downloader
from src.services.media_processor import MediaProcessor
from src.services.uploader import Uploader
from src.utils.retry import call_with_flood_retry
from src.utils.session_persistence import ensure_persistent_session, fingerprint as _session_fingerprint


logger = logging.getLogger(__name__)

BOT_DOWNLOAD_LIMIT = 2 * 1024 ** 3
BOT_PART_SIZE = int(1.95 * 1024 ** 3)
PREMIUM_PART_SIZE = int(3.95 * 1024 ** 3)
MAX_PIPELINE_SLOTS = 4


class Worker:
    def __init__(self, task_queue, user_settings_getter, client, config, helper_bots=None, helper_loads=None):
        self.task_queue = task_queue
        self.user_settings_getter = user_settings_getter
        self.client = client
        self.download_client = client
        self.helper_bots = helper_bots or {}
        self.helper_loads = helper_loads or {}
        self._premium_download_client: Client | None = None
        self._retired_download_clients: list[Client] = []
        self._premium_session_fingerprint: str | None = None

        self._dump_chat_id: int | None = None

        self._premium_dump_chat_id: int | None = None

        self._premium_extra_dump_chat_id: int | None = None

        self._premium_dump_can_write: bool | None = None
        self._premium_extra_dump_can_write: bool | None = None
        self.config = config
        self.media_processor = MediaProcessor(config.paths.ffmpeg)
        self.temp_base = config.paths.tmp
        self.thumbnails_dir = config.paths.thumbnails
        self.running = False

        configured_workers = getattr(
            config, "workers", getattr(config, "max_rename_at_once", MAX_PIPELINE_SLOTS)
        )
        self.workers = max(1, int(configured_workers or MAX_PIPELINE_SLOTS))
        self.pool_size = self.workers
        self.max_rename_at_once = self.workers
        self.download_limit = self.workers
        self.upload_limit = self.workers
        self.watermark_limit = self.workers

        self._download_slot = asyncio.Semaphore(self.download_limit)
        self._upload_slot = asyncio.Semaphore(self.upload_limit)
        self._watermark_processing_slot = asyncio.Semaphore(self.watermark_limit)
        logger.info(
            "[Worker] Concurrency: WORKERS=%d (jobs=%d download=%d upload=%d watermark=%d)",
            self.workers,
            self.pool_size,
            self.download_limit,
            self.upload_limit,
            self.watermark_limit,
        )
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._pool_worker_tasks: list[asyncio.Task] = []
        self._uploading_task_ids: set[str] = set()
        self._delivery_tasks: dict[str, asyncio.Task] = {}

        self._peer_resolve_locks: dict[tuple[int, int], asyncio.Lock] = {}

        self._ensure_runtime_directories()

    def _peer_lock(self, client: Client, chat_id) -> asyncio.Lock:
        key = (id(client), int(chat_id))
        lock = self._peer_resolve_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._peer_resolve_locks[key] = lock
        return lock

    async def _ensure_peer_resolved(
        self, client: Client, chat_id, chat_username: str | None = None
    ) -> None:
        lock = self._peer_lock(client, chat_id)
        async with lock:
            try:
                await client.get_chat(chat_id)
                return
            except Exception:
                logger.debug(
                    "[Worker] Peer warm-up get_chat(%s) failed; trying to "
                    "join %s into the chat before giving up.",
                    chat_id, getattr(client, "name", "client"), exc_info=True,
                )

            joined_via = await self._try_join_source_chat(client, chat_id, chat_username)

            try:
                await client.get_chat(chat_id)
                if joined_via:
                    logger.info(
                        "[Worker] %s joined chat %s via %s and can now "
                        "resolve it.",
                        getattr(client, "name", "client"), chat_id, joined_via,
                    )
            except Exception:
                logger.debug(
                    "[Worker] Peer warm-up get_chat(%s) still failing after "
                    "a join attempt; letting the caller's own call surface "
                    "the real error.",
                    chat_id, exc_info=True,
                )

    async def _try_join_source_chat(
        self, client: Client, chat_id, chat_username: str | None
    ) -> str | None:
        if chat_username:
            try:
                await client.join_chat(chat_username)
                return f"username @{chat_username}"
            except Exception:
                logger.debug(
                    "[Worker] join_chat(@%s) for source chat %s failed.",
                    chat_username, chat_id, exc_info=True,
                )

        if client is not self.client:
            try:
                invite = await self.client.export_chat_invite_link(chat_id)
                invite_link = getattr(invite, "invite_link", None) or (
                    invite if isinstance(invite, str) else None
                )
            except Exception:
                logger.debug(
                    "[Worker] export_chat_invite_link(%s) via the bot "
                    "client failed; the bot may not be an admin there, "
                    "or the chat may not exist.",
                    chat_id, exc_info=True,
                )
                return None

            if invite_link:
                try:
                    await client.join_chat(invite_link)
                    return "an invite link exported by the bot"
                except Exception:
                    logger.debug(
                        "[Worker] join_chat(<exported invite link>) for "
                        "source chat %s failed.",
                        chat_id, exc_info=True,
                    )

        return None

    async def _ensure_user_peer_resolved(
        self, client: Client, user_id: int, username: str | None
    ) -> None:
        if client is self.client:
            return

        try:
            await client.get_chat(user_id)
            return
        except Exception:
            logger.debug(
                "[Worker] Premium session has no cached peer for user_id=%s "
                "yet; attempting to resolve one.",
                user_id, exc_info=True,
            )

        if username:
            try:
                await client.get_users(username)
                return
            except Exception:
                logger.warning(
                    "[Worker] Could not resolve @%s (user_id=%s) via "
                    "username for the Premium session.",
                    username, user_id, exc_info=True,
                )

        raise RuntimeError(
            f"The Premium session has never interacted with user "
            f"{user_id} and can't resolve them"
            + (f" (tried @{username})" if username else " (no public "
               "@username on file to resolve them by")
            + ". Files over 2 GiB must be delivered by the Premium "
              "account, but Telegram requires it to have already 'met' "
              "the recipient -- either via a public @username, or by the "
              "user starting a chat with the Premium account once."
        )

    async def start(self):
        self.running = True
        self._startup_cleanup()

        await self._initialize_dump_chat()

        session_string = str(getattr(self.config, "session_string", "") or "").strip()
        if session_string:
            await self.configure_premium_download_session(session_string)

        self._pool_worker_tasks = [
            asyncio.create_task(self._pool_worker_loop(index + 1))
            for index in range(self.pool_size)
        ]
        try:
            await self._worker_loop()
        finally:
            await self.stop()

    async def stop(self):
        self.running = False
        tasks = list(self._active_tasks.values())
        delivery_tasks = list(self._delivery_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in delivery_tasks:
            if not task.done():
                task.cancel()
        for worker_task in self._pool_worker_tasks:
            if not worker_task.done():
                worker_task.cancel()
        await asyncio.gather(
            *tasks, *delivery_tasks, *self._pool_worker_tasks, return_exceptions=True
        )
        self._pool_worker_tasks.clear()
        self._uploading_task_ids.clear()
        await self._stop_download_clients()

    @property
    def has_premium_download_session(self) -> bool:
        return self._premium_download_client is not None

    async def configure_premium_download_session(self, session_string: str):
        session_string = (session_string or "").strip()
        if not session_string:
            raise ValueError("SESSION_STRING is empty.")

        new_fingerprint = _session_fingerprint(session_string)
        if (
            self._premium_download_client is not None
            and self._premium_session_fingerprint == new_fingerprint
            and self._premium_download_client.is_connected
        ):
            logger.debug(
                "[Worker] configure_premium_download_session() called again "
                "with an unchanged SESSION_STRING; reusing the existing "
                "premium client."
            )
            return await self._premium_download_client.get_me()

        premium_dump_target = getattr(self.config, "bot_dump_chat_id", None)
        if not premium_dump_target:
            premium_dump_target = getattr(self.config, "dump_chat_id", None)
        if not premium_dump_target:
            raise ValueError(
                "BOT_DUMP_CHAT_ID (or DUMP_CHAT_ID) is required when SESSION_STRING is configured."
            )
        premium_session_dir = self.config.paths.logs
        await ensure_persistent_session(
            "premium_download_session", premium_session_dir, session_string
        )
        candidate = Client(
            "premium_download_session",
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            workdir=premium_session_dir,
            in_memory=False,
            max_concurrent_transmissions=self.config.upload_part_workers,
            no_updates=True,
        )

        try:
            await candidate.start()
            account = await candidate.get_me()

            if account.is_bot:
                raise ValueError(
                    "SESSION_STRING belongs to a bot account, not a user account."
                )

            if not getattr(account, "is_premium", False):
                raise ValueError(
                    "The Telegram account in SESSION_STRING is not Premium."
                )

            premium_dump_id = await self._prepare_dump_chat(
                candidate,
                str(premium_dump_target),
            )
            self._premium_dump_can_write = await self._verify_can_write(
                candidate, premium_dump_id, "BOT_DUMP_CHAT_ID"
            )

            extra_dump_target = getattr(self.config, "dump_chat_id", None)
            if extra_dump_target and str(extra_dump_target) != str(premium_dump_target):
                try:
                    self._premium_extra_dump_chat_id = await self._prepare_dump_chat(
                        candidate, str(extra_dump_target)
                    )
                    logger.info(
                        "[Worker] Premium session also joined DUMP_CHAT_ID=%s "
                        "(chat_id=%s).",
                        extra_dump_target, self._premium_extra_dump_chat_id,
                    )
                    self._premium_extra_dump_can_write = await self._verify_can_write(
                        candidate, self._premium_extra_dump_chat_id, "DUMP_CHAT_ID"
                    )
                except Exception as exc:
                    self._premium_extra_dump_chat_id = None
                    self._premium_extra_dump_can_write = None
                    logger.warning(
                        "[Worker] Premium session could not join "
                        "DUMP_CHAT_ID=%r (non-fatal): %s",
                        extra_dump_target, exc,
                    )

            if not self._premium_dump_can_write and not self._premium_extra_dump_can_write:
                logger.error(
                    "[Worker] Premium session has no confirmed-writable dump "
                    "chat (BOT_DUMP_CHAT_ID writable=%s, DUMP_CHAT_ID "
                    "writable=%s). Premium deliveries will likely fail with "
                    "CHAT_WRITE_FORBIDDEN until the account is granted "
                    "posting rights in one of these chats.",
                    self._premium_dump_can_write, self._premium_extra_dump_can_write,
                )

        except Exception:
            try:
                await candidate.stop()
            except Exception:
                pass
            raise

        previous = self._premium_download_client
        self._premium_download_client = candidate
        self._premium_session_fingerprint = new_fingerprint
        self.download_client = candidate
        self._premium_dump_chat_id = premium_dump_id

        if previous:
            self._retired_download_clients.append(previous)

        logger.info(
            "[Worker] Premium session enabled: user_id=%s premium_dump_chat=%s",
            getattr(account, "id", "unknown"),
            self._premium_dump_chat_id,
        )
        return account

    async def _verify_can_write(self, client: Client, chat_id: int, label: str) -> bool:
        try:
            await client.send_chat_action(chat_id, enums.ChatAction.CANCEL)
            logger.info(
                "[Worker] Premium session confirmed writable: %s (chat_id=%s).",
                label, chat_id,
            )
            return True
        except Exception as exc:
            logger.warning(
                "[Worker] Premium session joined %s (chat_id=%s) but CANNOT "
                "post there: %s. Forwards/copies to this chat will fail "
                "with CHAT_WRITE_FORBIDDEN until the account is given "
                "posting rights (e.g. made an admin with post permission, "
                "or the chat's member permissions are relaxed).",
                label, chat_id, exc,
            )
            return False

    async def _prepare_dump_chat(self, client: Client, configured_chat: str) -> int:
        target = (configured_chat or "").strip()
        if not target:
            raise ValueError("DUMP_CHAT_ID is empty.")

        try:
            chat = await client.get_chat(target)
            if not chat or not getattr(chat, "id", None):
                raise ValueError("Telegram returned an invalid dump chat.")
            return int(chat.id)
        except Exception:
            logger.info(
                "[Worker] Premium session could not resolve DUMP_CHAT_ID=%r; "
                "trying join_chat().",
                target,
            )

        if target.startswith("-100") and target.lstrip("-").isdigit():
            raise ValueError(
                f"Premium account cannot resolve numeric DUMP_CHAT_ID={target}. "
                "Add the Premium account to the dump channel first, or provide "
                "the private invite link in DUMP_CHAT_ID."
            )

        try:
            chat = await client.join_chat(target)
        except Exception as exc:
            raise ValueError(
                f"Premium account could not resolve/join DUMP_CHAT_ID={target!r}: "
                f"{exc}"
            ) from exc

        if not chat or not getattr(chat, "id", None):
            raise ValueError(
                f"Premium dump chat could not be resolved from {target!r}."
            )

        return int(chat.id)

    async def _stop_download_clients(self) -> None:
        clients = [self._premium_download_client, *self._retired_download_clients]
        self._premium_download_client = None
        self._retired_download_clients.clear()
        self._premium_dump_chat_id = None
        self._premium_extra_dump_chat_id = None
        self._premium_dump_can_write = None
        self._premium_extra_dump_can_write = None
        self.download_client = self.client
        for client in clients:
            if not client:
                continue
            try:
                await client.stop()
            except Exception:
                logger.warning("Could not stop Premium download session cleanly.")

    async def clear_for_restart(self) -> None:
        await self.stop()
        if os.path.isdir(self.temp_base):
            for name in os.listdir(self.temp_base):
                folder = os.path.join(self.temp_base, name)
                if os.path.isdir(folder):
                    shutil.rmtree(folder, ignore_errors=True)
        self.task_queue.clear()

    def _startup_cleanup(self):
        self.task_queue.purge_stale_tasks()
        if os.path.exists(self.temp_base):
            active_ids = set(self.task_queue.queue)
            for name in os.listdir(self.temp_base):
                folder = os.path.join(self.temp_base, name)
                if os.path.isdir(folder) and name not in active_ids:
                    shutil.rmtree(folder, ignore_errors=True)

    async def _worker_loop(self):
        while self.running:
            await asyncio.sleep(0.25)

    async def _pool_worker_loop(self, worker_number: int):
        while self.running:
            task = self.task_queue.pop_next_queued_task()
            if not task:
                await asyncio.sleep(0.05)
                continue

            task_id = task["task_id"]
            if task_id in self._active_tasks:
                continue

            task["_worker_number"] = worker_number
            task["_start_time"] = task.get("_start_time") or time.time()
            self.task_queue.update_status(task_id, "starting", 0)
            active = asyncio.create_task(self._run_pipeline_task(task))
            self._active_tasks[task_id] = active
            self._refresh_current_task()
            try:
                await active
            except asyncio.CancelledError:
                if not active.done():
                    active.cancel()
                raise
            except Exception:
                pass


    def _refresh_current_task(self):
        self.task_queue.current_task = next(iter(self._active_tasks), None)

    @staticmethod
    def _has_watermark(task: dict) -> bool:
        watermark = task.get("watermark", {})
        return bool(watermark.get("enabled") and str(watermark.get("text", "")).strip())

    def _uses_premium_download(self, task: dict) -> bool:
        return False

    def _uses_premium_dump(self, task: dict) -> bool:
        return False

    async def _resolve_bot_dump_chat(self, configured_chat: str):
        target = (configured_chat or "").strip()

        logger.info(
            "[Bot Dump] Starting bot-side dump resolution: target=%r "
            "session_mode=%s",
            target,
            "BOT_SESSION_STRING"
            if getattr(self.config, "bot_session_string", "")
            else "file-based",
        )

        if not target:
            raise RuntimeError("BOT_DUMP_CHAT_ID is required.")

        try:
            me = await self.client.get_me()
            logger.info(
                "[Bot Dump] Bot identity before resolution: id=%s username=@%s",
                me.id,
                me.username or "",
            )
        except Exception:
            logger.exception("[Bot Dump] Could not retrieve bot identity.")
            raise

        try:
            logger.info(
                "[Bot Dump] Step 1: get_chat(%r)",
                target,
            )
            chat = await self.client.get_chat(target)

            logger.info(
                "[Bot Dump] Step 1 SUCCESS: id=%s title=%r username=%r "
                "type=%r is_preview=%s",
                getattr(chat, "id", None),
                getattr(chat, "title", None),
                getattr(chat, "username", None),
                getattr(chat, "type", None),
                chat.__class__.__name__ == "ChatPreview",
            )

            if chat and getattr(chat, "id", None):
                if chat.__class__.__name__ != "ChatPreview":
                    return chat

                if "+" not in target:
                    return chat

                logger.info(
                    "[Bot Dump] Invite returned a ChatPreview; attempting "
                    "join_chat(%r).",
                    target,
                )

        except Exception:
            logger.warning(
                "[Bot Dump] Step 1 get_chat(%r) failed; checking whether "
                "this is an invite link that can be joined.",
                target,
                exc_info=True,
            )

        if (
            "t.me/+" in target
            or "telegram.me/+" in target
            or "t.me/joinchat/" in target
            or "telegram.me/joinchat/" in target
        ):
            try:
                logger.info(
                    "[Bot Dump] Step 2: join_chat(%r)",
                    target,
                )
                chat = await self.client.join_chat(target)

                logger.info(
                    "[Bot Dump] Step 2 SUCCESS: id=%s title=%r username=%r type=%r",
                    getattr(chat, "id", None),
                    getattr(chat, "title", None),
                    getattr(chat, "username", None),
                    getattr(chat, "type", None),
                )

                if chat and getattr(chat, "id", None):
                    return chat

                raise RuntimeError(
                    "join_chat() returned no usable chat object."
                )

            except Exception as exc:
                logger.exception(
                    "[Bot Dump] Step 2 join_chat(%r) FAILED.",
                    target,
                )
                raise RuntimeError(
                    f"Bot could not resolve/join BOT_DUMP_CHAT_ID={target!r}: "
                    f"{exc}"
                ) from exc

        try:
            target_id = int(target)
        except ValueError:
            raise RuntimeError(
                f"Bot cannot resolve BOT_DUMP_CHAT_ID={target!r}. "
                "Use a numeric -100... ID, a public @username, or a valid "
                "Telegram invite link."
            )

        logger.info(
            "[Bot Dump] Step 3: numeric ID direct lookup failed; warming the "
            "peer cache via get_dialogs() and retrying get_chat(%s).",
            target_id,
        )
        try:
            async for dialog in self.client.get_dialogs():
                if getattr(dialog.chat, "id", None) == target_id:
                    return dialog.chat
        except Exception:
            logger.warning(
                "[Bot Dump] get_dialogs() warm-up failed while looking for %s.",
                target_id, exc_info=True,
            )

        try:
            chat = await self.client.get_chat(target_id)
            if chat and getattr(chat, "id", None):
                return chat
        except Exception:
            pass

        raise RuntimeError(
            f"Bot cannot resolve BOT_DUMP_CHAT_ID={target_id}. "
            "The bot has no cached peer for this chat, which usually means "
            "it was added to the channel/group without ever receiving a "
            "message or update from it. Fix: post any message in that chat "
            "(or remove and re-add the bot as admin so it receives a "
            "service message), or use the channel's @username or a private "
            "invite link in BOT_DUMP_CHAT_ID instead."
        )

    async def _initialize_dump_chat(self) -> None:
        bot_target = getattr(self.config, "bot_dump_chat_id", None)

        if not bot_target:
            raise RuntimeError(
                "BOT_DUMP_CHAT_ID is required. "
                "Use the numeric -100... ID of the bot's dump channel."
            )

        chat = await self._resolve_bot_dump_chat(str(bot_target))

        if not chat or not getattr(chat, "id", None):
            raise RuntimeError(
                f"BOT_DUMP_CHAT_ID={bot_target!r} could not be resolved."
            )

        self._dump_chat_id = int(chat.id)

        if bool(getattr(chat, "has_protected_content", False)):
            raise RuntimeError(
                "BOT_DUMP_CHAT_ID has content protection enabled. Disable "
                "Protect Content/Restrict Saving Content in the dump channel; "
                "BOT_TOKEN must be able to copy staged messages to user DMs."
            )

        try:
            me = await self.client.get_me()
            member = await self.client.get_chat_member(
                self._dump_chat_id,
                me.id,
            )
            status = getattr(member, "status", "unknown")
            logger.info(
                "[Worker] Bot dump ready: %s (%s); bot status=%s",
                getattr(chat, "title", None)
                or getattr(chat, "username", None)
                or self._dump_chat_id,
                self._dump_chat_id,
                status,
            )
        except Exception:
            logger.info(
                "[Worker] Bot dump resolved: %s",
                self._dump_chat_id,
            )

    async def _stage_source_to_dump(self, task: dict) -> None:
        if not self._dump_chat_id:
            raise RuntimeError("Bot dump chat is not initialized.")

        existing_chat_id = task.get("download_source_chat_id")
        existing_message_id = task.get("download_source_message_id")
        if existing_chat_id and existing_message_id:
            return

        source_chat_id = task.get("source_chat_id")
        source_chat_username = task.get("source_chat_username")
        source_message_id = task.get("source_message_id")
        if not source_chat_id or not source_message_id:
            raise Exception("Source chat/message ID is missing for dump staging.")

        file_size = int(task.get("file_size", 0) or 0)

        if file_size > BOT_DOWNLOAD_LIMIT and not self.has_premium_download_session:
            logger.warning(
                "[Worker] No Premium staging session for >2 GiB task %s; "
                "downloading the original source directly with HyperTG.",
                task.get("task_id", "")[:8],
            )
            return

        stage_client = (
            self._premium_download_client
            if file_size > BOT_DOWNLOAD_LIMIT and self.has_premium_download_session
            else self.client
        )
        stage_chat_id = self._dump_chat_id

        await self._ensure_peer_resolved(stage_client, source_chat_id, source_chat_username)

        try:
            forwarded = await call_with_flood_retry(
                stage_client.forward_messages,
                chat_id=stage_chat_id,
                from_chat_id=source_chat_id,
                message_ids=source_message_id,
                disable_notification=True,
                max_transient_retries=2,
            )
        except Exception as exc:
            if "CHANNEL_INVALID" in str(exc) or "PEER_ID_INVALID" in str(exc):
                logger.warning(
                    "[Worker] forward_messages hit %s for source_chat_id=%s; "
                    "retrying once after a fresh peer resolution.",
                    exc, source_chat_id,
                )
                await asyncio.sleep(1)
                await self._ensure_peer_resolved(stage_client, source_chat_id, source_chat_username)
                forwarded = await stage_client.forward_messages(
                    chat_id=stage_chat_id,
                    from_chat_id=source_chat_id,
                    message_ids=source_message_id,
                    disable_notification=True,
                )
            else:
                raise

        if isinstance(forwarded, list):
            forwarded = forwarded[0] if forwarded else None
        if not forwarded or not getattr(forwarded, "id", None):
            logger.error(
                "[Worker] forward_messages returned no usable message for "
                "task_id=%s: source_chat_id=%s source_message_id=%s "
                "stage_chat_id=%s premium_staging=%s result=%r",
                task.get("task_id"), source_chat_id, source_message_id,
                stage_chat_id, (stage_client is self._premium_download_client), forwarded,
            )
            raise Exception(
                "Failed to archive the received file in the dump channel "
                f"(source message {source_message_id} in chat "
                f"{source_chat_id} may no longer exist)."
            )

        task["download_source_chat_id"] = stage_chat_id
        task["download_source_message_id"] = forwarded.id
        task["dump_received_message_id"] = forwarded.id
        self.task_queue.checkpoint(task["task_id"])

    async def _download_with_slot(self, task: dict) -> str:
        task_id = task["task_id"]
        saved_path = task.get("downloaded_path", "")
        if task.get("download_completed") and saved_path and os.path.isfile(saved_path):
            if os.path.getsize(saved_path) > 0:
                logger.info("[Worker] Reusing completed download for %s", task_id[:8])
                return saved_path

        file_size = int(task.get("file_size", 0) or 0)

        self.task_queue.update_status(task_id, "staging_to_dump", 0)
        await self._stage_source_to_dump(task)

        self.task_queue.update_status(task_id, "waiting_for_download", 0)
        async with self._download_slot:
            self.task_queue.update_status(task_id, "downloading", 0)
            download_client = self.client
            downloader = Downloader(
                self.temp_base, self.task_queue, task_id,
                helper_bots=self.helper_bots, helper_loads=self.helper_loads,
            )
            path = await downloader.download(
                client=download_client,
                task_data=task,
            )
        if not path or not os.path.exists(path):
            raise Exception("Download failed or file missing after download")
        task["downloaded_path"] = path
        task["download_completed"] = True
        self.task_queue.checkpoint(task_id)
        return path

    async def _run_pipeline_task(self, task: dict):
        task_id = task["task_id"]
        try:
            self._ensure_runtime_directories()

            if task.get("dump_upload_done") and task.get("dump_message_ids"):
                logger.info(
                    "[Worker] %s already uploaded to dump before a restart; "
                    "resuming delivery only.",
                    task_id[:8],
                )
                self._spawn_delivery(task)
                return

            await self._snapshot_thumbnail(task)
            task["user_is_premium"] = await self._get_user_premium(task["user_id"])

            downloaded_path = await self._download_with_slot(task)
            job = self._build_job(task)
            task["output_filename"] = job["output_filename"]
            has_watermark = self._has_watermark(task)

            prepared_path = task.get("prepared_upload_path", "")
            if task.get("prepared_completed") and prepared_path and os.path.isfile(prepared_path):
                upload_path = prepared_path
                logger.info("[Worker] Reusing prepared output for %s.", task_id[:8])
            elif has_watermark:
                self.task_queue.update_status(task_id, "waiting_for_processing", 0)
                async with self._watermark_processing_slot:
                    upload_path = await self._prepare_upload_file(task, downloaded_path, job)
            else:
                upload_path = await self._prepare_upload_file(task, downloaded_path, job)

            task["prepared_upload_path"] = upload_path
            task["prepared_completed"] = True
            self.task_queue.checkpoint(task_id)

            self.task_queue.update_status(task_id, "waiting_for_upload", 0)
            async with self._upload_slot:
                self._uploading_task_ids.add(task_id)
                try:
                    await self._upload(task, upload_path, job)
                finally:
                    self._uploading_task_ids.discard(task_id)

            self._spawn_delivery(task)
            return
        except asyncio.CancelledError:
            if not self.running:
                logger.info(
                    "[Worker] Task %s interrupted by shutdown; leaving it for resume.",
                    task_id[:8],
                )
                raise
            await self._notify_user(
                task["user_id"],
                f"<b>▸ Task Cancelled</b>\n"
                f"────────────────\n"
                f"Task <code>{task_id[:8]}</code> was <u>cancelled</u>.",
            )
            self.task_queue.remove_task(task_id, final_status="cancelled")
            self._cleanup_task_folder(task_id)
        except Exception as exc:
            logger.exception("[Worker] Task %s failed", task_id[:8])
            failure_text = str(exc).strip()
            if len(failure_text) > 300:
                failure_text = failure_text[-300:]
            await self._notify_user(
                task["user_id"],
                f"<b>▸ Task Failed</b>\n"
                f"────────────────\n"
                f"Task <code>{task_id[:8]}</code> failed.\n"
                f"<code>{failure_text}</code>",
            )
            self.task_queue.remove_task(task_id, final_status="failed", error=str(exc))
            self._cleanup_task_folder(task_id)
        finally:
            self._uploading_task_ids.discard(task_id)
            self._active_tasks.pop(task_id, None)
            self._refresh_current_task()

    async def _prepare_upload_file(self, task: dict, downloaded_path: str, job: dict) -> str:
        metadata     = job.get("metadata") or task.get("metadata", {})
        watermark    = task.get("watermark", {})
        # `watermark` on the task is pure styling configuration (enabled,
        # text, color, timing, position); the actual font file it depends on
        # lives in task_assets. Older persisted tasks may still carry the
        # font path embedded in `watermark` itself, hence the fallback.
        task_assets  = task.get("task_assets") or {}
        watermark_font_path = task_assets.get("watermark_font_path") or watermark.get("font_path", "")
        has_metadata = any(str(v).strip() for v in metadata.values())
        has_watermark = bool(watermark.get("enabled") and str(watermark.get("text", "")).strip())
        if not has_metadata and not has_watermark:
            return downloaded_path

        task_id     = task["task_id"]
        task_folder = os.path.join(self.temp_base, task_id)
        _, ext = os.path.splitext(job["output_filename"])
        if not ext:
            ext = os.path.splitext(downloaded_path)[1] or ".mkv"
        output_path = os.path.join(task_folder, f"processed_{task_id}{ext}")

        self.task_queue.update_status(task_id, "processing", 0)
        if has_watermark:
            logger.info(
                "[Worker] %s starting watermark processing (mode=%s, output=%s).",
                task_id[:8],
                watermark.get("timing_mode", "range"),
                os.path.basename(output_path),
            )
        try:
            processed_path = await self.media_processor.process(
                input_path=downloaded_path,
                output_path=output_path,
                metadata=metadata,
                watermark={**watermark, "font_path": watermark_font_path},
            )
        except Exception:
            logger.exception("[Worker] %s media processing failed", task_id[:8])
            raise

        logger.info("[Worker] %s media processing completed.", task_id[:8])
        return processed_path

    @staticmethod
    def _part_filename(filename: str, part_number: int) -> str:
        base, ext = os.path.splitext(filename)
        return f"{base} P{part_number}{ext}"

    async def _split_output_for_bot(
        self, task: dict, file_path: str, output_filename: str, max_part_size: int
    ) -> list[tuple[str, str]]:
        task_dir = os.path.join(self.temp_base, task["task_id"])
        os.makedirs(task_dir, exist_ok=True)

        ffmpeg_parts = await self.media_processor.split_for_size(
            input_path=file_path,
            output_dir=task_dir,
            max_bytes=max_part_size,
        )
        if not ffmpeg_parts:
            raise RuntimeError("Failed to split oversized output with FFmpeg.")

        result = []
        for part_number, part_path in enumerate(ffmpeg_parts, start=1):
            name = self._part_filename(output_filename, part_number)
            final_path = os.path.join(task_dir, name)
            if os.path.abspath(part_path) != os.path.abspath(final_path):
                os.replace(part_path, final_path)
            if os.path.getsize(final_path) > max_part_size:
                raise RuntimeError(
                    f"FFmpeg produced an oversized part ({os.path.getsize(final_path)} bytes > {max_part_size} bytes)."
                )
            result.append((final_path, name))

        return result

    async def _upload(self, task: dict, file_path: str, job: dict):
        task_id = task["task_id"]
        self._ensure_runtime_directories()
        self.task_queue.update_status(task_id, "uploading", 0)
        task["upload_progress"] = {"percentage": 0.0}

        if not self._dump_chat_id:
            raise RuntimeError(
                "BOT_DUMP_CHAT_ID could not be resolved for the bot session."
            )

        actual_upload_size = os.path.getsize(file_path)
        use_premium_dump = (
            actual_upload_size > BOT_DOWNLOAD_LIMIT and self.has_premium_download_session
        )

        if use_premium_dump:
            upload_client = self._premium_download_client
            upload_chat_id = self._dump_chat_id
        else:
            upload_client = self.client
            upload_chat_id = self._dump_chat_id

        output_filename = job["output_filename"]

        if use_premium_dump and actual_upload_size <= PREMIUM_PART_SIZE:
            upload_parts = [(file_path, output_filename)]
        elif (not use_premium_dump) and actual_upload_size <= BOT_PART_SIZE:
            upload_parts = [(file_path, output_filename)]
        else:
            max_part_size = PREMIUM_PART_SIZE if use_premium_dump else BOT_PART_SIZE
            upload_parts = await self._split_output_for_bot(
                task, file_path, output_filename, max_part_size
            )

        # Resolve the thumbnail once per task, not once per part: auto-detect
        # downloads and normalization are both potentially expensive/flaky,
        # and every part of a given upload must use the same thumbnail asset
        # for a deterministic result regardless of how many parts there are.
        resolved_thumbnail_path = await self._resolve_thumbnail(task, job)

        results = []
        for part_path, part_name in upload_parts:
            upload_data = {
                **task,
                "upload_file_path": part_path,
                "output_filename": part_name,
                "send_type": job.get("send_type", "media"),
                "thumbnail_path": resolved_thumbnail_path,
                "upload_chat_id": upload_chat_id,
            }

            uploader = Uploader(
                upload_client,
                upload_data,
                self.task_queue,
                tmp_dir=self.temp_base,
                user_is_premium=use_premium_dump,
            )

            part_results = await uploader.upload()
            if not part_results:
                raise RuntimeError(
                    f"Upload returned no dump message for {part_name}."
                )

            results.extend(part_results)

        task["dump_message_ids"] = [r.id for r in results]
        task["dump_upload_done"] = True
        task["dump_used_premium"] = use_premium_dump
        self.task_queue.checkpoint(task_id)

    def _spawn_delivery(self, task: dict) -> None:
        task_id = task["task_id"]
        self.task_queue.update_status(task_id, "forwarding", 0)
        delivery_task = asyncio.create_task(self._finalize_delivery(task))
        self._delivery_tasks[task_id] = delivery_task
        delivery_task.add_done_callback(
            lambda _t, tid=task_id: self._delivery_tasks.pop(tid, None)
        )

    async def _finalize_delivery(self, task: dict) -> None:
        task_id = task["task_id"]
        try:
            await self._deliver_dump_messages(task)
            self.task_queue.remove_task(task_id, final_status="completed")
            self._cleanup_task_folder(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[Worker] Delivery failed for task %s", task_id[:8])
            failure_text = str(exc).strip()
            if len(failure_text) > 300:
                failure_text = failure_text[-300:]
            await self._notify_user(
                task["user_id"],
                f"<b>▸ Delivery Failed</b>\n"
                f"────────────────\n"
                f"Task <code>{task_id[:8]}</code> failed.\n"
                f"<code>{failure_text}</code>",
            )
            self.task_queue.remove_task(task_id, final_status="failed", error=str(exc))
            self._cleanup_task_folder(task_id)

    async def _deliver_dump_messages(self, task: dict) -> None:
        task_id = task["task_id"]
        dump_ids: list[int] = task.get("dump_message_ids") or []
        use_premium_dump = bool(task.get("dump_used_premium"))
        delivered = set(task.get("delivered_message_ids") or [])

        deliver_client = (
            self._premium_download_client if use_premium_dump else self.client
        )
        if use_premium_dump and not deliver_client:
            raise RuntimeError(
                "This file was uploaded via the Premium session but no "
                "Premium session is available anymore to deliver it. "
                "Reconfigure SESSION_STRING and retry."
            )

        await self._ensure_user_peer_resolved(
            deliver_client, task["user_id"], task.get("username")
        )

        for dump_id in dump_ids:
            if dump_id in delivered:
                continue

            source = await self._get_dump_message_with_retry(dump_id)
            source_unique_id = self._media_unique_id(source)
            caption = self._delivery_caption(task, getattr(source, "caption", None))

            delivered_ok = False
            try:
                await call_with_flood_retry(
                    deliver_client.copy_message,
                    chat_id=task["user_id"],
                    from_chat_id=self._dump_chat_id,
                    message_id=dump_id,
                    caption=caption,
                    parse_mode=enums.ParseMode.HTML,
                )
                delivered_ok = True
            except Exception as copy_exc:
                logger.warning(
                    "[Worker] copy_message failed for dump message %s: %s; "
                    "checking recent history before falling back.",
                    dump_id, copy_exc,
                )
                if source_unique_id and await self._recently_delivered(
                    deliver_client, task["user_id"], source_unique_id,
                ):
                    logger.info(
                        "[Worker] Dump message %s already present in the "
                        "user's chat; skipping duplicate re-send.", dump_id,
                    )
                    delivered_ok = True
                else:
                    await self._resend_media(deliver_client, task["user_id"], source, caption)
                    delivered_ok = True

            if delivered_ok:
                delivered.add(dump_id)
                task["delivered_message_ids"] = sorted(delivered)
                self.task_queue.checkpoint(task_id)

    async def _get_dump_message_with_retry(self, dump_id: int, attempts: int = 4):
        delay = 1.0
        last_empty = False
        for attempt in range(1, attempts + 1):
            source = await self.client.get_messages(
                chat_id=self._dump_chat_id, message_ids=dump_id,
            )
            if isinstance(source, list):
                source = source[0] if source else None

            if not source or getattr(source, "empty", False):
                last_empty = True
            else:
                last_empty = False
                if self._media_unique_id(source) or attempt == attempts:
                    return source
                logger.warning(
                    "[Worker] Dump message %s read but has no media yet "
                    "(attempt %d/%d); retrying shortly.",
                    dump_id, attempt, attempts,
                )

            if attempt < attempts:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)

        if last_empty:
            raise RuntimeError(
                f"Uploaded dump message {dump_id} could not be read."
            )
        raise RuntimeError(
            "Dump message contains no supported media for delivery."
        )

    @staticmethod
    def _media_unique_id(message) -> str:
        media = (
            getattr(message, "video", None)
            or getattr(message, "document", None)
            or getattr(message, "audio", None)
            or getattr(message, "photo", None)
        )
        return getattr(media, "file_unique_id", "") or ""

    @staticmethod
    def _dump_filename(dump_caption: str | None) -> str | None:
        if not dump_caption:
            return None
        first_line = dump_caption.split("\n\n", 1)[0].strip()
        first_line = re.sub(r"^<[^>]+>|<[^>]+>$", "", first_line).strip()
        return unescape(first_line) or None

    def _delivery_caption(self, task: dict, dump_caption: str | None) -> str | None:
        filename = self._dump_filename(dump_caption)
        if filename is None:
            return dump_caption

        # The caption template is frozen into the task at enqueue time (see
        # `_build_task` in rename.py) so that a user changing their caption
        # settings mid-queue can't affect files already queued. `task.get(...)`
        # returns None only for tasks created before this field existed; those
        # legacy tasks fall back to the static default template rather than a
        # live UserSettings lookup, so the worker never re-queries UserSettings
        # for a task once it has been queued.
        template = task.get("caption_template")
        if template is None:
            template = DEFAULT_CAPTION_TEMPLATE

        if "{filename}" in template:
            return template.replace("{filename}", filename)
        return template

    @staticmethod
    async def _recently_delivered(client, user_id: int, file_unique_id: str) -> bool:
        try:
            async for msg in client.get_chat_history(user_id, limit=5):
                media = (
                    getattr(msg, "video", None)
                    or getattr(msg, "document", None)
                    or getattr(msg, "audio", None)
                    or getattr(msg, "photo", None)
                )
                if media and getattr(media, "file_unique_id", None) == file_unique_id:
                    return True
        except Exception:
            logger.warning("[Worker] Recent-history duplicate check failed.", exc_info=True)
        return False

    async def _resend_media(self, client, user_id: int, source, caption: str | None) -> None:
        if getattr(source, "video", None):
            await call_with_flood_retry(
                client.send_video, chat_id=user_id, video=source.video.file_id,
                caption=caption, parse_mode=enums.ParseMode.HTML, supports_streaming=True,
            )
        elif getattr(source, "document", None):
            await call_with_flood_retry(
                client.send_document, chat_id=user_id, document=source.document.file_id,
                caption=caption, parse_mode=enums.ParseMode.HTML, force_document=True,
            )
        elif getattr(source, "audio", None):
            await call_with_flood_retry(
                client.send_audio, chat_id=user_id, audio=source.audio.file_id, caption=caption,
                parse_mode=enums.ParseMode.HTML,
            )
        elif getattr(source, "photo", None):
            await call_with_flood_retry(
                client.send_photo, chat_id=user_id, photo=source.photo.file_id, caption=caption,
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            raise RuntimeError("Dump message contains no supported media for delivery.")


    async def _send_completion_to_group(self, task: dict, job: dict, file_path: str):
        source_chat_id = task.get("source_chat_id")
        if not source_chat_id:
            return
        if source_chat_id == task.get("user_id"):
            return
        text = ""
        try:
            file_size_bytes = os.path.getsize(file_path) if os.path.exists(file_path) else 0
            if file_size_bytes >= 1024 ** 3:
                size_str = f"{file_size_bytes / (1024**3):.2f} GB"
            elif file_size_bytes >= 1024 ** 2:
                size_str = f"{file_size_bytes / (1024**2):.2f} MB"
            else:
                size_str = f"{file_size_bytes / 1024:.2f} KB"

            elapsed = int(time.time() - task.get("_start_time", time.time()))
            if elapsed < 60:
                elapsed_str = f"{elapsed}s"
            elif elapsed < 3600:
                elapsed_str = f"{elapsed // 60}m {elapsed % 60}s"
            else:
                h, r = divmod(elapsed, 3600)
                elapsed_str = f"{h}h {r // 60}m {r % 60}s"

            text = (
                f"<b>▸ Delivered</b>\n"
                f"────────────────\n"
                f"┃ File : <code>{escape(job['output_filename'])}</code>\n"
                f"┠ Size : <code>{size_str}</code>\n"
                f"┠ Elapsed : <code>{elapsed_str}</code>\n"
                f"┖ <i>Sent to your Bot PM (Private)</i>"
            )

            await call_with_flood_retry(
                self.client.send_message,
                chat_id=source_chat_id, text=text, parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("[Worker] Completion message failed: %s", e)


    async def _snapshot_thumbnail(self, task: dict):
        # `setdefault` both reads the block created at enqueue time (see
        # `_build_task` in rename.py) and, for older persisted tasks that
        # predate task_assets, creates it here so the rest of the pipeline
        # only ever has one place to look.
        task_assets = task.setdefault("task_assets", {})
        src = task_assets.get("thumbnail_path") or task.get("thumbnail_path", "")
        if not src or not os.path.exists(src):
            return
        task_folder = os.path.join(self.temp_base, task["task_id"])
        frozen_path = os.path.join(task_folder, f"thumbnail_{task['task_id']}.jpg")
        if os.path.abspath(src) == os.path.abspath(frozen_path):
            return
        os.makedirs(task_folder, exist_ok=True)
        try:
            shutil.copy2(src, frozen_path)
            task_assets["thumbnail_path"] = frozen_path
        except Exception as e:
            print(f"[Worker] Thumbnail snapshot failed: {e}")

    async def _resolve_thumbnail(self, task: dict, job: dict) -> str | None:
        auto_detect = bool(task.get("auto_detect_thumb", False))
        task_assets = task.get("task_assets") or {}
        # `task_assets` is the current, authoritative location; the flat
        # `task`/`job` keys are only read as a fallback for tasks that were
        # queued (and persisted) before task_assets existed.
        user_thumb  = (
            task_assets.get("thumbnail_path")
            or task.get("thumbnail_path")
            or job.get("thumbnail_path")
            or ""
        )
        task_folder = os.path.join(self.temp_base, task["task_id"])

        if not auto_detect:
            return await self._normalized_thumbnail(user_thumb, task_folder)

        source_thumb_id = task.get("source_thumbnail_file_id", "")
        if source_thumb_id:
            os.makedirs(task_folder, exist_ok=True)
            dest = os.path.join(task_folder, "_source_thumb.jpg")
            try:
                downloaded = await call_with_flood_retry(
                    self.client.download_media, source_thumb_id, file_name=dest,
                )
                if downloaded and os.path.exists(downloaded):
                    return await self._normalized_thumbnail(os.path.abspath(downloaded), task_folder)
            except Exception:
                logger.warning(
                    "[Worker] Auto-detected thumbnail download failed for task %s "
                    "after retries; falling back to the saved thumbnail.",
                    task["task_id"][:8], exc_info=True,
                )

        return await self._normalized_thumbnail(user_thumb, task_folder)

    _THUMB_MAX_BYTES = 190 * 1024
    _THUMB_MAX_SIDE  = 320
    _THUMB_MIN_QUALITY = 30

    def _build_normalized_thumbnail(self, path: str, normalized_path: str) -> bool:
        """Synchronous, CPU-bound Pillow work - run via asyncio.to_thread so it
        doesn't block the event loop. Returns True only if `normalized_path` was
        written and satisfies both the max-side and max-byte-size constraints.
        """
        try:
            with Image.open(path) as img:
                img = img.convert("RGB")
                img.thumbnail((self._THUMB_MAX_SIDE, self._THUMB_MAX_SIDE), Image.LANCZOS)

                quality = 90
                while quality >= self._THUMB_MIN_QUALITY:
                    img.save(normalized_path, "JPEG", quality=quality, optimize=True)
                    if os.path.getsize(normalized_path) <= self._THUMB_MAX_BYTES:
                        return True
                    quality -= 10
                return False
        except Exception:
            logger.warning("[Worker] Thumbnail normalization errored for %s.", path, exc_info=True)
            return False

    async def _normalized_thumbnail(self, path: str, task_folder: str) -> str | None:
        """Return a task-owned thumbnail path that is guaranteed to satisfy
        Telegram's thumbnail constraints (JPEG, each side <= _THUMB_MAX_SIDE,
        file size <= _THUMB_MAX_BYTES), or None if no such thumbnail could be
        produced.

        This never falls back to returning an unverified/oversized thumbnail:
        a normalization failure is treated as "no thumbnail for this upload"
        (explicit, logged) rather than silently handing Telegram something
        that may be rejected or dropped.
        """
        if not path or not os.path.exists(path):
            return None

        os.makedirs(task_folder, exist_ok=True)
        normalized_path = os.path.join(task_folder, "_thumb_normalized.jpg")

        ok = await asyncio.to_thread(self._build_normalized_thumbnail, path, normalized_path)

        if not ok or not os.path.exists(normalized_path) or os.path.getsize(normalized_path) > self._THUMB_MAX_BYTES:
            logger.warning(
                "[Worker] Could not produce a thumbnail for %s within Telegram's "
                "size/dimension limits; uploading without a thumbnail.", path,
            )
            try:
                if os.path.exists(normalized_path):
                    os.remove(normalized_path)
            except OSError:
                pass
            return None

        return normalized_path


    def _cleanup_task_folder(self, task_id: str):
        folder = os.path.join(self.temp_base, task_id)
        if os.path.exists(folder):
            shutil.rmtree(folder, ignore_errors=True)

    def _ensure_runtime_directories(self) -> None:
        os.makedirs(self.temp_base, exist_ok=True)
        os.makedirs(self.thumbnails_dir, exist_ok=True)

    async def _get_user_premium(self, user_id: int) -> bool:
        try:
            user = await self.client.get_users(user_id)
            return bool(getattr(user, "is_premium", False))
        except Exception:
            return False

    def _build_job(self, task: dict) -> dict:
        settings_snapshot = task.get("settings_snapshot") or {}
        task_assets = task.get("task_assets") or {}
        jobs = task.get("jobs") or []
        job_snapshot = jobs[0] if jobs else {}
        return {
            "output_filename": task["output_filename"],
            "metadata":        deepcopy(settings_snapshot.get("metadata", job_snapshot.get("metadata", {}))),
            "thumbnail_path":  (
                task_assets.get("thumbnail_path")
                or task.get("thumbnail_path")
                or job_snapshot.get("thumbnail_path", "")
            ),
            "send_type":       task.get("send_type") or job_snapshot.get("send_type", settings_snapshot.get("send_type", "media")),
        }

    async def _notify_user(self, user_id: int, text: str):
        try:
            await call_with_flood_retry(self.client.send_message, user_id, text)
        except Exception as e:
            logger.warning("[Worker] Notify failed: %s", e)


    async def cancel_task(self, task_id: str):
        task = self.task_queue.get_task(task_id)

        active_task = self._active_tasks.get(task_id)
        if active_task and not active_task.done():
            active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)
            return

        delivery_task = self._delivery_tasks.get(task_id)
        if delivery_task and not delivery_task.done():
            delivery_task.cancel()
            await asyncio.gather(delivery_task, return_exceptions=True)

        task = self.task_queue.get_task(task_id)
        if task:
            self.task_queue.remove_task(task_id, final_status="cancelled")
            self._cleanup_task_folder(task_id)
            await self._notify_user(
                task["user_id"],
                f"<b>▸ Task Cancelled</b>\n"
                f"────────────────\n"
                f"Task <code>{task_id[:8]}</code> was <u>cancelled</u>.",
            )