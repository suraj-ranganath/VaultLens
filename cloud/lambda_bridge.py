from __future__ import annotations

import json
import sys

from cloud.telegram_webhook_lambda import handler


def main() -> int:
    payload = json.load(sys.stdin)
    event = payload.get("event") or {}
    result = handler(event, None)
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
