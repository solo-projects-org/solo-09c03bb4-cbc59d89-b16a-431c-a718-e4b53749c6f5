import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="settle-test-")
os.environ["SETTLE_DB"] = os.path.join(_tmp, "test.db")
os.environ["SETTLE_PACKAGES"] = os.path.join(_tmp, "packages")
os.environ["SETTLE_SECRET"] = "test"
