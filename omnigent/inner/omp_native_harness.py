"""Native OMP harness application."""

from fastapi import FastAPI

from omnigent.inner.omp_native_executor import OmpNativeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter


def create_app() -> FastAPI:
    return ExecutorAdapter(executor_factory=OmpNativeExecutor).build()
