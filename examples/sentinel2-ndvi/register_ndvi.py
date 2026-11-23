"""Part 2 of the Sentinel-2 NDVI tutorial: register `ndvi_latest` for Globus Flows.

    python examples/sentinel2-ndvi/register_ndvi.py

Run it as the endpoint owner. It registers the function as source text, the form the Compute
action provider can run with JSON arguments, and prints its function id.
"""

import sys
from pathlib import Path

from globus_compute_sdk import Client
from globus_compute_sdk.serialize import PureSourceTextInspect

sys.path.insert(0, str(Path(__file__).parent))
from ndvi_tasks import ndvi_latest

if __name__ == "__main__":
    gcc = Client(code_serialization_strategy=PureSourceTextInspect())
    print(gcc.register_function(ndvi_latest))
