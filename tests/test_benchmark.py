import shutil

from pdlwatch import benchmark


def test_benchmark_times_each_worker_count_and_a_cached_run(built, tmp_path):
    pdfs = sorted((built["root"] / "corpus" / "pdfs").glob("*.pdf"))[:2]
    for p in pdfs:
        shutil.copy(p, tmp_path / p.name)
    res = benchmark.run(benchmark.jobs_for(tmp_path), workers=(1,))
    assert res["documents"] == 2 and res["pages"] > 0
    (one,) = res["passes"]
    assert one["workers"] == 1 and one["pages_per_minute"] > 0
    assert 0 <= res["cached_seconds"] < one["seconds"]  # nothing is read again
    assert "2 PDFs" in benchmark.report(res)
