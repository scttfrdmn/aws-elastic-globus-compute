"""The task for the Spot sweep tutorial (docs/tutorials/spot-sweep.md).

`collatz_chunk` is CPU-bound, pure Python and deterministic: the same arguments give the same
answer on any machine, so a sweep that survives interruptions can be checked against one that
didn't have any.
"""


def collatz_chunk(index, size):
    """The longest Collatz chain among the start values index*size+1 .. (index+1)*size.

    Returns the chunk's bounds, the start value with the most steps to reach 1, that step count,
    and the total steps over the chunk (a checksum of the whole chunk).
    """
    import time

    t0 = time.monotonic()
    lo = index * size + 1
    best_n, best_steps, total = lo, 0, 0
    for n in range(lo, lo + size):
        steps = 0
        x = n
        while x != 1:
            x = x >> 1 if x & 1 == 0 else 3 * x + 1
            steps += 1
        total += steps
        if steps > best_steps:
            best_n, best_steps = n, steps
    return {
        "index": index,
        "lo": lo,
        "hi": lo + size - 1,
        "best_n": best_n,
        "best_steps": best_steps,
        "total_steps": total,
        "seconds": round(time.monotonic() - t0, 2),  # not part of the answer
    }
