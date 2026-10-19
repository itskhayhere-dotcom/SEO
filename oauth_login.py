"""One-time OAuth login (installed-app flow) -> writes the token file used when SHEETS_BACKEND=oauth.
Usage: python scripts/oauth_login.py path/to/client_secret.json"""
import sys
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow  # pip install google-auth-oauthlib
SC = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.metadata.readonly"]
creds = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SC).run_local_server(port=0)
out = Path("secrets/oauth-token.json"); out.parent.mkdir(exist_ok=True); out.write_text(creds.to_json()); out.chmod(0o600)
print("saved", out)
