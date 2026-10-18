"""Wait until FalkorDB answers PING (used by `make up` / `make ping`)."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.config import FALKORDB_HOST, FALKORDB_PORT  # noqa: E402


def main() -> int:
    import redis

    deadline = time.time() + 60
    last = None
    while time.time() < deadline:
        try:
            pong = redis.Redis(host=FALKORDB_HOST, port=FALKORDB_PORT, socket_timeout=2).ping()
            modules = redis.Redis(host=FALKORDB_HOST, port=FALKORDB_PORT).execute_command("MODULE", "LIST")
            names = [
                f"{m[b'name'].decode()}@{m[b'ver']}" if isinstance(m, dict) else str(m[1]) for m in modules
            ]
            print(f"PING {FALKORDB_HOST}:{FALKORDB_PORT} -> {'PONG' if pong else pong}; modules={names}")
            return 0
        except Exception as exc:  # retry until deadline
            last = exc
            time.sleep(1)
    print(f"FalkorDB not reachable at {FALKORDB_HOST}:{FALKORDB_PORT}: {last}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
