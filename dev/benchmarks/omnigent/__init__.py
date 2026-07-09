"""Omnigent user-journey performance benchmark.

Stands up a real server + runner against a zero-latency mock LLM, drives
key user journeys under load, and emits a versioned JSON report of latency
percentiles and throughput. See ``README.md`` for the workflow and how the
workspace ETL notebook consumes the JSON.
"""
