import os
from functools import wraps
from typing import List, Callable
from dotenv import load_dotenv

load_dotenv()


def get_owner_ids() -> List[int]:
    owner_ids_str = os.getenv("OWNER_IDS", "")
    if not owner_ids_str:
        return []
    try:
        return [int(owner_id.strip()) for owner_id in owner_ids_str.split(",") if owner_id.strip()]
    except ValueError:
        return []


def admin_only(func: Callable) -> Callable:
    @wraps(func)
    async def wrapper(client, message, *args, **kwargs):
        user_id = message.from_user.id
        owner_ids = get_owner_ids()
        if user_id not in owner_ids:
            await message.reply_text("❌ You are not authorized to use this command.")
            return
        return await func(client, message, *args, **kwargs)
    return wrapper
