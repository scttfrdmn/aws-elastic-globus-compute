#!/bin/sh
# With arguments: run them (e.g. `globus-compute-endpoint login`). Without: start the endpoint.
set -e
if [ "$#" -gt 0 ]; then
    exec "$@"
fi
: "${GC_ENDPOINT:?set GC_ENDPOINT}"

# Copy user endpoint process logs to the console: logs that already exist (from before a restart)
# follow from their end, new ones from the top. $1: a glob of endpoint.log files; $2: the field of
# the path that names the local user (multi-user), or empty.
tail_ueps() {
    (
        seen=""
        from="-n 0"
        while true; do
            for f in $1; do
                [ -f "$f" ] || continue
                case " $seen " in
                    *" $f "*) ;;
                    *)
                        seen="$seen $f"
                        tag="[uep]"
                        [ -z "$2" ] || tag="[uep $(echo "$f" | cut -d/ -f"$2")]"
                        tail $from -F "$f" 2>/dev/null | sed -u "s|^|$tag |" &
                        ;;
                esac
            done
            from="-n +1"
            sleep 5
        done
    ) &
}

if [ -n "${GC_MULTI_USER:-}" ]; then
    # A multi-user endpoint (#62): the root Core Endpoint maps each submitter's Globus identity to
    # a local user and starts that user's endpoint process as that user. Without PAM, Compute
    # drops all capabilities and sets no-new-privs in that process.
    [ "$(id -u)" = 0 ] || { echo "aegc: a multi-user endpoint runs as root (--user 0)"; exit 1; }
    ep="$HOME/.globus_compute/$GC_ENDPOINT"
    users="$ep/aegc_local_users"
    [ -s "$users" ] || { echo "aegc: no $users; run aegc endpoint configure --multi-user"; exit 1; }
    rm -f "$ep/daemon.pid"
    while read -r name uid; do
        [ -n "$name" ] || continue
        if ! cur=$(id -u "$name" 2>/dev/null); then
            useradd --uid "$uid" --user-group --no-create-home --home-dir "/home/$name" \
                --shell /usr/sbin/nologin "$name"
        elif [ "$cur" -lt 1000 ]; then
            echo "aegc: refusing to map to system account $name (uid $cur)"; exit 1
        fi
        g=$(id -g "$name")
        install -d -m 700 -o "$name" -g "$g" "/home/$name" "/home/$name/.globus_compute"
    done < "$users"
    # ~/.aws (read-only) for each mapped user: a copy each can read, refreshed when it changes on
    # the host (e.g. `aws sso login`). The template's row picks each user's profile.
    sync_aws() {
        while read -r name uid; do
            [ -n "$name" ] || continue
            rm -rf "/home/$name/.aws.new"
            cp -R /etc/aegc/aws "/home/$name/.aws.new"
            chown -R "$name:$(id -g "$name")" "/home/$name/.aws.new"
            chmod -R go-rwx "/home/$name/.aws.new"
            rm -rf "/home/$name/.aws"
            mv "/home/$name/.aws.new" "/home/$name/.aws"
        done < "$users"
    }
    if [ -d /etc/aegc/aws ]; then
        sync_aws || echo "aegc: couldn't copy ~/.aws to the mapped users"
        stamp=$(mktemp)
        (
            while sleep 60; do
                if [ -n "$(find /etc/aegc/aws -newer "$stamp" 2>/dev/null | head -n 1)" ]; then
                    touch "$stamp"
                    sync_aws || echo "aegc: couldn't refresh the ~/.aws copies"
                fi
            done
        ) &
    fi
    tail_ueps "/home/*/.globus_compute/uep.*/endpoint.log" 3
    if [ -n "${GC_ENDPOINT_UUID:-}" ]; then
        exec globus-compute-endpoint start "$GC_ENDPOINT" --endpoint-uuid "$GC_ENDPOINT_UUID" \
            --log-to-console
    fi
    exec globus-compute-endpoint start "$GC_ENDPOINT" --log-to-console
fi

# A container stopped without `globus-compute-endpoint stop` leaves a stale pid file (spike #3).
rm -f "$HOME/.globus_compute/$GC_ENDPOINT/daemon.pid"
# The cloud endpoint (#22) keeps its id across instance replacements and logs to the console,
# which the host's awslogs driver ships to CloudWatch.
if [ -n "${GC_ENDPOINT_UUID:-}" ]; then
    # User endpoint processes (where AWSEngine runs) log to files: copy them to the console too.
    tail_ueps "$HOME/.globus_compute/uep.*/endpoint.log" ""
    exec globus-compute-endpoint start "$GC_ENDPOINT" --endpoint-uuid "$GC_ENDPOINT_UUID" \
        --log-to-console
fi
exec globus-compute-endpoint start "$GC_ENDPOINT"
