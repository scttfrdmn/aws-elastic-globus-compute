"""The task for the Sentinel-2 NDVI tutorial (docs/tutorials/sentinel2-ndvi.md).

`ndvi_latest` runs on an aegc worker built from the aarchsci earth-observation image. It's
registered as source text (PureSourceTextInspect), so it carries only its own text: everything it
needs is imported inside it, and its arguments and return value are JSON, as Globus Flows needs.
"""


def ndvi_latest(bbox, days=30, max_cloud=20):
    """NDVI of the most recent low-cloud Sentinel-2 L2A scene over `bbox` in the last `days`.

    `bbox` is [west, south, east, north] in degrees. Reads only the window over the bbox from the
    red, NIR and scene-classification (SCL) COGs, masks clouds, shadows and nodata with SCL, writes
    a float32 NDVI GeoTIFF with `aegc.outputs.put`, and returns a small JSON summary.
    """
    import os
    import time
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    # Anonymous reads of the open bucket, and no directory listing per COG.
    os.environ["AWS_NO_SIGN_REQUEST"] = "YES"
    os.environ["GDAL_DISABLE_READDIR_ON_OPEN"] = "EMPTY_DIR"
    os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")

    import numpy as np
    import pystac_client
    import rasterio.warp
    import rioxarray
    from rasterio.enums import Resampling

    from aegc import outputs

    def bytes_read():
        # Bytes this process has read (files and sockets): a close estimate of what came over
        # the network for the COG reads, once the imports are done. Linux only.
        try:
            for line in Path("/proc/self/io").read_text().splitlines():
                if line.startswith("rchar:"):
                    return int(line.split()[1])
        except OSError:
            pass
        return None

    t0 = time.monotonic()
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    search = pystac_client.Client.open("https://earth-search.aws.element84.com/v1").search(
        collections=["sentinel-2-c1-l2a"],
        bbox=bbox,
        datetime=f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
        query={"eo:cloud_cover": {"lt": max_cloud}},
        sortby="-properties.datetime",
        max_items=1,
    )
    items = list(search.items())
    if not items:
        return {"found": False, "bbox": bbox, "days": days, "max_cloud": max_cloud}
    item = items[0]

    def red_crs(item):
        code = item.properties.get("proj:code") or item.properties.get("proj:epsg")
        return code if str(code).startswith("EPSG:") else f"EPSG:{code}"

    def band(key, match=None):
        asset = item.assets[key]
        da = rioxarray.open_rasterio(asset.href, masked=True).squeeze("band", drop=True)
        da = da.rio.clip_box(*bbox, crs="EPSG:4326")
        if match is not None:
            da = da.rio.reproject_match(match, resampling=Resampling.nearest)
        info = (asset.extra_fields.get("raster:bands") or [{}])[0]
        scale, offset = info.get("scale", 1.0), info.get("offset", 0.0)
        return da, scale, offset, asset.extra_fields.get("file:size")

    # Look up the scene's CRS once, so PROJ's database reads don't count as bytes read.
    rasterio.warp.transform_bounds("EPSG:4326", red_crs(item), *bbox)
    before = bytes_read()
    red, red_scale, red_offset, red_size = band("red")
    nir, nir_scale, nir_offset, nir_size = band("nir")
    scl, _, _, scl_size = band("scl", match=red)  # 20 m, resampled onto the 10 m grid
    red_r = red * red_scale + red_offset
    nir_r = nir * nir_scale + nir_offset
    # SCL classes kept: 4 vegetation, 5 bare soil, 6 water, 7 unclassified, 11 snow.
    clear = scl.isin([4, 5, 6, 7, 11])
    ndvi = ((nir_r - red_r) / (nir_r + red_r)).where(clear).astype("float32")
    ndvi = ndvi.rio.write_nodata(np.nan, encoded=False)
    values = ndvi.values
    after = bytes_read()

    work = Path("/scratch") / f"ndvi-{outputs.task_uuid() or os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    path = work / f"{item.id}_ndvi.tif"
    ndvi.rio.to_raster(path, driver="COG", compress="DEFLATE")
    uri = outputs.put(path)
    out_bytes = path.stat().st_size
    path.unlink()
    work.rmdir()

    valid = np.isfinite(values)
    sizes = [s for s in (red_size, nir_size, scl_size) if s]
    return {
        "found": True,
        "item": item.id,
        "datetime": item.properties.get("datetime"),
        "cloud_cover": item.properties.get("eo:cloud_cover"),
        "uri": uri,
        "shape": list(values.shape),
        "valid_fraction": round(float(valid.mean()), 4),
        "ndvi_mean": round(float(np.nanmean(values)), 4) if valid.any() else None,
        "bytes_read": (after - before) if before is not None and after is not None else None,
        "asset_bytes": sum(sizes) if len(sizes) == 3 else None,
        "output_bytes": out_bytes,
        "seconds": round(time.monotonic() - t0, 2),
    }
