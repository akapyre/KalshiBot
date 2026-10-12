import ctypes
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot import keepawake


def test_windows_asks_to_stay_awake_while_running(monkeypatch):
    calls = []
    kernel32 = SimpleNamespace(SetThreadExecutionState=lambda flags: calls.append(flags) or 1)
    monkeypatch.setattr(keepawake.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    assert keepawake.keep_awake() is True
    assert calls == [keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED]


def test_mac_ties_caffeinate_to_the_bot(monkeypatch):
    started = []
    monkeypatch.setattr(keepawake.sys, "platform", "darwin")
    monkeypatch.setattr(keepawake.shutil, "which", lambda name: "/usr/bin/caffeinate")
    monkeypatch.setattr(keepawake.subprocess, "Popen", lambda args: started.append(args))
    assert keepawake.keep_awake() is True
    assert started[0][:3] == ["caffeinate", "-i", "-w"]


def test_other_platforms_do_nothing(monkeypatch):
    monkeypatch.setattr(keepawake.sys, "platform", "linux")
    assert keepawake.keep_awake() is False
