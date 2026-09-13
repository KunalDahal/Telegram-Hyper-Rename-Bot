import asyncio
import sys
import logging

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

_original_get_event_loop = asyncio.get_event_loop


def _patched_get_event_loop():
    try:
        return _original_get_event_loop()
    except RuntimeError:
        return _loop


asyncio.get_event_loop = _patched_get_event_loop

from pyrogram import Client, idle

from src import Config, TaskQueue, UserSettings
from src.core import AccessControl, UserSettingsStore
from src.services import Worker
from src.handlers.settings import setup_settings_handlers
from src.handlers.status import setup_status_handlers
from src.handlers.start import setup_start_handler
from src.handlers.cancel import setup_cancel_handlers, set_worker_instance
from src.handlers.restart import setup_restart_handler
from src.handlers.rename import setup_rename_handler
from src.handlers.set import setup_set_handlers
from src.utils.workers import sync_bot_command_scopes
from src.utils.premium import setup_premium_handlers
from src.utils.moderation import setup_moderation_handlers
from src.handlers.mi import setup_mediainfo_handlers
from src.utils.session_persistence import ensure_persistent_session
from src.utils.output_style import apply_global_styling

apply_global_styling()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


async def main():
    config = Config()
    config.paths.makedirs()

    logging.info(
        "[Bot] Session mode: %s",
        "BOT_SESSION_STRING" if config.bot_session_string else "file-based Pyrogram session",
    )

    await ensure_persistent_session(
        "encode_bot_session", config.paths.logs, config.bot_session_string
    )
    app = Client(
        "encode_bot_session",
        api_id=config.api_id,
        api_hash=config.api_hash,
        bot_token=config.bot_token,
        workdir=config.paths.logs,
        workers=32,
        max_concurrent_transmissions=config.upload_part_workers,
        in_memory=False,
    )

    task_queue = TaskQueue()
    access_control = AccessControl(
        config.owner_ids,
        config.mongo_uri,
        config.mongo_db_name,
    )
    user_settings_store = UserSettingsStore(
        config.mongo_uri,
        config.mongo_db_name,
    )
    _user_settings_cache: dict[int, UserSettings] = {}

    def get_user_settings(user_id: int) -> UserSettings:
        if user_id not in _user_settings_cache:
            _user_settings_cache[user_id] = UserSettings(
                user_id,
                config.paths,
                user_settings_store,
            )
        return _user_settings_cache[user_id]

    worker = None
    helper_bots = {}
    helper_loads = {}

    await app.start()
    logging.info(
        "[Bot] Pyrogram bot client connected and session started "
        "(persistent_session=%s).",
        bool(config.bot_session_string),
    )

    if config.sub_bot_tokens:
        for i, token in enumerate(config.sub_bot_tokens):
            helper_client = Client(
                f"sub_bot_session_{i}",
                api_id=config.api_id,
                api_hash=config.api_hash,
                bot_token=token,
                workers=32,
                workdir=config.paths.logs,
                in_memory=True,
            )
            await helper_client.start()
            helper_bots[i] = helper_client
            helper_loads[i] = 0
        logging.info(
            "[Bot] %d sub bot(s) connected for multi-client downloading.",
            len(helper_bots),
        )

    try:
        await access_control.initialize()
        user_settings_store.initialize()

        task_queue.set_task_store(access_control.task_store)

        saved_tasks = await access_control.task_store.load_active()
        recovered_count = sum(
            task_queue.restore_task(task) for task in saved_tasks
        )
        if recovered_count:
            logging.info(
                "Recovered %d unfinished task(s) from MongoDB.",
                recovered_count,
            )

        worker = Worker(
            task_queue, get_user_settings, app, config,
            helper_bots=helper_bots, helper_loads=helper_loads,
        )
        set_worker_instance(worker)

        if config.session_string:
            logging.info(
                "[Premium] SESSION_STRING present; initializing optional "
                "Premium user session."
            )
            await worker.configure_premium_download_session(
                config.session_string
            )
            logging.info(
                "[Premium] Premium download session enabled."
            )
        else:
            logging.info(
                "[Premium] SESSION_STRING not configured; Premium path disabled."
            )

        setup_set_handlers(
            app=app,
            user_settings=get_user_settings,
            config=config,
            access_control=access_control,
        )
        setup_rename_handler(
            app,
            task_queue,
            get_user_settings,
            config,
            access_control,
        )
        setup_cancel_handlers(
            app,
            task_queue,
            config,
            access_control,
        )
        setup_restart_handler(
            app,
            task_queue,
            config,
            access_control,
        )
        setup_status_handlers(
            app=app,
            task_queue=task_queue,
            config=config,
            access_control=access_control,
        )
        setup_start_handler(app, config, access_control)
        setup_settings_handlers(
            app=app,
            user_settings=get_user_settings,
            config=config,
            access_control=access_control,
            premium_session_checker=lambda: (
                worker.has_premium_download_session
            ),
        )
        setup_premium_handlers(
            app=app,
            config=config,
            access_control=access_control,
        )
        setup_moderation_handlers(
            app=app,
            config=config,
            access_control=access_control,
        )
        setup_mediainfo_handlers(
            app=app,
            config=config,
            access_control=access_control,
        )

        await sync_bot_command_scopes(app, config, access_control)

        worker_task = asyncio.create_task(
            worker.start(),
            name="telegram-rename-worker",
        )
        idle_task = asyncio.create_task(
            idle(),
            name="telegram-pyrogram-idle",
        )

        me = await app.get_me()
        print(
            f"""
    ╔══════════════════════════════════╗
    ║  @{me.username:<31}║
    ╚══════════════════════════════════╝
    """
        )

        try:
            done, _ = await asyncio.wait(
                {worker_task, idle_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if worker_task in done:
                worker_task.result()

            return

        finally:
            if idle_task and not idle_task.done():
                idle_task.cancel()

            if worker_task and not worker_task.done():
                worker_task.cancel()

            await asyncio.gather(
                idle_task,
                worker_task,
                return_exceptions=True,
            )

    finally:
        if worker is not None:
            try:
                await worker.stop()
            except Exception:
                logging.exception("Worker shutdown failed")

        try:
            await task_queue.flush_checkpoints()
        except Exception:
            logging.exception("Failed to flush task checkpoints")

        user_settings_store.close()
        access_control.close()

        for helper_client in helper_bots.values():
            if helper_client.is_connected:
                await helper_client.stop()

        if app.is_connected:
            await app.stop()


if __name__ == "__main__":
    try:
        _loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        _loop.close()
