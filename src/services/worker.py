import asyncio
import os
import shutil
import time
import logging
from copy import deepcopy

from pyrogram import Client

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
        # Fingerprint of the SESSION_STRING the current premium client was
        # built from. `__main__.py` and `Worker.start()` both call
        # `configure_premium_download_session()` on startup; now that the
        # session is file-backed (see session_persistence.py) instead of
        # in-memory, letting that redundant second call spin up another
        # Client on the *same* on-disk session file would mean two
        # concurrent sqlite connections to one file -- so a repeat call
        # with an unchanged SESSION_STRING becomes a no-op below.
        self._premium_session_fingerprint: str | None = None

        # Bot-side dump. This belongs exclusively to BOT_TOKEN.
        self._dump_chat_id: int | None = None

        # Premium-side view of the same dump channel. This is only populated
        # when SESSION_STRING is configured and DUMP_CHAT_ID is resolved.
        self._premium_dump_chat_id: int | None = None
        self.config = config
        self.media_processor = MediaProcessor(config.paths.ffmpeg)
        self.temp_base = config.paths.tmp
        self.thumbnails_dir = config.paths.thumbnails
        self.running = False

        # WORKERS is the single concurrency knob for the whole bot: it is
        # the number of complete job lifecycles admitted to the global pool
        # AND the cap for each stage (download, upload, watermark
        # processing). Premium and normal tasks share the same pool, so
        # e.g. WORKERS=4 means at most 4 jobs total (premium + normal
        # combined), at most 4 downloads at a time, and at most 4 uploads
        # at a time -- never more than that.
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
        # Premium and normal uploads share one pool-sized semaphore -- see
        # the WORKERS comment above; there's no separate premium cap.
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
        # Delivery (dump -> user copy_message) now runs as its own follow-up
        # task instead of blocking a pool worker slot for the whole
        # copy_message() round trip -- see _spawn_delivery()/_finalize_delivery().
        self._delivery_tasks: dict[str, asyncio.Task] = {}

        # Multiple pool workers can pick up tasks from the same source
        # channel within milliseconds of each other (a typical batch
        # rename). The FIRST forward_messages()/get_chat() call against a
        # chat the client hasn't seen yet triggers Telegram's peer
        # resolution (GetChannels); if several workers fire that call
        # concurrently before the first one finishes caching the peer, the
        # others get a spurious CHANNEL_INVALID even though the chat is
        # perfectly valid. These per-chat locks make concurrent tasks wait
        # for the first resolution instead of racing it.
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
        """Warm a client's peer cache for chat_id, serialized so concurrent
        tasks don't race Telegram's first-time GetChannels resolution.

        If plain resolution fails, this also tries to actually get
        `client` INTO the chat -- not just look it up -- since a client
        that has never been a member (typically the Premium session on a
        chat only the bot account has been active in) can't resolve a
        bare numeric ID at all: Telegram requires a username or invite
        link to join, and without joining there's no access_hash to be
        had. `chat_username` (captured at task-creation time from the
        source chat, if it's public) is what makes that possible; for a
        private chat with no username, we try to have `self.client` (the
        bot) mint an invite link, since it's normally already active/
        admin in whatever chat the source message came from.
        """
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
                # Resolution failure here isn't fatal on its own -- the
                # caller's real operation (forward_messages, etc.) will
                # raise its own, more specific error if the chat truly
                # can't be reached. This is best-effort cache warming.
                logger.debug(
                    "[Worker] Peer warm-up get_chat(%s) still failing after "
                    "a join attempt; letting the caller's own call surface "
                    "the real error.",
                    chat_id, exc_info=True,
                )

    async def _try_join_source_chat(
        self, client: Client, chat_id, chat_username: str | None
    ) -> str | None:
        """Best-effort: get `client` into `chat_id` so it can resolve it.

        Returns a short description of how it joined on success, or None
        if no join was possible/needed with the information available.
        Never raises -- this is a fallback path, and the caller always
        re-attempts `get_chat()` afterward regardless of the outcome here.
        """
        if chat_username:
            try:
                await client.join_chat(chat_username)
                return f"username @{chat_username}"
            except Exception:
                logger.debug(
                    "[Worker] join_chat(@%s) for source chat %s failed.",
                    chat_username, chat_id, exc_info=True,
                )

        # No public username: see if the bot side (which the source
        # message came through, and is normally already active/admin
        # there) can mint an invite link for the Premium session to use.
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
        """Warm `client`'s peer cache for `user_id` before it DMs them.

        The bot client never needs this: a user necessarily already
        messaged the bot to create the task, so the bot's peer cache is
        always warm for them. The Premium session is different -- it's
        only ever used for files >2 GiB, and it typically has *no* prior
        interaction with the requesting user at all (no shared dialog, no
        mutual contact). Telegram then rejects a bare numeric chat_id with
        PEER_ID_INVALID ("make sure you meet the peer before interacting
        with it"), because the client has no access_hash for that peer.

        Resolving by @username (captured at task-creation time) gives the
        client a real, non-"min" access_hash it can use afterward. This is
        the same trick Telegram clients use internally: `users.getUsers`/
        `contacts.resolveUsername` by public username always works,
        regardless of prior contact. If the user has no public username,
        there is no automatic fix -- Telegram requires the Premium
        account to have *some* prior visibility into that user (they'd
        need to start a chat with the Premium account once) before it can
        message them. That limitation is surfaced via a clear error
        instead of a raw PEER_ID_INVALID further down the call stack.
        """
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

        # The bot dump is always initialized first because it is the final
        # delivery staging area. Premium uploads are copied into this dump
        # before BOT_TOKEN sends them to the user.
        await self._initialize_dump_chat()

        # Initialize the optional Premium user session BEFORE accepting any
        # queued work. This makes SESSION_STRING effective without requiring
        # application code to remember a second initialization call.
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
        """Configure the optional Premium user session.

        BOT_TOKEN remains responsible for normal bot operations and the
        bot-side dump. DUMP_CHAT_ID is used only by this Premium user session.
        """
        session_string = (session_string or "").strip()
        if not session_string:
            raise ValueError("SESSION_STRING is empty.")

        new_fingerprint = _session_fingerprint(session_string)
        if (
            self._premium_download_client is not None
            and self._premium_session_fingerprint == new_fingerprint
            and self._premium_download_client.is_connected
        ):
            # Redundant call with the same credential (this legitimately
            # happens once at startup -- see the comment in __init__).
            # The existing client already owns the on-disk session file;
            # don't open a second connection to it.
            logger.debug(
                "[Worker] configure_premium_download_session() called again "
                "with an unchanged SESSION_STRING; reusing the existing "
                "premium client."
            )
            return await self._premium_download_client.get_me()

        # Premium uploads go directly to the bot-visible dump. This is the
        # canonical staging chat because BOT_TOKEN must later copy the message
        # into the user's DM. DUMP_CHAT_ID remains a fallback for old configs.
        premium_dump_target = getattr(self.config, "bot_dump_chat_id", None)
        if not premium_dump_target:
            premium_dump_target = getattr(self.config, "dump_chat_id", None)
        if not premium_dump_target:
            raise ValueError(
                "BOT_DUMP_CHAT_ID (or DUMP_CHAT_ID) is required when SESSION_STRING is configured."
            )

        # As with the bot Client: passing `session_string` to the
        # constructor forces MemoryStorage regardless of `in_memory`, so
        # this client's peer cache -- the access-hash table forward_messages
        # depends on to reach a source channel -- was wiped on every
        # restart. Materialize a persistent on-disk session from
        # SESSION_STRING once, then start the real Client on that file
        # (no `session_string=`) so peers resolved during this run are
        # written straight to disk and survive the next restart.
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
            # Same reasoning as the bot Client: this is a single shared cap
            # for downloads AND uploads on this session, so it must cover
            # both stages running at once (WORKERS each), not just one.
            max_concurrent_transmissions=self.workers * 2,
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

    async def _prepare_dump_chat(self, client: Client, configured_chat: str) -> int:
        """Resolve the Premium-side dump target.

        A private invite URL can be joined by the user session. A numeric
        private ID requires the Premium account to already be a member.
        """
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
        self.download_client = self.client
        for client in clients:
            if not client:
                continue
            try:
                await client.stop()
            except Exception:
                logger.warning("Could not stop Premium download session cleanly.")

    async def clear_for_restart(self) -> None:
        """Cancel all work and remove only task scratch data before a clean restart."""
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
        # Pool workers block on the same pending queue. Each worker owns one
        # complete task until its upload and delivery finish.
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
                # _run_pipeline_task handles task failure and user notification.
                pass

    # ── Bounded transfer pipeline ────────────────────────────────────────────

    def _refresh_current_task(self):
        self.task_queue.current_task = next(iter(self._active_tasks), None)

    @staticmethod
    def _has_watermark(task: dict) -> bool:
        watermark = task.get("watermark", {})
        return bool(watermark.get("enabled") and str(watermark.get("text", "")).strip())

    def _uses_premium_download(self, task: dict) -> bool:
        # Kept for compatibility with callers, but download routing is never
        # selected from file size. HyperTG is the single download path.
        return False

    def _uses_premium_dump(self, task: dict) -> bool:
        return False

    async def _resolve_bot_dump_chat(self, configured_chat: str):
        """Resolve BOT_DUMP_CHAT_ID using the bot session.

        Accepted targets:
        - numeric -100... chat ID
        - public @username
        - Telegram private invite URL

        For an invite URL, get_chat() is attempted first. If the bot has not
        joined yet, join_chat() is attempted. Pyrogram documents join_chat()
        as usable by bots and accepts t.me invite links.
        """
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

        # 1. Try direct resolution first. This works for numeric IDs,
        # usernames, and invite links when the bot already has access.
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
                # A ChatPreview means the bot can see the target but may not
                # have joined yet. For invite links, attempt to join.
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

        # 2. Invite-link path.
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

        # 3. Numeric ID: Pyrogram can only build a peer reference for a bare
        # ID if it already has that chat's access_hash cached from a prior
        # interaction (a message/update the bot has seen). A bot that was
        # only added as admin, without ever receiving anything from that
        # chat, has no cached peer and get_chat(id) fails even though the
        # bot genuinely has access. get_dialogs() walks every chat the bot
        # is a member of and caches each one as a side effect, so it warms
        # the peer cache without requiring a username or invite link.
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
        """Initialize the mandatory BOT_TOKEN-side dump.

        DUMP_CHAT_ID is deliberately not read here. It belongs only to the
        optional Premium SESSION_STRING client.
        """
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
            # The peer itself has already been resolved. A membership-status
            # lookup failure should not be reported as PEER_ID_INVALID.
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

        # The pipeline has ONE download implementation: HyperTG.  The dump is
        # only the staging transport immediately before it.  Normal-size media
        # is staged with the bot; oversized media must be staged with the
        # Premium user session because the bot cannot upload a >2 GiB message.
        # This keeps download logic completely independent of the 2 GiB limit.
        if file_size > BOT_DOWNLOAD_LIMIT and not self.has_premium_download_session:
            # There is no legal way for BOT_TOKEN to upload the >2 GiB source
            # into the dump.  Fall back to the original source message so the
            # same HyperTG downloader can still download it without Premium.
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

        # Warm/serialize peer resolution for the SOURCE chat before
        # forwarding. Without this, several pool workers picking up files
        # from the same never-before-seen source channel at once can race
        # Telegram's first GetChannels lookup and get a transient
        # CHANNEL_INVALID even though the channel is fine. This also
        # covers a client (typically the Premium session) that has never
        # been a member of the source chat at all: see _try_join_source_chat.
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
                # One more attempt: force a fresh peer lookup (bypassing the
                # lock's "someone already tried" shortcut isn't needed here
                # since the lock has already been released and re-acquired)
                # and retry the forward once. This clears the rare case
                # where the first resolution genuinely failed rather than
                # merely raced.
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
            # forward_messages() returned here without raising -- so this
            # isn't the CHANNEL_INVALID/peer-resolution failure mode above,
            # it's Telegram accepting the call but handing back nothing to
            # forward. The most common cause is the source message no
            # longer existing by the time this task was staged (deleted by
            # the user, or an auto-delete timer in the source chat).
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
        # There is intentionally no artificial 4 GiB download guard here.
        # The downloader will use the available MTProto/HyperTG path and let
        # Telegram report a genuine source-side limit or access error.

        # Stage into the dump first when the account capability permits it.
        # This is an upload/staging operation, not a download implementation.
        self.task_queue.update_status(task_id, "staging_to_dump", 0)
        await self._stage_source_to_dump(task)

        # Every file then enters the exact same HyperTG downloader path.
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

            # Resume-only path: the previous run of this task already got the
            # finished file all the way into the dump chat (recorded right
            # after Uploader.upload() succeeds, see _upload()) but the process
            # restarted/crashed before the task could be marked "completed".
            # Re-running the WHOLE pipeline here would re-download, re-process
            # and re-upload a file that's already sitting in the dump, and
            # then deliver it to the user a second time. Since the dump
            # message IDs survive in the checkpoint, just resume delivery.
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
            # All uploads (premium or normal, large or small) share the one
            # WORKERS-sized upload semaphore -- see the WORKERS comment in
            # Worker.__init__.
            async with self._upload_slot:
                self._uploading_task_ids.add(task_id)
                try:
                    await self._upload(task, upload_path, job)
                finally:
                    self._uploading_task_ids.discard(task_id)

            # The file is now safely archived in the dump (dump_upload_done +
            # dump_message_ids are already checkpointed inside _upload()).
            # Delivering it to the user runs as its own follow-up task rather
            # than inline here, so this pool worker slot frees up immediately
            # for the next queued task instead of sitting idle through a
            # copy_message() round trip.
            self._spawn_delivery(task)
            return
        except asyncio.CancelledError:
            if not self.running:
                # self.running is flipped to False at the top of stop(), before
                # it cancels every active task. Reaching this branch means the
                # BOT PROCESS is shutting down/restarting (SIGTERM, dyno cycle,
                # redeploy, crash) — the user never asked to cancel anything.
                # Do NOT archive/notify as "cancelled": that would permanently
                # delete the Mongo checkpoint this task needs to resume, and
                # would incorrectly tell the user their task was cancelled.
                # Leave the checkpoint and any partial download/prepared file
                # on disk exactly as-is so restore_task() can pick it back up
                # as "queued" on the next startup.
                logger.info(
                    "[Worker] Task %s interrupted by shutdown; leaving it for resume.",
                    task_id[:8],
                )
                raise
            # A real user-initiated cancel (via /cancel or the Cancel All
            # button) goes through cancel_task(), which also cancels this
            # same asyncio task — that's the only other way to land here
            # while self.running is still True.
            await self._notify_user(task["user_id"], f"⚠️ Task `{task_id[:8]}` was cancelled.")
            self.task_queue.remove_task(task_id, final_status="cancelled")
            self._cleanup_task_folder(task_id)
        except Exception as exc:
            logger.exception("[Worker] Task %s failed", task_id[:8])
            failure_text = str(exc).strip()
            if len(failure_text) > 300:
                failure_text = failure_text[-300:]
            await self._notify_user(task["user_id"], f"❌ Task `{task_id[:8]}` failed.\n{failure_text}")
            self.task_queue.remove_task(task_id, final_status="failed", error=str(exc))
            self._cleanup_task_folder(task_id)
        finally:
            self._uploading_task_ids.discard(task_id)
            self._active_tasks.pop(task_id, None)
            self._refresh_current_task()

    async def _prepare_upload_file(self, task: dict, downloaded_path: str, job: dict) -> str:
        metadata     = job.get("metadata") or task.get("metadata", {})
        watermark    = task.get("watermark", {})
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
                watermark=watermark,
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

        # Upload routing must be based on the ACTUAL prepared output size, not
        # the original source size. Processing can make the output larger or
        # smaller than the source file.
        actual_upload_size = os.path.getsize(file_path)
        # Premium is selected only for files above the bot's 2 GiB single-
        # message ceiling and only when a real Premium SESSION_STRING client
        # is available. Otherwise the upload is performed by the bot in
        # multiple FFmpeg-created parts.
        use_premium_dump = (
            actual_upload_size > BOT_DOWNLOAD_LIMIT and self.has_premium_download_session
        )

        if use_premium_dump:
            # The Premium account performs the large-file upload, but the
            # destination is the bot-visible dump so BOT_TOKEN can copy the
            # resulting message to the user's DM.
            upload_client = self._premium_download_client
            upload_chat_id = self._dump_chat_id
        else:
            upload_client = self.client
            upload_chat_id = self._dump_chat_id

        output_filename = job["output_filename"]

        # User-requested policy:
        #   bot/no Premium: >1.95 GiB => FFmpeg parts of <=1.95 GiB
        #   Premium session: >3.95 GiB => FFmpeg parts of <=3.95 GiB
        # Files at or below the applicable threshold are uploaded whole.
        if use_premium_dump and actual_upload_size <= PREMIUM_PART_SIZE:
            upload_parts = [(file_path, output_filename)]
        elif (not use_premium_dump) and actual_upload_size <= BOT_PART_SIZE:
            upload_parts = [(file_path, output_filename)]
        else:
            max_part_size = PREMIUM_PART_SIZE if use_premium_dump else BOT_PART_SIZE
            upload_parts = await self._split_output_for_bot(
                task, file_path, output_filename, max_part_size
            )

        results = []
        for part_path, part_name in upload_parts:
            upload_data = {
                **task,
                "upload_file_path": part_path,
                "output_filename": part_name,
                "send_type": job.get("send_type", "media"),
                "thumbnail_path": await self._resolve_thumbnail(task, job),
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

            # Both bot and Premium uploads now land in BOT_DUMP_CHAT_ID.
            # The returned message IDs are therefore directly copyable.
            results.extend(part_results)

        # Persist the dump message IDs BEFORE attempting final delivery, and
        # remember whether the Premium session put them there. If the process
        # crashes/restarts anywhere after this point, _run_pipeline_task's
        # resume path (see above) will re-enter here without re-downloading,
        # re-processing, or re-uploading anything.
        task["dump_message_ids"] = [r.id for r in results]
        task["dump_upload_done"] = True
        task["dump_used_premium"] = use_premium_dump
        self.task_queue.checkpoint(task_id)

    def _spawn_delivery(self, task: dict) -> None:
        """Kick off dump -> user delivery as an independent follow-up task.

        Upload to the dump has already finished and is checkpointed
        (dump_upload_done/dump_message_ids), so the pipeline slot this task
        was using can be released right away -- delivery (copy_message to the
        user) runs concurrently instead of holding that slot for its round
        trip. The task moves to "forwarding" here and only becomes
        "completed" once _finalize_delivery() actually gets it to the user.
        """
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
            # Shutdown/user-cancel mid-delivery. The file is already safely
            # in the dump, so leave the checkpoint (dump_upload_done +
            # dump_message_ids) as-is rather than losing it -- the resume
            # branch in _run_pipeline_task picks delivery back up on the
            # next startup. cancel_task() handles the user-cancel case's
            # own cleanup/notification itself.
            raise
        except Exception as exc:
            logger.exception("[Worker] Delivery failed for task %s", task_id[:8])
            failure_text = str(exc).strip()
            if len(failure_text) > 300:
                failure_text = failure_text[-300:]
            await self._notify_user(
                task["user_id"], f"❌ Task `{task_id[:8]}` failed.\n{failure_text}"
            )
            self.task_queue.remove_task(task_id, final_status="failed", error=str(exc))
            self._cleanup_task_folder(task_id)

    async def _deliver_dump_messages(self, task: dict) -> None:
        """Copy/re-send every staged dump message to the user's DM.

        Large (>2 GiB) files were uploaded to the dump by the Premium
        session, and a bot token cannot send or copy files above that size —
        that's why 4 GiB tasks used to get stuck at this exact step. Delivery
        for those must go through the Premium client too; the bot only
        handles delivery for files it could have uploaded itself.

        Already-delivered parts (tracked in task["delivered_message_ids"])
        are skipped, so resuming after a restart never re-sends a part that
        made it through before the crash.
        """
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

        # Must happen before ANY send/history call on deliver_client below --
        # copy_message, get_chat_history (inside _recently_delivered), and
        # send_* (inside _resend_media) all fail with PEER_ID_INVALID the
        # same way if the Premium session has never resolved this user.
        await self._ensure_user_peer_resolved(
            deliver_client, task["user_id"], task.get("username")
        )

        for dump_id in dump_ids:
            if dump_id in delivered:
                continue

            source = await self._get_dump_message_with_retry(dump_id)
            source_unique_id = self._media_unique_id(source)

            delivered_ok = False
            try:
                await call_with_flood_retry(
                    deliver_client.copy_message,
                    chat_id=task["user_id"],
                    from_chat_id=self._dump_chat_id,
                    message_id=dump_id,
                    caption=self._clean_delivery_caption(getattr(source, "caption", None)),
                )
                delivered_ok = True
            except Exception as copy_exc:
                # copy_message may have actually gone through on Telegram's
                # side even though the client raised (timeout/connection
                # reset). Blindly re-sending here is exactly how the same
                # file ends up in a user's DM twice. Check recent history
                # for a message with the same file_unique_id before assuming
                # the copy really failed.
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
                    await self._resend_media(deliver_client, task["user_id"], source)
                    delivered_ok = True

            if delivered_ok:
                delivered.add(dump_id)
                task["delivered_message_ids"] = sorted(delivered)
                self.task_queue.checkpoint(task_id)

    async def _get_dump_message_with_retry(self, dump_id: int, attempts: int = 4):
        """Fetch a just-uploaded dump message, tolerating the brief
        read-after-write lag that can happen right after Uploader.upload()
        returns under concurrent load: get_messages() can momentarily come
        back empty, or return the message before its media attachment has
        fully synced. A short retry clears this almost every time; a real
        problem (message truly missing/deleted) still surfaces after the
        retries are exhausted, via the original error messages.
        """
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
    def _clean_delivery_caption(dump_caption: str | None) -> str | None:
        """Strip the internal tracking block (sender name / user ID / task
        ID) that Uploader adds to dump captions before the file reaches the
        end user -- they should just see their renamed filename, not
        delivery bookkeeping meant for the dump chat.
        """
        if not dump_caption:
            return dump_caption
        return dump_caption.split("\n\n", 1)[0] or dump_caption

    @staticmethod
    async def _recently_delivered(client, user_id: int, file_unique_id: str) -> bool:
        """Best-effort duplicate check: has this exact file already landed
        in the user's chat recently? Used only to decide whether a re-send
        fallback is actually needed after an ambiguous copy_message error.
        """
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

    @staticmethod
    async def _resend_media(client, user_id: int, source) -> None:
        caption = Worker._clean_delivery_caption(getattr(source, "caption", None))
        if getattr(source, "video", None):
            await call_with_flood_retry(
                client.send_video, chat_id=user_id, video=source.video.file_id,
                caption=caption, supports_streaming=True,
            )
        elif getattr(source, "document", None):
            await call_with_flood_retry(
                client.send_document, chat_id=user_id, document=source.document.file_id,
                caption=caption, force_document=True,
            )
        elif getattr(source, "audio", None):
            await call_with_flood_retry(
                client.send_audio, chat_id=user_id, audio=source.audio.file_id, caption=caption,
            )
        elif getattr(source, "photo", None):
            await call_with_flood_retry(
                client.send_photo, chat_id=user_id, photo=source.photo.file_id, caption=caption,
            )
        else:
            raise RuntimeError("Dump message contains no supported media for delivery.")

    # ── Completion message ────────────────────────────────────────────────────

    async def _send_completion_to_group(self, task: dict, job: dict, file_path: str):
        source_chat_id = task.get("source_chat_id")
        if not source_chat_id:
            return
        # The task was queued from the user's own DM (issue: DM support), so
        # the finished file already lands in the only chat there is. Sending
        # a second "delivered to your PM" notice into that same DM would just
        # be a redundant message right above the file itself.
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
                f"`{job['output_filename']}`\n"
                f"┠ **Size:** {size_str}\n"
                f"┠ **Elapsed:** {elapsed_str}\n"
                f"➲ File has been Sent to Bot PM (Private)"
            )

            await call_with_flood_retry(
                self.client.send_message,
                chat_id=source_chat_id, text=text, disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("[Worker] Completion message failed: %s", e)

    # ── Thumbnail helpers ─────────────────────────────────────────────────────

    async def _snapshot_thumbnail(self, task: dict):
        src = task.get("thumbnail_path", "")
        if not src or not os.path.exists(src):
            return
        task_folder = os.path.join(self.temp_base, task["task_id"])
        frozen_path = os.path.join(task_folder, f"thumbnail_{task['task_id']}.jpg")
        if os.path.abspath(src) == os.path.abspath(frozen_path):
            return
        os.makedirs(task_folder, exist_ok=True)
        try:
            shutil.copy2(src, frozen_path)
            task["thumbnail_path"] = frozen_path
        except Exception as e:
            print(f"[Worker] Thumbnail snapshot failed: {e}")

    async def _resolve_thumbnail(self, task: dict, job: dict) -> str | None:
        auto_detect = bool(task.get("auto_detect_thumb", False))
        user_thumb  = task.get("thumbnail_path") or job.get("thumbnail_path") or ""
        task_folder = os.path.join(self.temp_base, task["task_id"])

        if not auto_detect:
            return await self._normalized_thumbnail(user_thumb, task_folder)

        source_thumb_id = task.get("source_thumbnail_file_id", "")
        if source_thumb_id:
            os.makedirs(task_folder, exist_ok=True)
            dest = os.path.join(task_folder, "_source_thumb.jpg")
            try:
                downloaded = await self.client.download_media(source_thumb_id, file_name=dest)
                if downloaded and os.path.exists(downloaded):
                    return await self._normalized_thumbnail(os.path.abspath(downloaded), task_folder)
            except Exception:
                pass

        return await self._normalized_thumbnail(user_thumb, task_folder)

    _THUMB_MAX_BYTES = 190 * 1024   # stay under Telegram's 200 KB cap
    _THUMB_MAX_SIDE  = 320          # Telegram requires both sides <= 320px

    async def _normalized_thumbnail(self, path: str, task_folder: str) -> str | None:
        """Re-encode a thumbnail to what Telegram actually requires.

        Telegram silently drops a thumbnail that isn't a JPEG under 200 KB
        with both dimensions <= 320px, depending on media type and client —
        which is why a custom/auto-detected thumbnail appears to "not apply"
        for some files in a batch even though the code picked a valid file
        path for every one of them. Re-encoding every resolved thumbnail
        through ffmpeg here makes it always compliant instead of only
        sometimes, regardless of what the source image actually looked like.
        """
        if not path or not os.path.exists(path):
            return None
        try:
            if os.path.getsize(path) <= self._THUMB_MAX_BYTES:
                return path
        except OSError:
            return None

        os.makedirs(task_folder, exist_ok=True)
        normalized_path = os.path.join(task_folder, "_thumb_normalized.jpg")
        cmd = [
            self.media_processor.ffmpeg_path, "-hide_banner", "-y",
            "-i", path,
            "-vf", f"scale='min({self._THUMB_MAX_SIDE},iw)':'min({self._THUMB_MAX_SIDE},ih)':force_original_aspect_ratio=decrease",
            "-vframes", "1",
            "-q:v", "4",
            normalized_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0 or not os.path.exists(normalized_path):
                logger.warning(
                    "[Worker] Thumbnail normalization failed (%s); using original.",
                    stderr.decode(errors="ignore")[-300:].strip(),
                )
                return path
            return normalized_path
        except Exception:
            logger.warning("[Worker] Thumbnail normalization errored; using original.", exc_info=True)
            return path

    # ── Misc helpers ──────────────────────────────────────────────────────────

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
        # Always build from the task's immutable enqueue-time snapshot. Never
        # consult the live per-user settings here: another task from the same
        # user may have changed those settings while this task was queued.
        settings_snapshot = task.get("settings_snapshot") or {}
        jobs = task.get("jobs") or []
        job_snapshot = jobs[0] if jobs else {}
        return {
            "output_filename": task["output_filename"],
            "metadata":        deepcopy(settings_snapshot.get("metadata", job_snapshot.get("metadata", {}))),
            "thumbnail_path":  task.get("thumbnail_path") or job_snapshot.get("thumbnail_path", ""),
            "send_type":       task.get("send_type") or job_snapshot.get("send_type", settings_snapshot.get("send_type", "media")),
        }

    async def _notify_user(self, user_id: int, text: str):
        try:
            await call_with_flood_retry(self.client.send_message, user_id, text)
        except Exception as e:
            logger.warning("[Worker] Notify failed: %s", e)

    # ── Cancel ────────────────────────────────────────────────────────────────

    async def cancel_task(self, task_id: str):
        task = self.task_queue.get_task(task_id)

        # Upload runs inside the task's pool slot, so cancelling the pipeline
        # task directly also cancels the active Telegram upload.
        active_task = self._active_tasks.get(task_id)
        if active_task and not active_task.done():
            active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)
            return

        # By the time a task reaches "forwarding", the pipeline task itself
        # has already finished (the file is safely uploaded to the dump) and
        # delivery is running as its own follow-up task -- cancel that instead.
        delivery_task = self._delivery_tasks.get(task_id)
        if delivery_task and not delivery_task.done():
            delivery_task.cancel()
            await asyncio.gather(delivery_task, return_exceptions=True)

        task = self.task_queue.get_task(task_id)
        if task:
            self.task_queue.remove_task(task_id, final_status="cancelled")
            self._cleanup_task_folder(task_id)
            await self._notify_user(task["user_id"], f"⚠️ Task `{task_id[:8]}` cancelled.")
