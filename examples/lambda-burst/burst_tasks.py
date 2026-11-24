"""The task: a few seconds of CPU work (hashing), standing in for a short analysis step."""


def crunch(index: int, seconds: float) -> dict:
    import hashlib
    import os
    import time

    t0 = time.monotonic()
    h = hashlib.sha256(str(index).encode())
    rounds = 0
    while time.monotonic() - t0 < seconds:
        for _ in range(1000):
            h = hashlib.sha256(h.digest())
        rounds += 1000
    where = "lambda" if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") else "ec2"
    return {"index": index, "rounds": rounds, "digest": h.hexdigest()[:16], "where": where}
