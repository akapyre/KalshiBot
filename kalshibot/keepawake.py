"""Stop the computer from going to sleep on its own while the bot runs.
A sleeping computer pauses the bot, so games go unwatched.

- Windows: SetThreadExecutionState, the same call video players use. It
  lasts while the bot runs and ends with it.
- macOS: a `caffeinate -i` tied to the bot's process (start.sh does the
  same; this covers starting the bot directly).

This only stops idle sleep. Closing a laptop lid, or choosing Sleep, still
sleeps it -- see README "Keeping the computer awake".
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger("kalshibot.keepawake")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake() -> bool:
    try:
        if sys.platform == "win32":
            import ctypes
            ok = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            if ok:
                logger.info("Keeping Windows awake while the bot runs (closing the lid still sleeps it)")
            return bool(ok)
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            logger.info("Keeping the Mac awake while the bot runs (closing the lid still sleeps it)")
            return True
    except Exception as e:
        logger.warning("Couldn't stop the computer from sleeping: %s", e)
    return False
