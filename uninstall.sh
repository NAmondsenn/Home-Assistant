#!/bin/bash
# Takes a satellite off this machine - the service, then, if you say so, the Python
# packages, the models, its settings and saved data, raspotify and the shared audio
# setup. Asks before each, and before anything which can't be got back. It doesn't do
# a brain, and says so.
#
#   bash uninstall.sh          asks as it goes
#   bash uninstall.sh --yes    removes the service and the Python packages, and keeps
#                              everything else
#
# The repository folder itself is left; delete it yourself once this has run.

source "$(dirname "${BASH_SOURCE[0]}")/setup.sh"

for argument in "$@"; do
    case "$argument" in
        --yes) ASSUME_YES=true ;;
        *) echo "Unknown option: ${argument} (see the top of uninstall.sh)" >&2; exit 1 ;;
    esac
done

# A brain has a system service, a user of its own and its files in that user's home, none
# of which the steps below look at - they would leave nova-backend enabled and look in
# root's home, so it's stopped here before anything is touched.
decide_role
if [ "$ROLE" = "brain" ]; then
    problem "This is a brain, and uninstall.sh only takes a satellite off a machine."
    problem "To take the brain off by hand: systemctl disable --now ${BRAIN_SERVICE_NAME}, delete ${BRAIN_SERVICE_FILE}, then delete the clone and, if you want them gone, ${CONFIG_DIRECTORY} and ${NOVA_DATA:-${SERVICE_HOME}/.local/share/nova}."
    exit 1
fi

DATA_DIRECTORY="${NOVA_DATA:-${SERVICE_HOME}/.local/share/nova}"

echo "Removing Nova from ${PROJECT_DIRECTORY}"

step "Service"
if [ -f "$SERVICE_FILE" ]; then
    systemctl --user disable -q --now "$SERVICE_NAME" || true
    rm -f "$SERVICE_FILE"
    systemctl --user daemon-reload
    done_ "Stopped and removed"
else
    skipped "Not installed"
fi

step "Python packages"
if [ -d "$VENV_DIRECTORY" ] && [ "$(ask "Remove ${VENV_DIRECTORY}? (y/n)" "y")" = "y" ]; then
    rm -rf "$VENV_DIRECTORY"
    done_ "Removed"
else
    skipped "Kept"
fi

step "Models"
if [ -d "$MODELS_DIRECTORY" ] && [ "$(ask "Remove the models, including your wake word model? (y/n)" "n")" = "y" ]; then
    rm -rf "$MODELS_DIRECTORY"
    done_ "Removed"
else
    skipped "Kept"
fi

step "Settings, keys and saved data"
echo "  ${CONFIG_DIRECTORY} and ${DATA_DIRECTORY} hold your settings and keys, timers,"
echo "  alarms, volumes, the Spotify sign-in and the logs. They can't be got back."
if [ "$(ask "Delete them? (yes/n)" "n")" = "yes" ]; then
    rm -rf "$CONFIG_DIRECTORY" "$DATA_DIRECTORY"
    done_ "Deleted"
else
    skipped "Kept - a reinstall picks them up again"
fi

step "Spotify Connect (raspotify)"
if [ -f /etc/raspotify/conf ] && [ "$(ask "Remove raspotify and its nightly restart? (y/n)" "n")" = "y" ]; then
    sudo systemctl disable -q --now raspotify-restart.timer || true
    sudo rm -f /etc/systemd/system/raspotify-restart.service /etc/systemd/system/raspotify-restart.timer
    sudo rm -rf /etc/systemd/system/raspotify.service.d
    # Purged, as removing leaves /etc/raspotify/conf behind, and setup.sh takes a conf it
    # finds for raspotify already being set up.
    sudo apt-get purge -y -qq raspotify
    sudo systemctl daemon-reload
    done_ "Removed"
else
    skipped "Kept"
fi

step "Shared audio (/etc/asound.conf)"
backup=$(ls -t /etc/asound.conf.backup.* 2> /dev/null | head -1 || true)
if [ -n "$backup" ] && [ "$(ask "Put back ${backup}? (y/n)" "n")" = "y" ]; then
    sudo cp "$backup" /etc/asound.conf
    done_ "Put back"
else
    skipped "Left as it is"
fi

echo ""
echo "Done. The code is still in ${PROJECT_DIRECTORY} - delete it with: rm -rf ${PROJECT_DIRECTORY}"
