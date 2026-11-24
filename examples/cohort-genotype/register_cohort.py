"""Register the cohort tutorial's three functions for Globus Flows, as source text.

    python examples/cohort-genotype/register_cohort.py

Prints the function ids as JSON, for the flow's input.
"""

import json
import sys
from pathlib import Path

from globus_compute_sdk import Client
from globus_compute_sdk.serialize import PureSourceTextInspect

sys.path.insert(0, str(Path(__file__).parent))
from cohort_tasks import genotype, merge, plan

if __name__ == "__main__":
    gcc = Client(code_serialization_strategy=PureSourceTextInspect())
    ids = {
        "plan_function": gcc.register_function(plan),
        "genotype_function": gcc.register_function(genotype),
        "merge_function": gcc.register_function(merge),
    }
    print(json.dumps(ids, indent=2))
