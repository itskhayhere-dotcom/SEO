"""python -m app.hashpw  -> prints the APP_PASSWORD_HASH line for .env"""
import getpass
from .auth import hash_password
a, b = getpass.getpass("New password: "), getpass.getpass("Repeat: ")
if a != b or len(a) < 10:
    raise SystemExit("Passwords differ or are shorter than 10 characters.")
print("\nAdd this line to .env:\nAPP_PASSWORD_HASH=" + hash_password(a))
