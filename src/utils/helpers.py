from pyrogram import Client
from dotenv import load_dotenv
import os

load_dotenv()

api_id = int(os.getenv("API_ID"))
api_hash = str(os.getenv("API_HASH"))
with Client(
    "my_account",
    api_id=api_id,
    api_hash=api_hash,
    in_memory=True
) as app:
    session_string = app.export_session_string()
    print("\nYour Session String:\n")
    print(session_string)

def progress_bar(percentage: float, length: int = 12):
    filled = int(length * percentage // 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"{bar} {percentage:.1f}%"