#!/bin/sh
# With arguments: run them (e.g. `globus-compute-endpoint login`). Without: start the endpoint.
set -e
if [ "$#" -gt 0 ]; then
    exec "$@"
fi
: "${GC_ENDPOINT:?set GC_ENDPOINT}"
# A container stopped without `globus-compute-endpoint stop` leaves a stale pid file (spike #3).
rm -f "$HOME/.globus_compute/$GC_ENDPOINT/daemon.pid"
# The cloud endpoint (#22) keeps its id across instance replacements and logs to the console,
# which the host's awslogs driver ships to CloudWatch.
if [ -n "${GC_ENDPOINT_UUID:-}" ]; then
    exec globus-compute-endpoint start "$GC_ENDPOINT" --endpoint-uuid "$GC_ENDPOINT_UUID" \
        --log-to-console
fi
exec globus-compute-endpoint start "$GC_ENDPOINT"
