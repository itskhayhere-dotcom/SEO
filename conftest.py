import os, tempfile
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.update(ALLOW_PRIVATE_HOSTS="true", CRAWL_DELAY_SEC="0.01", SHEETS_BACKEND="apps_script")
