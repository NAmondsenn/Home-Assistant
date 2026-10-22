#!/bin/bash
# Updates Nova to the latest code on the branch it's on, and restarts it - from inside
# the clone, as the user it runs as:
#
#   bash update.sh              fetches the latest code first
#   bash update.sh --no-pull    uses the code as it is, e.g. after switching branch
#
# Only the code, its Python packages, any new models and the service file change.
# This install's settings, keys and saved data, in ~/.config/nova and
# ~/.local/share/nova, are never touched. To go back, check out the version before
# and run this again.
#
# It looks after whichever role setup.sh installed - the satellite or the brain - which
# it reads from ~/.config/nova/role (see decide_role in setup.sh).

source "$(dirname "${BASH_SOURCE[0]}")/setup.sh"

PULL=true
for argument in "$@"; do
    case "$argument" in
        --no-pull) PULL=false ;;
        *) echo "Unknown option: ${argument} (see the top of update.sh)" >&2; exit 1 ;;
    esac
done

decide_role
echo "Updating Nova's ${ROLE} in ${PROJECT_DIRECTORY}"
step "Code"
if [ "$PULL" = true ]; then
    # As the service user, so the files it brings are theirs, and git doesn't object to
    # a clone it doesn't think root should touch.
    before=$(as_user git -C "$PROJECT_DIRECTORY" rev-parse HEAD)
    as_user git -C "$PROJECT_DIRECTORY" pull --ff-only
    # This script and setup.sh's steps were read before the pull, so any new steps
    # the update brings - a new model to fetch, say - would be missed. So once there's
    # new code, it starts again on that.
    if [ "$(as_user git -C "$PROJECT_DIRECTORY" rev-parse HEAD)" != "$before" ]; then
        exec bash "${PROJECT_DIRECTORY}/update.sh" --no-pull
    fi
fi
done_ "On $(as_user git -C "$PROJECT_DIRECTORY" rev-parse --abbrev-ref HEAD) at $(as_user git -C "$PROJECT_DIRECTORY" log -1 --format='%h %s')"

# An install from before roles has none saved yet, so what decide_role worked out is.
run_steps save_role install_python_packages download_models

# /etc/asound.conf is only ever written by setup.sh, so one written before the
# shorter audio bursts is pointed out rather than changed behind the user's back. Only
# a satellite has one.
if [ "$ROLE" = "satellite" ] && grep -q "pcm.shared_out" /etc/asound.conf 2> /dev/null && ! grep -q "period_size" /etc/asound.conf; then
    problem "/etc/asound.conf is from before Nova's shorter audio bursts - run 'bash setup.sh' and let it write it again, for quicker stopping and wake word."
fi

# Stopped before its files are moved, so the old version can't write to where they
# used to be in the meantime. The satellite is a user service, the brain a system one.
if [ "$ROLE" = "brain" ]; then
    stop_service() { root_do systemctl stop "$BRAIN_SERVICE_NAME" || true; }
    start_service() { root_do systemctl start "$BRAIN_SERVICE_NAME"; }
    service_is_active() { systemctl is-active -q "$BRAIN_SERVICE_NAME"; }
    show_log() { journalctl -u "$BRAIN_SERVICE_NAME" -n "$1" --no-pager -o cat; }
else
    stop_service() { systemctl --user stop "$SERVICE_NAME" || true; }
    start_service() { systemctl --user start "$SERVICE_NAME"; }
    service_is_active() { systemctl --user is-active -q "$SERVICE_NAME"; }
    show_log() { journalctl --user-unit "$SERVICE_NAME" -n "$1" --no-pager -o cat; }
fi

step "Restarting"
stop_service
run_steps move_old_files
install_service
start_service
# It says it's ready once its models are loaded (see voice_assistant/watchdog.py), so
# "active" means it really has started.
for _ in $(seq 1 90); do
    if service_is_active; then
        break
    fi
    sleep 1
done
if service_is_active; then
    done_ "Running"
    show_log 12
else
    problem "It didn't start - here's why:"
    show_log 30
    exit 1
fi
