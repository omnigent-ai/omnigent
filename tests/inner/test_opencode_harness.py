from fastapi import FastAPI

from omnigent.inner.opencode_executor import OpenCodeExecutor
from omnigent.inner.opencode_harness import _build_opencode_executor, create_app


def test_build_executor():
    assert isinstance(_build_opencode_executor(), OpenCodeExecutor)


def test_create_app():
    assert isinstance(create_app(), FastAPI)
