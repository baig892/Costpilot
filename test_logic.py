"""Quick checks for the pure logic (no AWS needed). Run: python test_logic.py"""
import datetime as dt
import scanners as sc

T = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
pts = lambda vals: [(T + dt.timedelta(hours=i), v) for i, v in enumerate(vals)]


def test_percentile():
    assert sc.percentile([], 95) is None
    assert sc.percentile([5], 95) == 5
    assert sc.percentile(list(range(1, 101)), 50) == 50.5
    assert round(sc.percentile(list(range(1, 101)), 95), 2) == 95.05


def test_one_size_down():
    assert sc.one_size_down("m5.2xlarge") == "m5.xlarge"
    assert sc.one_size_down("t3.large") == "t3.medium"
    assert sc.one_size_down("t3.nano") is None
    assert sc.one_size_down("weird") is None


def test_summarize_needs_data():
    assert sc.summarize(pts([1] * 10), pts([2] * 10), pts([0] * 10), pts([0] * 10)) is None


def mk(cpu, peak, net_bytes_per_hour, hours=24 * 60):
    return sc.summarize(pts([cpu] * hours), pts([peak] * hours),
                        pts([net_bytes_per_hour] * hours), pts([0] * hours))


def test_idle():
    s = mk(1, 5, 1e6)  # ~0.02 GB/day
    assert sc.classify(s) == "idle", s


def test_low_cpu_but_busy_network_is_not_idle():
    s = mk(2, 10, 5e8)  # 12 GB/day
    assert sc.classify(s) == "oversized", s  # low CPU, so it is oversized, not idle


def test_oversized():
    assert sc.classify(mk(15, 40, 1e8)) == "oversized"


def test_ok_and_hot():
    assert sc.classify(mk(50, 90, 1e8)) == "ok"
    assert sc.classify(mk(85, 99, 1e8)) == "hot"


def test_spiky_workload_not_oversized():
    # low average but a regular 70% peak must NOT be called oversized
    vals = [5] * 23 + [70]
    s = sc.summarize(pts(vals * 30), pts([max(v, 70) if v == 70 else v for v in vals] * 30),
                     pts([1e8] * 720), pts([0] * 720))
    assert sc.classify(s) in ("ok", "hot"), s


def test_not_enough_data():
    assert sc.classify(mk(1, 2, 0, hours=24 * 5)) == "not_enough_data"


def test_confidence():
    assert sc.confidence(90) == "high" and sc.confidence(40) == "medium" and sc.confidence(15) == "low"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"\nall {len(tests)} checks passed")
    