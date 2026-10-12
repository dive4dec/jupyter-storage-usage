"""Functional test: start a real Jupyter server in cleanup mode and test HTTP endpoints.

This tests the full request/response cycle — HTML rendering, delete via POST,
download via GET, path traversal blocking, and auth.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Setup: create a temp home dir with files ────────────────────────────

HOME = tempfile.mkdtemp(prefix="jsu_test_home_")
os.environ["JSU_HOME_DIR"] = HOME
os.environ["JSU_WARN_LIMIT_BYTES"] = "10240"   # 10 KiB
os.environ["JSU_HARD_LIMIT_BYTES"] = "20480"   # 20 KiB
os.environ["JSU_CLEANUP_MODE"] = "1"

# Create test files
os.makedirs(os.path.join(HOME, "homework"))
os.makedirs(os.path.join(HOME, ".cache"))
with open(os.path.join(HOME, "notebook.ipynb"), "w") as f:
    f.write('{"cells": []}')
with open(os.path.join(HOME, "homework", "hw1.py"), "w") as f:
    f.write("print('hello world')")
with open(os.path.join(HOME, ".cache", "big.bin"), "wb") as f:
    f.write(b"\x00" * 15000)  # 15KB — over hard limit
with open(os.path.join(HOME, "small.txt"), "w") as f:
    f.write("small file")

print(f"Test home: {HOME}")
print(f"Files created: {os.listdir(HOME)}")

# ── Start Jupyter server ────────────────────────────────────────────────

# We need jupyter_server installed — use the venv from jupyter-storage-usage
VENV_PYTHON = "/home/ccha23/dive-deploy/jupyter-storage-usage/.venv/bin/python"
PORT = 9876
TOKEN = "test-token-12345"

env = os.environ.copy()
env["JSU_CLEANUP_MODE"] = "1"
env["JSU_HOME_DIR"] = HOME
env["JUPYTERHUB_BASE_URL"] = "/"

# Start jupyter server with our extension
proc = subprocess.Popen(
    [VENV_PYTHON, "-m", "jupyter_server",
     "--ServerApp.port=" + str(PORT),
     "--ServerApp.token=" + TOKEN,
     "--ServerApp.ip=127.0.0.1",
     "--ServerApp.base_url=/",
     "--ServerApp.allow_origin=*",
     "--ServerApp.tornado_settings={}",
    ],
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    cwd="/home/ccha23/dive-deploy/jupyter-storage-usage",
)

# Wait for server to start
time.sleep(5)

# Check if it started
if proc.poll() is not None:
    out, err = proc.communicate()
    print(f"Server failed to start! exit={proc.returncode}")
    print(f"STDOUT: {out.decode()[:2000]}")
    print(f"STDERR: {err.decode()[:2000]}")
    sys.exit(1)

print(f"Jupyter server started on port {PORT}")

# ── HTTP helpers ────────────────────────────────────────────────────────

BASE = f"http://127.0.0.1:{PORT}"
COOKIES = {}

def fetch(path, method="GET", data=None, headers=None):
    """Make an authenticated HTTP request."""
    url = BASE + path
    h = {"Authorization": f"token {TOKEN}"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)

# ── Tests ────────────────────────────────────────────────────────────────

results = []

def test(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, condition))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not condition else ""))

# 1. Root redirect to /cleanup/
print("\n=== 1. Root redirect ===")
status, body, headers = fetch("/")
test("GET / redirects to /cleanup/", status == 200, f"status={status}")

# 2. Browse root directory
print("\n=== 2. Browse /cleanup/ ===")
status, body, headers = fetch("/cleanup/")
html = body.decode()
test("GET /cleanup/ returns 200", status == 200, f"status={status}")
test("HTML contains 'Storage Cleanup'", "Storage Cleanup" in html, html[:200])
test("HTML shows over-limit banner", "over" in html.lower(), html[:500])
test("HTML lists notebook.ipynb", "notebook.ipynb" in html, "")
test("HTML lists homework dir", "homework" in html, "")
test("HTML lists .cache dir", ".cache" in html, "")
test("HTML has delete buttons", "🗑️" in html or "Delete" in html, "")
test("HTML has download links", "⬇️" in html or "download" in html.lower(), "")
test("HTML has breadcrumb", "Home" in html or "🏠" in html, "")

# 3. Browse subdirectory
print("\n=== 3. Browse /cleanup/homework/ ===")
status, body, headers = fetch("/cleanup/homework/")
html = body.decode()
test("GET /cleanup/homework/ returns 200", status == 200, f"status={status}")
test("HTML lists hw1.py", "hw1.py" in html, "")
test("HTML has breadcrumb with homework", "homework" in html, "")

# 4. Browse file info page
print("\n=== 4. Browse file /cleanup/notebook.ipynb ===")
status, body, headers = fetch("/cleanup/notebook.ipynb")
html = body.decode()
test("GET file page returns 200", status == 200, f"status={status}")
test("HTML shows file name", "notebook.ipynb" in html, "")
test("HTML has download button", "Download" in html or "⬇️" in html, "")
test("HTML has delete button", "Delete" in html or "🗑️" in html, "")

# 5. Download a file
print("\n=== 5. Download file ===")
status, body, headers = fetch("/cleanup/notebook.ipynb?download=1")
test("Download returns 200", status == 200, f"status={status}")
test("Content-Disposition set", "attachment" in headers.get("Content-Disposition", ""), "")
test("Content is correct", body == b'{"cells": []}', f"got: {body[:50]}")

# 6. Download a directory as tar.gz
print("\n=== 6. Download directory as tar.gz ===")
status, body, headers = fetch("/cleanup/homework?download=1")
test("Dir download returns 200", status == 200, f"status={status}")
test("Content-Type is gzip", "gzip" in headers.get("Content-Type", ""), headers.get("Content-Type"))
test("Body starts with gzip magic", body[:2] == b"\x1f\x8b", f"first bytes: {body[:4]}")

# 7. Path traversal blocked
print("\n=== 7. Path traversal ===")
status, body, headers = fetch("/cleanup/../../etc/passwd")
test("Path traversal returns 403", status == 403, f"status={status}")

# 8. Delete a file
print("\n=== 8. Delete file ===")
assert os.path.exists(os.path.join(HOME, "small.txt"))
status, body, headers = fetch("/cleanup/small.txt", method="POST", data=b"confirm=yes")
test("Delete returns redirect", status in (200, 302), f"status={status}")
test("File actually deleted", not os.path.exists(os.path.join(HOME, "small.txt")), "")

# 9. Delete a directory
print("\n=== 9. Delete directory ===")
assert os.path.isdir(os.path.join(HOME, ".cache"))
status, body, headers = fetch("/cleanup/.cache", method="POST", data=b"confirm=yes")
test("Delete dir returns redirect", status in (200, 302), f"status={status}")
test("Directory actually deleted", not os.path.isdir(os.path.join(HOME, ".cache")), "")

# 10. Cannot delete home root
print("\n=== 10. Cannot delete home ===")
status, body, headers = fetch("/cleanup/", method="POST", data=b"confirm=yes")
test("Delete home returns 403", status == 403, f"status={status}")

# 11. 404 for nonexistent
print("\n=== 11. 404 ===")
status, body, headers = fetch("/cleanup/nonexistent.txt")
test("Nonexistent returns 404", status == 404, f"status={status}")

# 12. POST requires confirm
print("\n=== 12. POST requires confirm ===")
status, body, headers = fetch("/cleanup/notebook.ipynb", method="POST", data=b"")
test("POST without confirm returns 400", status == 400, f"status={status}")
test("File not deleted (no confirm)", os.path.exists(os.path.join(HOME, "notebook.ipynb")), "")

# ── Cleanup ─────────────────────────────────────────────────────────────

proc.terminate()
proc.wait()
shutil.rmtree(HOME, ignore_errors=True)

# ── Summary ─────────────────────────────────────────────────────────────

print(f"\n{'='*50}")
passed = sum(1 for _, c in results if c)
failed = sum(1 for _, c in results if not c)
print(f"Results: {passed} passed, {failed} failed")
if failed:
    print("\nFailed tests:")
    for name, cond in results:
        if not cond:
            print(f"  - {name}")
    if __name__ == "__main__":
        sys.exit(1)
    else:
        raise AssertionError(f"{failed} functional test(s) failed")
print("All tests passed!")
