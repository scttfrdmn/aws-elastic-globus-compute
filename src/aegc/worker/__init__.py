"""aegc-worker: claims tasks from SQS and runs them with Globus Compute's execute_task.

Runs inside the worker image (a science environment plus globus-compute-endpoint), one agent per
instance. See :mod:`aegc.worker.agent`.
"""
