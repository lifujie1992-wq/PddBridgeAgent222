"""Append one queue record while the caller holds its queue lock."""
import json


def append_event(path, event):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    with path.open("ab", buffering=0) as handle:
        position = handle.tell()
        try:
            if handle.write(data) != len(data):
                raise OSError("short queue append")
        except OSError:
            # Do not concatenate the next retry onto a partially written JSON line.
            handle.truncate(position)
            raise
