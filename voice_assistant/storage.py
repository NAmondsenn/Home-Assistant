"""
Storage Module.

Saves and reads the small JSON files Nova keeps in its data folder - timers,
alarms, volumes and memory - the same way for each, so a fix to one reaches all of
them.

A save is written to a temporary file beside the real one, flushed to the disk,
and renamed over it. The rename is atomic on Linux, so losing power mid-write
leaves the previous file intact rather than a half-written one - a timer set a
moment before a power cut is still there afterwards.
"""

import json
import os
import tempfile
from typing import Any


def save_json(path: str, data: Any, indent: int = None):
    """
    Writes data to a JSON file atomically, readable by this user only.

    Args:
        path: The file to write.
        data: Anything json.dump accepts.
        indent: Indentation for a file meant to be read by hand, e.g. memory.json.

    Raises:
        OSError or TypeError if it couldn't be written - the previous file, if any,
        is left as it was.
    """
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    # In the same folder as the target, since a rename is only atomic within one
    # filesystem. mkstemp makes it readable by this user only.
    name = os.path.splitext(os.path.basename(path))[0]
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=f".{name}-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as file:
            json.dump(data, file, indent=indent)
            # Flushed to the disk itself, not just the OS cache.
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        # Never leaves the temporary file behind if the write failed.
        if os.path.exists(temporary):
            os.remove(temporary)
        raise

    # The rename itself lives in the folder, so the folder is flushed too - on ext4
    # a power cut just after the rename could otherwise bring back the old file.
    try:
        descriptor = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some filesystems (and Windows) don't allow a folder to be flushed.
        pass
    finally:
        os.close(descriptor)


def load_json(path: str, default: Any = None) -> Any:
    """
    Reads a JSON file.

    Args:
        path: The file to read.
        default: What to return if there's no file yet.

    Returns:
        What was in it, or default if it doesn't exist.

    Raises:
        OSError or ValueError if it exists but can't be read - each caller decides
        what a broken file means for it.
    """
    try:
        with open(path) as file:
            return json.load(file)
    except FileNotFoundError:
        return default
