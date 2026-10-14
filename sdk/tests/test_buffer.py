"""SG-A6.4 (A6-5): the disk buffer lives in a directory only its user can read."""
import logging
import os
import stat
import tempfile

import pytest

from agentops_sdk.utils import append_to_disk, disk_buffer_path, drain_disk

pytestmark = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX permissions")


@pytest.fixture
def tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_buffer_dir_and_file_are_private(tmp):
    old = os.umask(0)
    try:
        path = disk_buffer_path("key12345678")
        append_to_disk(path, {"input": "secret"})
        append_to_disk(path, {"input": "more"})
    finally:
        os.umask(old)
    assert path == tmp / f"agentops_sdk-{os.getuid()}" / "buffer-key12345.jsonl"
    assert (_mode(path.parent), _mode(path)) == (0o700, 0o600)
    assert drain_disk(path) == [{"input": "secret"}, {"input": "more"}]


def test_own_dir_with_loose_mode_is_tightened(tmp):
    d = tmp / f"agentops_sdk-{os.getuid()}"
    d.mkdir(mode=0o777)
    os.chmod(d, 0o777)
    assert disk_buffer_path("k").parent == d
    assert _mode(d) == 0o700


@pytest.mark.parametrize("planted", ["other_user", "symlink", "file"])
def test_dir_not_ours_is_refused(tmp, monkeypatch, caplog, planted):
    uid = os.getuid()
    d = tmp / f"agentops_sdk-{uid}"
    if planted == "other_user":
        d.mkdir(mode=0o700)
        (d / "buffer-k.jsonl").write_text('{"trace_id": "planted"}\n')
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
        d = d.rename(tmp / f"agentops_sdk-{uid + 1}")  # owned by uid, which is now "another user"
    elif planted == "symlink":
        (tmp / "elsewhere").mkdir(mode=0o700)
        d.symlink_to(tmp / "elsewhere")
    else:
        d.write_text("")
    with caplog.at_level(logging.ERROR, logger="agentops_sdk"):
        path = disk_buffer_path("k")
    assert path.parent != d and path.parent.parent == tmp and _mode(path.parent) == 0o700
    assert drain_disk(path) == []
    assert "not this user's" in caplog.text


def test_buffer_file_symlink_is_not_followed(tmp):
    path = disk_buffer_path("k")
    target = tmp / "target"
    target.write_text("")
    path.symlink_to(target)
    with pytest.raises(OSError):
        append_to_disk(path, {"x": 1})
    assert target.read_text() == ""
