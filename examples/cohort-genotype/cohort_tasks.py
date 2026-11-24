"""The cohort tutorial's tasks: genotype one region in every 1000 Genomes high-coverage sample.

- ``plan``: list the samples (from the collection's sequence index), cut them into shards and
  write that manifest to your bucket, returning one small Globus Compute task per shard for the
  flow's next step. (A flow's whole state must stay under 200 KB, so sample lists travel in S3,
  not through the flow.)
- ``genotype``: one shard. ``bcftools mpileup | call`` per sample over the region, reading each
  CRAM through its index and the reference through its ``.fai``, then merge the shard into one
  BCF next to the manifest. Runs on the Lambda pool.
- ``merge``: all shards into one VCF with allele counts, plus allele frequencies by
  super-population for the variant sites. Runs on the Spot pool.

Each function imports what it needs inside, so Globus Compute can ship it as source text.
"""


def plan(region, out_prefix, genotype_function, per_shard=20, limit=0):
    """Write the shard manifest to ``out_prefix``/manifest.json; returns the shards as Compute
    action tasks: ``[{"function_id", "kwargs": {"manifest", "shard"}}, ...]``."""
    import csv
    import io
    import json
    import urllib.request

    import boto3

    index = (
        "https://1000genomes.s3.amazonaws.com/1000G_2504_high_coverage/"
        "1000G_2504_high_coverage.sequence.index"
    )
    text = urllib.request.urlopen(index, timeout=60).read().decode()
    rows = [ln for ln in text.splitlines() if ln and not ln.startswith("##")]
    samples = []
    for r in csv.DictReader(io.StringIO("\n".join(rows)), delimiter="\t"):
        run, name = r["RUN_ID"].strip(), r["SAMPLE_NAME"].strip()  # some names end in a space
        cram = f"s3://1000genomes/1000G_2504_high_coverage/data/{run}/{name}.final.cram"
        samples.append([name, cram, r["POPULATION"].strip()])
    samples.sort()
    if limit:
        samples = samples[:limit]
    shards = [samples[i : i + per_shard] for i in range(0, len(samples), per_shard)]
    out_prefix = out_prefix.rstrip("/") + "/"
    bucket, _, prefix = out_prefix.removeprefix("s3://").partition("/")
    body = {"region": region, "out_prefix": out_prefix, "shards": shards}
    boto3.client("s3").put_object(
        Bucket=bucket, Key=f"{prefix}manifest.json", Body=json.dumps(body).encode()
    )
    manifest = f"{out_prefix}manifest.json"
    return [
        {"function_id": genotype_function, "kwargs": {"manifest": manifest, "shard": n}}
        for n in range(len(shards))
    ]


def genotype(manifest, shard):
    """Genotype shard ``shard`` of ``manifest`` (its samples: ``[[name, cram_uri, population],
    ...]``) over the manifest's region; write the shard's BCF next to the manifest and return
    where, with timings and bytes read (kept small: the flow's state holds every shard's)."""
    import json
    import os
    import subprocess
    import tempfile
    import threading
    import time

    import boto3

    ref = (
        "s3://1000genomes/technical/reference/GRCh38_reference_genome/"
        "GRCh38_full_analysis_set_plus_decoy_hla.fa"
    )
    s3 = boto3.client("s3")

    def run(cmd, cwd):
        """Run one bcftools command; returns the bytes it read (rchar, sampled until it
        exits)."""
        p = subprocess.Popen(cmd, cwd=cwd, stderr=subprocess.PIPE)
        last = [0]

        def watch():
            while p.poll() is None:
                try:
                    with open(f"/proc/{p.pid}/io") as f:
                        for ln in f:
                            if ln.startswith("rchar:"):
                                last[0] = int(ln.split()[1])
                except OSError:
                    pass
                time.sleep(0.05)

        t = threading.Thread(target=watch)
        t.start()
        _, err = p.communicate()
        t.join()
        if p.returncode:
            raise RuntimeError(f"{cmd[:2]} failed: {err.decode()[-500:]}")
        return last[0]

    def one(name, cram, d):
        """Genotype one sample; returns (bcf path, bytes read)."""
        raw, called, out = (f"{d}/{name}.{x}.bcf" for x in ("raw", "called", "gt"))
        # Reads and bases of quality 20 or more; every site is called (reference calls too),
        # and genotypes with depth < 8 or genotype quality < 20 are set to missing.
        n = run(["bcftools", "mpileup", "-q", "20", "-Q", "20", "-a", "FORMAT/AD,FORMAT/DP",
                 "-f", ref, "-r", region, "-Ob", "-o", raw, cram], d)  # fmt: skip
        run(["bcftools", "call", "-m", "-a", "FORMAT/GQ", "-Ob", "-o", called, raw], d)
        run(["bcftools", "filter", "-S", ".", "-e", "FMT/DP<8 || FMT/GQ<20", "-Ob", "-o", out,
             called], d)  # fmt: skip
        run(["bcftools", "index", out], d)
        return out, n

    bucket, _, key = manifest.removeprefix("s3://").partition("/")
    m = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    samples, region, out_prefix = m["shards"][shard], m["region"], m["out_prefix"]
    where = "lambda" if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") else "ec2"
    t0 = time.time()
    read = 0
    done, failed = 0, []
    bcf = None
    with tempfile.TemporaryDirectory() as d:
        files = []
        for name, cram, _pop in samples:
            # Real data has gaps (two of the 2,504 CRAM indexes are empty files, which htslib
            # calls "not supported"): those fail at once; anything else (a read that timed out
            # with 100 tasks reading at once) is tried three times, then reported, not fatal.
            for attempt in range(3):
                try:
                    out, n = one(name, cram, d)
                    read += n
                    files.append(out)
                    done += 1
                    break
                except RuntimeError as e:
                    if attempt == 2 or "is not supported" in str(e):
                        failed.append(name)
                        print(f"{name}: {str(e)[-300:]}", flush=True)  # in the task's log
                        break
                    time.sleep(3 * (attempt + 1))
        if files:
            shard_bcf = f"{d}/shard-{shard:04d}.bcf"
            if len(files) == 1:
                os.replace(files[0], shard_bcf)
            else:
                run(["bcftools", "merge", "-Ob", "-o", shard_bcf, *files], d)
            run(["bcftools", "index", shard_bcf], d)
            ob, _, prefix = out_prefix.removeprefix("s3://").partition("/")
            k = f"{prefix}shards/shard-{shard:04d}.bcf"
            s3.upload_file(shard_bcf, ob, k)
            s3.upload_file(shard_bcf + ".csi", ob, k + ".csi")
            bcf = f"s3://{ob}/{k}"
    return {
        "shard": shard,
        "bcf": bcf,
        "samples": done,
        "failed": failed,
        "bytes_read": read,
        "seconds": round(time.time() - t0, 2),
        "where": where,
    }


def merge(*shards, manifest):
    """Merge the shards, add AC/AN/AF, keep variant sites, and summarise allele frequencies by
    super-population. Writes ``cohort.vcf.gz`` (+ ``.tbi``) and ``af_by_superpop.tsv`` next to
    the manifest."""
    import collections
    import json
    import os
    import subprocess
    import tempfile
    import time

    import boto3

    superpop = {
        **dict.fromkeys(["YRI", "LWK", "GWD", "MSL", "ESN", "ASW", "ACB"], "AFR"),
        **dict.fromkeys(["CEU", "TSI", "FIN", "GBR", "IBS"], "EUR"),
        **dict.fromkeys(["CHB", "JPT", "CHS", "CDX", "KHV"], "EAS"),
        **dict.fromkeys(["GIH", "PJL", "BEB", "STU", "ITU"], "SAS"),
        **dict.fromkeys(["MXL", "PUR", "CLM", "PEL"], "AMR"),
    }
    t0 = time.time()
    s3 = boto3.client("s3")
    mb, _, mk = manifest.removeprefix("s3://").partition("/")
    m = json.loads(s3.get_object(Bucket=mb, Key=mk)["Body"].read())
    region, out_prefix = m["region"], m["out_prefix"]
    failed = sorted({n for s in shards for n in s.get("failed", [])})
    pops = {
        name: superpop.get(pop, pop)
        for shard in m["shards"]
        for name, _cram, pop in shard
        if name not in failed
    }
    bucket, _, prefix = out_prefix.removeprefix("s3://").partition("/")
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for s in sorted(shards, key=lambda s: s["shard"]):
            if not s["bcf"]:
                continue
            b, _, k = s["bcf"].removeprefix("s3://").partition("/")
            local = f"{d}/{os.path.basename(k)}"
            s3.download_file(b, k, local)
            s3.download_file(b, k + ".csi", local + ".csi")
            paths.append(local)
        with open(f"{d}/inputs.txt", "w") as f:
            f.write("\n".join(paths) + "\n")
        groups = collections.defaultdict(list)
        for n, sp in pops.items():
            groups[sp].append(n)
        with open(f"{d}/groups.txt", "w") as f:
            f.writelines(f"{n}\t{sp}\n" for sp, ns in sorted(groups.items()) for n in ns)
        out = f"{d}/cohort.vcf.gz"
        threads = str(os.cpu_count() or 1)
        merged = subprocess.Popen(
            ["bcftools", "merge", "--threads", threads, "-l", f"{d}/inputs.txt", "-Ou"],
            stdout=subprocess.PIPE,
        )
        tags = subprocess.Popen(
            ["bcftools", "+fill-tags", "-Ou", "--", "-t", "AC,AN,AF", "-S", f"{d}/groups.txt"],
            stdin=merged.stdout, stdout=subprocess.PIPE,
        )  # fmt: skip
        # Variant sites called in at least 90% of samples.
        subprocess.run(
            ["bcftools", "view", "-c", "1", "-i", "F_MISSING<0.1", "-Oz", "-o", out],
            stdin=tags.stdout, check=True,
        )  # fmt: skip
        if merged.wait() or tags.wait():
            raise RuntimeError("bcftools merge or +fill-tags failed")
        subprocess.run(["bcftools", "index", "-t", out], check=True)
        cols = ["AF"] + [f"AF_{sp}" for sp in sorted(groups)]
        fmt = "%CHROM\t%POS\t%REF\t%ALT\t" + "\t".join(f"%{c}" for c in cols) + "\n"
        table = subprocess.run(
            ["bcftools", "query", "-f", fmt, out], check=True, capture_output=True, text=True
        ).stdout
        tsv = f"{d}/af_by_superpop.tsv"
        with open(tsv, "w") as f:
            f.write("\t".join(["chrom", "pos", "ref", "alt", *cols]) + "\n" + table)
        for local, name in ((out, "cohort.vcf.gz"), (out + ".tbi", "cohort.vcf.gz.tbi"),
                            (tsv, "af_by_superpop.tsv")):  # fmt: skip
            s3.upload_file(local, bucket, f"{prefix}{name}")
        sites = table.count("\n")
    return {
        "region": region,
        "samples": len(pops),
        "failed_samples": failed,
        "superpops": {sp: len(ns) for sp, ns in sorted(groups.items())},
        "variant_sites": sites,
        "vcf": f"{out_prefix}cohort.vcf.gz",
        "table": f"{out_prefix}af_by_superpop.tsv",
        "genotype_bytes_read": sum(s["bytes_read"] for s in shards),
        "genotype_task_seconds": round(sum(s["seconds"] for s in shards), 1),
        "merge_seconds": round(time.time() - t0, 1),
    }
