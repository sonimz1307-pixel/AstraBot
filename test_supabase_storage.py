import os
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
BUCKET = os.getenv("SUPABASE_BUCKET", "kling")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

with open("test.txt", "w") as f:
    f.write("hello kling")

with open("test.txt", "rb") as f:
    sb.storage.from_(BUCKET).upload(
        "test/test.txt",
        f,
        file_options={"upsert": True}
    )

url = sb.storage.from_(BUCKET).get_public_url("test/test.txt")
print("PUBLIC URL:", url)
