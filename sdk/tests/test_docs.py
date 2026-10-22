"""The README quick starts and sdk/demo.py work as written (SG-P2)."""
import ast
import asyncio
import importlib.util
import json
import os
import random
import re
import sys
from pathlib import Path

import pytest

from agentops_sdk import client as client_mod
from agentops_sdk.utils import disk_buffer_path

SDK = Path(__file__).resolve().parents[1]
ROOT_README, SDK_README = (SDK.parent / "README.md").read_text(), (SDK / "README.md").read_text()
FLAGS = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT


def _blocks(text):
    return re.findall(r"```python\n(.*?)```", text, re.DOTALL)


@pytest.fixture
def isolated(server, monkeypatch, tmp_path):
    monkeypatch.setattr(client_mod, "_singleton", None)
    monkeypatch.setattr(client_mod, "disk_buffer_path", lambda key: tmp_path / "buffer.jsonl")
    yield server
    client_mod.close()


def _sent_traces(server):
    return [t for body in server.bodies("bulk") for t in body["traces"]]


@pytest.mark.parametrize("block", _blocks(ROOT_README) + _blocks(SDK_README))
def test_readme_python_blocks_compile_and_import(block):
    tree = compile(block, "<readme>", "exec", flags=FLAGS | ast.PyCF_ONLY_AST)
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    for name in names:
        assert importlib.util.find_spec(name.split(".")[0]) is not None, name


class _Agent:
    def run(self, msg):
        return f"answer to {msg}"

    async def arun(self, msg):
        return f"async answer to {msg}"


def test_root_readme_quick_start_sends_a_trace(isolated):
    sync_block, async_block = [b for b in _blocks(ROOT_README) if "agentops.trace" in b][:2]
    g = {"user_msg": "hello", "q": "hi", "my_agent": _Agent()}
    exec(compile(sync_block, "<readme>", "exec"), g)
    coro = eval(compile(async_block, "<readme>", "exec", flags=FLAGS), g)
    asyncio.run(coro)
    g["agentops"].flush()
    sent = _sent_traces(isolated)
    assert [(t["input"], t["output"], t["status"]) for t in sent] == [
        ("hello", "answer to hello", "success"), ("hi", "async answer to hi", "success")]
    assert sent[0]["tool_calls"][0]["name"] == "web_search" and sent[0]["cost_usd"] == 0.0021


def test_sdk_readme_quick_start_sends_a_trace(isolated):
    block = _blocks(SDK_README)[0]
    g = {"user_input": "hello", "my_agent": _Agent()}
    exec(compile(block, "<readme>", "exec"), g)
    g["agentops"].flush()
    assert [t["output"] for t in _sent_traces(isolated)] == ["answer to hello"]


def test_readmes_name_the_buffer_path_the_sdk_uses():
    p = disk_buffer_path("aop_abcdefgh123")
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    assert (p.parent.name, p.name) == (f"agentops_sdk-{uid}", "buffer-aop_abcd.jsonl")
    assert "`<tempdir>/agentops_sdk-<uid>/buffer-<key prefix>.jsonl`" in ROOT_README
    assert "`<tempdir>/agentops_sdk-<uid>/`" in SDK_README
    assert "/tmp/agentops_sdk/" not in ROOT_README + SDK_README


def test_demo_runs(isolated, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(SDK))
    sys.modules.pop("demo", None)
    import demo

    monkeypatch.setattr(random, "uniform", lambda a, b: 0)
    demo.main()
    sent = _sent_traces(isolated)
    assert len(sent) == 15 and {t["status"] for t in sent} <= {"success", "error"}
    assert "demo: done" in capsys.readouterr().out
    assert json.dumps(sent[0])  # plain JSON
