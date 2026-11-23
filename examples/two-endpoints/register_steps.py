"""Register the two-endpoints tutorial's steps for Globus Flows, as source text.

    python examples/two-endpoints/register_steps.py

Run it as the owner of both endpoints. Prints the two function ids as JSON.
"""

import json
import sys
from pathlib import Path

from globus_compute_sdk import Client
from globus_compute_sdk.serialize import PureSourceTextInspect

sys.path.insert(0, str(Path(__file__).parent))
from steps import make_manifest, summarize

if __name__ == "__main__":
    gcc = Client(code_serialization_strategy=PureSourceTextInspect())
    ids = {
        "manifest_function": gcc.register_function(make_manifest),
        "summarize_function": gcc.register_function(summarize),
    }
    print(json.dumps(ids, indent=2))
