import os
from supabase import create_client

# === ENV ===
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")  # ВАЖНО: как у тебя в Render
BUCKET = os.getenv("SUPABASE_BUCKET", "kling")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is not set")

if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_KEY is not set")

print("ENV OK")

# === CLIENT ===
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# === CREATE LOCAL FILE ===
with open("test.txt", "w", encoding="utf-8") as f:
    f.write("hello kling")

print("Local file created")

# === UPLOAD ===
with open("test.txt", "rb") as f:
    sb.storage.from_(BUCKET).upload(
        path="test/test.txt",
        file=f,
        file_options={
            "content-type": "text/plain"
        }
    )

print("File uploaded to Supabase")

# === PUBLIC URL ===
url = sb.storage.from_(BUCKET).get_public_url("test/test.txt")
print("PUBLIC URL:", url)

