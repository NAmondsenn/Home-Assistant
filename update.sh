#!/bin/bash
# Updates Nova to the latest code on the branch it's on, and restarts it - from inside
# the clone: on a satellite, as the user it runs as, and on the brain, as root, which
# does all but the system's own steps as the brain's user (see run_steps in setup.sh):
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

# Nova goes on running the code it started with until it restarts, so code just pulled
# that doesn't get through the steps after the pull and the settings check - a package
# install failing, or the phone's session dropping mid-step - is taken back out, and a
# reboot starts what's running now.
# BEGIN take back
take_back() {
    local why="$1"
    # The reset comes before any message, so a session that's gone loses only that.
    if [ -z "${NOVA_UPDATED_FROM:-}" ]; then
        # Run with --no-pull, on code checked out by hand, so there's nothing to go back to.
        problem "${why} The code checked out is in place, but Nova is still running the code it started with until it next restarts:"
    elif as_user git -C "$PROJECT_DIRECTORY" reset -q --keep "$NOVA_UPDATED_FROM"; then
        # --keep refuses, rather than throws away, local changes to the files it would move.
        problem "${why} The new code has been taken back out, so Nova goes on running the code it had before, even once it restarts:"
    else
        problem "${why} The new code couldn't be taken back out, so check out ${NOVA_UPDATED_FROM} before Nova next restarts:"
    fi
}

watch_steps() {
    steps_done=false
    trap failed_after_pull EXIT
    # With no trap of their own, a dropped session's hangup or a Ctrl+C would end bash
    # with the EXIT trap seeing $? at 0, as if the steps had all gone through.
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

steps_finished() {
    steps_done=true
    trap - EXIT HUP INT TERM
}

failed_after_pull() {
    local status=$?
    if [ "$steps_done" = true ]; then
        return
    fi
    # Ignored, by git too, so a second Ctrl+C can't cut the reset short half-way.
    trap '' HUP INT TERM
    # Only an update that fetched new code has new code to take back, or old code its
    # packages might not suit.
    if [ -n "${NOVA_UPDATED_FROM:-}" ]; then
        take_back "A step after fetching the new code failed - Nova is still running the code it had before."
        problem "Python packages it installed stay installed, so put right what failed and run this again soon - until then the old code may not start with them."
    else
        take_back "A step of the update failed - put right what failed, then run this again."
    fi
    exit $(( status == 0 ? 1 : status ))
}
# END take back

# The run started again on the new code is watched from here, so even a role file it
# can't read leaves the code it fetched for nothing. The run before can't be - exec drops
# traps - and any other run has fetched nothing yet, so is watched from its steps.
if [ "$PULL" = false ] && [ -n "${NOVA_UPDATED_FROM:-}" ]; then
    watch_steps
fi

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
    # And with where it was before, to go back to if the new code is refused.
    if [ "$(as_user git -C "$PROJECT_DIRECTORY" rev-parse HEAD)" != "$before" ]; then
        export NOVA_UPDATED_FROM="$before"
        exec bash "${PROJECT_DIRECTORY}/update.sh" --no-pull
    fi
fi
done_ "On $(as_user git -C "$PROJECT_DIRECTORY" rev-parse --abbrev-ref HEAD) at $(as_user git -C "$PROJECT_DIRECTORY" log -1 --format='%h %s')"

watch_steps

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
    show_log() { root_do journalctl -u "$BRAIN_SERVICE_NAME" -n "$1" --no-pager -o cat; }
else
    stop_service() { systemctl --user stop "$SERVICE_NAME" || true; }
    start_service() { systemctl --user start "$SERVICE_NAME"; }
    service_is_active() { systemctl --user is-active -q "$SERVICE_NAME"; }
    show_log() { journalctl --user-unit "$SERVICE_NAME" -n "$1" --no-pager -o cat; }
fi

# The new code reads the settings as it starts, so a setting it refuses is found out
# here, while the old version is still running, rather than after it has been stopped.
# Exit 1 from the check is the settings, 2 is the code itself (a missing package, say).
# Code just pulled is then taken back out, so whatever restarts Nova next - the page's
# Restart now, the watchdog, a power cut - starts the code it's running now, not the
# code refused.
# BEGIN settings check
settings_check() {
    local output status=0 why
    output=$(cd "$PROJECT_DIRECTORY" && as_user "$PYTHON" -c '
import sys
try:
    from voice_assistant.config import Config, ConfigError
except Exception as e:
    print(f"{type(e).__name__}: {e}")
    sys.exit(2)
try:
    Config()
except ConfigError as e:
    print(e)
    sys.exit(1)
except Exception as e:
    print(f"{type(e).__name__}: {e}")
    sys.exit(2)
' 2>&1) || status=$?
    if [ "$status" = 0 ]; then
        return 0
    fi
    if [ "$status" = 1 ]; then
        why="Nova's settings won't start the new code - put them right, then run this again."
    else
        why="The new code won't start on this machine, whatever the settings - fix that, then run this again."
    fi
    # This takes the code back and says so itself, so the watch over the steps stands
    # down first rather than doing both a second time - with a hangup or Ctrl+C ignored,
    # by git too, as the watch's own take-back has, so neither cuts the reset short.
    # They're ignored before the watch's traps go, not after as with steps_finished,
    # which would leave a moment when a hangup ends the run with nothing taken back.
    trap '' HUP INT TERM
    steps_done=true
    trap - EXIT
    take_back "$why"
    echo "$output" | tail -n 5 >&2
    return 1
}
# END settings check
settings_check || exit 1
steps_finished

step "Restarting"
stop_service
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
