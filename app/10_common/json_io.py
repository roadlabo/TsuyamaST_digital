import json
import os
import time
from pathlib import Path
from typing import Any, Tuple


def write_json_safe(
    path: str | Path,
    data: dict[str, Any],
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    retries: int = 10,
    base_delay: float = 0.2,
) -> Tuple[bool, int, Exception | None]:
    path_obj = Path(path)
    os.makedirs(path_obj.parent, exist_ok=True)

    tmp_path = Path(str(path_obj) + ".tmp")
    try:
        if tmp_path.exists():
            tmp_path.unlink()
    except Exception:
        pass

    last_err: Exception | None = None
    attempts = max(1, int(retries))

    for attempt in range(attempts):
        try:
            with open(path_obj, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=ensure_ascii, indent=indent)
                handle.flush()
                os.fsync(handle.fileno())
            return True, attempt, None
        except (PermissionError, OSError) as exc:
            last_err = exc
            delay = min(1.0, base_delay * (1.5**attempt))
            time.sleep(delay)
        except Exception as exc:
            last_err = exc
            break

    try:
        if tmp_path.exists():
            tmp_path.unlink()
    except Exception:
        pass

    return False, attempts, last_err
