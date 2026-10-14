import json
import logging
import os
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import Iterator

MAX_REQUEST_EVENTS = 10_000  # the server's cap on events per bulk request (TraceBulkIn)


def now_ms() -> int:
    return int(time.time() * 1000)


def gen_uid() -> str:
    return str(uuid.uuid4())


def _private_dir(base: Path) -> bool:
    """Creates `base` 0700, or accepts it if it is already a directory only this user can enter."""
    try:
        base.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = os.lstat(base)
    if not stat.S_ISDIR(st.st_mode) or not hasattr(os, "getuid"):
        return stat.S_ISDIR(st.st_mode)
    if st.st_uid != os.getuid():
        return False
    if st.st_mode & 0o077:
        os.chmod(base, 0o700)
    return True


def disk_buffer_path(api_key_prefix: str) -> Path:
    """The buffer of undelivered traces (raw input and output) in a per-user directory only this user can read;
    one another user created is refused for a fresh private one, whose traces the next run will not find."""
    base = Path(tempfile.gettempdir()) / f"agentops_sdk-{os.getuid() if hasattr(os, 'getuid') else 'user'}"
    if not _private_dir(base):
        logging.getLogger("agentops_sdk").error(
            "agentops buffer directory %s is not this user's; buffering to a new private directory", base)
        base = Path(tempfile.mkdtemp(prefix="agentops_sdk-", dir=base.parent))
    return base / f"buffer-{api_key_prefix[:8]}.jsonl"


def append_to_disk(path: Path, payload: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with open(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str) + "\n")


def buffer_rejected(path: Path, items: list[dict], response) -> None:
    """Re-buffers the traces a 202 bulk response rejected with `storage_error` and logs the other rejections."""
    try:
        body = response.json()
    except ValueError:
        return
    rejected = body.get("rejected") if isinstance(body, dict) else None
    for r in rejected if isinstance(rejected, list) else []:
        i = r.get("index") if isinstance(r, dict) else None
        if not isinstance(i, int) or not 0 <= i < len(items):
            continue
        if r.get("reason") == "storage_error":
            append_to_disk(path, items[i])
        else:
            logging.getLogger("agentops_sdk").warning(
                "agentops dropped trace %s: %s", items[i].get("trace_id"), r.get("reason"))


def _event_count(payload: dict) -> int:
    return sum(len(payload[k]) for k in ("events", "tool_calls") if isinstance(payload.get(k), list))


def batches(items: list[dict], size: int, max_events: int = MAX_REQUEST_EVENTS) -> Iterator[list[dict]]:
    """Splits traces into bulk requests of at most `size` traces and `max_events` events. A trace over the event
    cap alone is logged and dropped: no retry can deliver it."""
    out: list[dict] = []
    n = 0
    for it in items:
        k = _event_count(it)
        if k > max_events:
            logging.getLogger("agentops_sdk").warning(
                "agentops dropped trace %s: %d events, the server takes at most %d", it.get("trace_id"), k, max_events)
            continue
        if out and (len(out) >= size or n + k > max_events):
            yield out
            out, n = [], 0
        out.append(it)
        n += k
    if out:
        yield out


def drain_disk(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                continue
    try:
        path.unlink()
    except Exception:
        pass
    return items
