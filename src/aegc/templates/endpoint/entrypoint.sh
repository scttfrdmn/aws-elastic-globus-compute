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
    # User endpoint processes (where AWSEngine runs) log to files: copy them to the console too.
    (
        seen=""
        while true; do
            for f in "$HOME"/.globus_compute/uep.*/endpoint.log; do
                [ -f "$f" ] || continue
                case " $seen " in
                    *" $f "*) ;;
                    *) seen="$seen $f"; tail -n +1 -F "$f" 2>/dev/null | sed -u 's/^/[uep] /' & ;;
                esac
            done
            sleep 5
        done
    ) &
    exec globus-compute-endpoint start "$GC_ENDPOINT" --endpoint-uuid "$GC_ENDPOINT_UUID" \
        --log-to-console
fi
exec globus-compute-endpoint start "$GC_ENDPOINT"
