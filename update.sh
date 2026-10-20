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

source "$(dirname "${BASH_SOURCE[0]}")/setup.sh"

PULL=true
for argument in "$@"; do
    case "$argument" in
        --no-pull) PULL=false ;;
        *) echo "Unknown option: ${argument} (see the top of update.sh)" >&2; exit 1 ;;
    esac
done

echo "Updating Nova in ${PROJECT_DIRECTORY}"
step "Code"
if [ "$PULL" = true ]; then
    git -C "$PROJECT_DIRECTORY" pull --ff-only
fi
done_ "On $(git -C "$PROJECT_DIRECTORY" rev-parse --abbrev-ref HEAD) at $(git -C "$PROJECT_DIRECTORY" log -1 --format='%h %s')"

install_python_packages
download_models

# Stopped before its files are moved, so the old version can't write to where they
# used to be in the meantime.
step "Restarting"
systemctl --user stop "$SERVICE_NAME" || true
move_old_files
install_service
systemctl --user start "$SERVICE_NAME"
# It says it's ready once its models are loaded (see voice_assistant/watchdog.py), so
# "active" means it really has started.
for _ in $(seq 1 90); do
    if systemctl --user is-active -q "$SERVICE_NAME"; then
        break
    fi
    sleep 1
done
if systemctl --user is-active -q "$SERVICE_NAME"; then
    done_ "Running"
    journalctl --user -u "$SERVICE_NAME" -n 12 --no-pager -o cat
else
    problem "It didn't start - here's why:"
    journalctl --user -u "$SERVICE_NAME" -n 30 --no-pager -o cat
    exit 1
fi
