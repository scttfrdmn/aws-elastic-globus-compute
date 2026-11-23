"""The task for the 1000 Genomes tutorial (docs/tutorials/genomics-slice.md).

`slice_vcf` runs on an aegc worker in us-east-1, next to the 1000 Genomes bucket. It reads one
region of a remote, indexed VCF over HTTPS with bcftools (only the index and the compressed blocks
that cover the region), fills in allele frequencies, and writes the slice and an allele-frequency
table with `aegc.outputs.put`. Everything it needs is imported inside it, and it takes and
returns JSON.
"""

CHR22 = (
    "https://1000genomes.s3.amazonaws.com/1000G_2504_high_coverage/working/"
    "20201028_3202_raw_GT_with_annot/"
    "20201028_CCDG_14151_B01_GRM_WGS_2020-08-05_chr22.recalibrated_variants.vcf.gz"
)


def slice_vcf(region, url):
    """Slice `region` (e.g. "chr22:20000000-21000000") out of the bgzipped, indexed VCF at `url`."""
    import gzip
    import shutil
    import subprocess
    import tempfile
    import time
    import urllib.request
    from pathlib import Path

    from aegc import outputs

    head = urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30)
    source_bytes = int(head.headers["Content-Length"])

    # bcftools downloads the remote index into its working directory, so give it one in /scratch.
    work = Path(tempfile.mkdtemp(dir="/scratch", prefix="slice-"))
    stem = region.replace(":", "_").replace("-", "_")
    vcf = work / f"{stem}.vcf.gz"
    t0 = time.monotonic()
    view = subprocess.Popen(
        ["bcftools", "view", "-r", region, "-Ou", url], cwd=work, stdout=subprocess.PIPE
    )
    subprocess.run(
        ["bcftools", "+fill-tags", "-Oz", "-o", vcf.name, "-", "--", "-t", "AF,AC,AN"],
        cwd=work,
        stdin=view.stdout,
        check=True,
    )
    view.stdout.close()
    if view.wait() != 0:
        raise RuntimeError(f"bcftools view -r {region} failed (exit {view.returncode})")
    subprocess.run(["bcftools", "index", "-t", vcf.name], cwd=work, check=True)
    table = subprocess.run(
        ["bcftools", "query", "-f", "%CHROM\t%POS\t%REF\t%ALT\t%FILTER\t%AF\n", vcf.name],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    slice_s = time.monotonic() - t0

    # Allele-frequency bins over every alternate allele.
    bins = {"rare (<1%)": 0, "low (1-5%)": 0, "common (>=5%)": 0}
    records = passing = 0
    for line in table.splitlines():
        _chrom, _pos, _ref, _alt, filt, afs = line.split("\t")
        records += 1
        passing += filt == "PASS"
        for af in afs.split(","):
            if af == ".":
                continue
            f = float(af)
            bins["rare (<1%)" if f < 0.01 else "low (1-5%)" if f < 0.05 else "common (>=5%)"] += 1
    af_table = work / f"{stem}.af.tsv.gz"
    af_table.write_bytes(gzip.compress(("CHROM\tPOS\tREF\tALT\tFILTER\tAF\n" + table).encode()))

    slice_uri = outputs.put(vcf)
    index_uri = outputs.put(work / f"{vcf.name}.tbi")
    af_uri = outputs.put(af_table)
    result = {
        "region": region,
        "records": records,
        "pass": passing,
        "af_bins": bins,
        "slice_uri": slice_uri,
        "index_uri": index_uri,
        "af_uri": af_uri,
        "slice_bytes": vcf.stat().st_size,
        "af_bytes": af_table.stat().st_size,
        "source_bytes": source_bytes,
        "slice_seconds": round(slice_s, 1),
    }
    shutil.rmtree(work)
    return result
