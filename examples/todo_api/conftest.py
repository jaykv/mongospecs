import os
import sys
import uuid

# The tests import the application as a module, and point it at a database of their own.
sys.path.insert(0, os.path.dirname(__file__))
_base = os.environ.get("MONGOSPECS_TEST_URI", "mongodb://localhost:27117/?replicaSet=rs0")
_host, _, _query = _base.partition("?")
os.environ.setdefault("MONGO_URI", f"{_host.rstrip('/')}/todo_test_{uuid.uuid4().hex[:8]}?{_query}")
