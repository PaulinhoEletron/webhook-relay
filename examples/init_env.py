"""Generate private local credentials once; never print their values."""

import os
import secrets
from pathlib import Path

path = Path(".env")
password = secrets.token_hex(24)
content = (
    Path(".env.example")
    .read_text()
    .replace("replace-with-random-database-password", password)
    .replace("replace-with-at-least-32-random-characters", secrets.token_hex(32))
    .replace("replace-with-another-32-random-characters", secrets.token_hex(32))
)
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print(".env already exists; left unchanged")
else:
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    print("Created .env with random local credentials")
