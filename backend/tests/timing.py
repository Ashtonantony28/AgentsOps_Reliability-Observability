import statistics
import time


def median_ms(f, n: int = 5) -> float:
    """Median wall time of `n` calls: one sample on a busy CI runner is noise, not a budget breach."""
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        f()
        samples.append((time.perf_counter() - t) * 1000)
    return statistics.median(samples)
