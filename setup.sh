#!/bin/bash
# Installs Nova on a Raspberry Pi (Raspberry Pi OS or Debian), from inside a clone of
# the repository. Safe to run again: anything already set up is left as it is.
#
#   bash setup.sh                 asks as it goes
#   bash setup.sh --yes           takes what it finds and the defaults, asking nothing
#   bash setup.sh --no-spotify    skips raspotify, for Navidrome only
#   bash setup.sh --no-service    doesn't start Nova at boot
#
# This install's own settings and keys go in ~/.config/nova, and what it saves in
# ~/.local/share/nova - never in the repository, so updating never conflicts with
# them (see voice_assistant/paths.py). update.sh and uninstall.sh sit beside this,
# and use its steps.

set -euo pipefail

PROJECT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_DIRECTORY="${PROJECT_DIRECTORY}/models"
VENV_DIRECTORY="${PROJECT_DIRECTORY}/venv"
PYTHON="${VENV_DIRECTORY}/bin/python"
CONFIG_DIRECTORY="${NOVA_CONFIG:-${HOME}/.config/nova}"
SETTINGS_FILE="${CONFIG_DIRECTORY}/config.yaml"
SECRETS_FILE="${CONFIG_DIRECTORY}/.env"

SERVICE_NAME="voice-assistant"
SERVICE_TEMPLATE="${PROJECT_DIRECTORY}/${SERVICE_NAME}.service"
SERVICE_FILE="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"

PIPER_VERSION="2023.11.14-2"
VOICE_MODEL="en_GB-cori-medium"
VOICE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/cori/medium"

# The SHA-256 of everything downloaded and then run or trusted, so a tampered
# release or mirror stops setup rather than being run on the Pi. Taken from the
# files themselves - Piper's on 20 October 2026, when the binary inside the aarch64
# archive matched the one Nova had been using, and the voice's on 22 October 2026,
# when the model's matched Hugging Face's own. Changing PIPER_VERSION or
# VOICE_MODEL means updating these too.
PIPER_SHA256_AARCH64="fea0fd2d87c54dbc7078d0f878289f404bd4d6eea6e7444a77835d1537ab88eb"
PIPER_SHA256_X86_64="a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992"
VOICE_SHA256_MODEL="1899f98e5fb8310154f3c2973f4b8a929ba7245e722b3d3a85680b833d95f10d"
VOICE_SHA256_CONFIG="e262c16d7f192f69d4edd6b4ef8a5915379e67495fcc402f1ab15eeb33da3d36"
RASPOTIFY_KEY_SHA256="36dd5aadd073950d55c404f0e0c6c2e0555259bf38bd8b6448c35591a3cc9cfd"

ASSUME_YES=false
WITH_SPOTIFY=true
WITH_SERVICE=true

# How each step went, said the same way every time.
done_() { echo "  ✓ $*"; }
skipped() { echo "  – $*"; }
problem() { echo "  ✗ $*" >&2; }
step() { echo ""; echo "$*"; }

# Stops setup, removing the file, if a download isn't the one its hash was taken from.
check_sha256() {
    local file="$1" expected="$2"
    if ! echo "${expected}  ${file}" | sha256sum -c --status; then
        rm -f "$file"
        problem "$(basename "$file") isn't the file Nova expects - its SHA-256 doesn't match, so it may have been tampered with. Stopping."
        exit 1
    fi
}

# Asks a question, or takes the default without asking under --yes.
ask() {
    local question="$1" default="$2" answer
    if [ "$ASSUME_YES" = true ]; then
        echo "$default"
        return
    fi
    read -rp "  ${question} [${default}]: " answer
    echo "${answer:-$default}"
}

check_system() {
    step "Checking this machine"
    if ! command -v apt-get > /dev/null; then
        problem "This needs Raspberry Pi OS or Debian (apt)."
        exit 1
    fi
    if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        problem "Python 3.11 or newer is needed; this has $(python3 --version)."
        exit 1
    fi
    done_ "$(python3 --version), $(uname -m)"
    if aplay -l 2> /dev/null | grep -q "^card" && arecord -l 2> /dev/null | grep -q "^card"; then
        done_ "Found a speaker and a microphone"
    else
        problem "No speaker or microphone found yet - plug them in before starting Nova (aplay -l, arecord -l)."
    fi
}

install_system_packages() {
    step "System packages"
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3-pip python3-venv libportaudio2 libsndfile1 ffmpeg alsa-utils \
        wget curl tar git
    done_ "Installed"
}

# Installs the Python packages - exactly those requirements.lock lists on Python 3.13,
# which is what the Pi has, or the newest which fit requirements.txt otherwise.
install_python_packages() {
    step "Python packages"
    if [ ! -x "$PYTHON" ]; then
        python3 -m venv "$VENV_DIRECTORY"
        "$PYTHON" -m pip install -q --upgrade pip
    fi
    if "$PYTHON" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))'; then
        "$PYTHON" -m pip install -q --no-deps -r "${PROJECT_DIRECTORY}/requirements.lock"
        done_ "Installed the versions in requirements.lock"
    else
        "$PYTHON" -m pip install -q -r "${PROJECT_DIRECTORY}/requirements.txt"
        # Without its declared dependencies, since it pins tflite-runtime, which has
        # no wheels for Python 3.12+ - Nova uses its ONNX models instead.
        "$PYTHON" -m pip install -q --no-deps "openwakeword>=0.6.0"
        done_ "Installed from requirements.txt (requirements.lock is for Python 3.13)"
    fi
}

# Moves anything an older install kept in the repository folder - its settings, keys
# and saved timers - to where they now belong, before anything below looks for them.
move_old_files() {
    step "Settings and saved data"
    local moved
    moved=$(cd "$PROJECT_DIRECTORY" && "$PYTHON" -c 'from voice_assistant import paths; print(", ".join(paths.migrate()))')
    if [ -n "$moved" ]; then
        done_ "Moved out of the repository folder: ${moved}"
    else
        skipped "Nothing to move"
    fi
}

write_settings() {
    if [ -f "$SETTINGS_FILE" ]; then
        skipped "${SETTINGS_FILE} already exists, so it's left as it is"
        return
    fi
    local name wake_word speaker
    echo "  The name and wake word only change what Nova says - what it listens for is"
    echo "  the wake word model's (see below)."
    name=$(ask "Assistant name" "Nova")
    wake_word=$(ask "Wake word" "Hey Nova")
    speaker=$(ask "Spotify Connect speaker name" "Voice-Assistant")

    mkdir -p -m 700 "$CONFIG_DIRECTORY"
    {
        echo "# This install's own settings, on top of the defaults in the repository's"
        echo "# config.yaml - see there for every setting and what it's for."
        if [ "$name" != "Nova" ] || [ "$wake_word" != "Hey Nova" ]; then
            printf 'assistant:\n  name: "%s"\n  wake_word: "%s"\n' "${name//\"/}" "${wake_word//\"/}"
        fi
        if [ "$speaker" != "Voice-Assistant" ]; then
            printf 'spotify:\n  device_name: "%s"\n' "${speaker//\"/}"
        fi
    } > "$SETTINGS_FILE"
    chmod 600 "$SETTINGS_FILE"
    done_ "Saved to ${SETTINGS_FILE}"
}

write_secrets() {
    step "Keys"
    if [ -f "$SECRETS_FILE" ]; then
        skipped "${SECRETS_FILE} already exists, so it's left as it is"
        return
    fi
    local anthropic="" spotify_id="" spotify_secret=""
    if [ "$ASSUME_YES" = false ]; then
        echo "  They're kept in ${SECRETS_FILE}, readable only by you. Leave any blank to"
        echo "  add it there by hand later."
        read -rp "  Anthropic API key: " anthropic
        if [ "$WITH_SPOTIFY" = true ]; then
            read -rp "  Spotify client ID: " spotify_id
            read -rp "  Spotify client secret: " spotify_secret
        fi
    fi
    mkdir -p -m 700 "$CONFIG_DIRECTORY"
    (
        umask 077
        cat > "$SECRETS_FILE" <<EOF
ANTHROPIC_API_KEY=${anthropic}
SPOTIFY_CLIENT_ID=${spotify_id}
SPOTIFY_CLIENT_SECRET=${spotify_secret}
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
# For the lights and calendar - a long-lived token from Nova's own Home Assistant user:
# HA_TOKEN=
# For music from Navidrome:
# NAVIDROME_PASSWORD=
EOF
    )
    done_ "Saved to ${SECRETS_FILE}"
}

find_wake_word_model() {
    step "Wake word model"
    local wake_word key found=()
    wake_word=$(cd "$PROJECT_DIRECTORY" && "$PYTHON" -c 'from voice_assistant.config import Config; print(Config().section("assistant")["wake_word"])')
    key=$(echo "$wake_word" | tr '[:upper:]' '[:lower:]' | tr -s ' ' '_')
    mkdir -p "$MODELS_DIRECTORY"
    if [ -f "${MODELS_DIRECTORY}/${key}.onnx" ]; then
        skipped "models/${key}.onnx is already there"
        return
    fi
    # openWakeWord's own models and Piper's voices are .onnx files too, so they're left out.
    mapfile -t found < <(find "$HOME" /tmp /mnt /media -maxdepth 3 -iname "${key}*.onnx" 2> /dev/null \
        | grep -v "$MODELS_DIRECTORY" || true)
    if [ ${#found[@]} -ge 1 ] && [ "$(ask "Use ${found[0]}? (y/n)" "y")" = "y" ]; then
        cp "${found[0]}" "${MODELS_DIRECTORY}/${key}.onnx"
        done_ "Copied ${found[0]} to models/${key}.onnx"
    else
        problem "No ${key}.onnx found. Copy it to ${MODELS_DIRECTORY}/ - until then, Nova listens for 'Hey Jarvis'."
    fi
}

# Downloads whatever models aren't there yet - Piper and its voice, the Whisper models
# config.yaml names, and openWakeWord's own. Run by update.sh too, for any new ones.
download_models() {
    step "Models"
    mkdir -p "$MODELS_DIRECTORY"
    if [ ! -x "${MODELS_DIRECTORY}/piper/piper" ]; then
        local archive hash
        case "$(uname -m)" in
            aarch64) archive="piper_linux_aarch64.tar.gz"; hash="$PIPER_SHA256_AARCH64" ;;
            x86_64) archive="piper_linux_x86_64.tar.gz"; hash="$PIPER_SHA256_X86_64" ;;
            *) problem "No ready-made Piper for $(uname -m)"; exit 1 ;;
        esac
        if ! wget -q "https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/${archive}" \
            -O "${MODELS_DIRECTORY}/${archive}"; then
            rm -f "${MODELS_DIRECTORY}/${archive}"
            problem "Couldn't download Piper - check the internet connection, and run this again."
            exit 1
        fi
        check_sha256 "${MODELS_DIRECTORY}/${archive}" "$hash"
        tar -xzf "${MODELS_DIRECTORY}/${archive}" -C "$MODELS_DIRECTORY"
        rm -f "${MODELS_DIRECTORY}/${archive}"
        done_ "Piper ${PIPER_VERSION}"
    fi
    for file in "${VOICE_MODEL}.onnx" "${VOICE_MODEL}.onnx.json"; do
        if [ ! -f "${MODELS_DIRECTORY}/${file}" ]; then
            local hash="$VOICE_SHA256_MODEL"
            [ "$file" = "${VOICE_MODEL}.onnx.json" ] && hash="$VOICE_SHA256_CONFIG"
            if ! wget -q "${VOICE_URL}/${file}" -O "${MODELS_DIRECTORY}/${file}.part"; then
                rm -f "${MODELS_DIRECTORY}/${file}.part"
                problem "Couldn't download the voice - check the internet connection, and run this again."
                exit 1
            fi
            check_sha256 "${MODELS_DIRECTORY}/${file}.part" "$hash"
            mv "${MODELS_DIRECTORY}/${file}.part" "${MODELS_DIRECTORY}/${file}"
            done_ "${file}"
        fi
    done
    # Into the folder config.yaml names, rather than left to the first start - which
    # then needed the internet, and kept them in ~/.cache, where clearing the cache
    # broke the next offline boot.
    (cd "$PROJECT_DIRECTORY" && "$PYTHON" - <<'EOF'
import os
from faster_whisper.utils import download_model
from openwakeword.utils import download_models
from voice_assistant import paths
from voice_assistant.config import Config

config = Config()
whisper = config.section("whisper")
root = os.path.join(paths.PROJECT_DIRECTORY, whisper["download_root"])
for name in filter(None, [whisper.get("model"), whisper.get("fallback_model")]):
    # Looked for locally first, so one already there isn't checked online every update.
    try:
        download_model(name, cache_dir=root, local_files_only=True)
    except Exception:
        download_model(name, cache_dir=root)
download_models()

# Moonshine's model, if that's the speech-to-text in use. It keeps the files it has.
if config.section("speech_to_text").get("engine") == "moonshine":
    import moonshine_voice
    moonshine = config.section("moonshine")
    architecture = moonshine_voice.string_to_model_arch(moonshine["model"].strip().lower().replace("_", "-"))
    moonshine_voice.get_model_for_language("en", architecture, on_progress=lambda fraction, name: None,
                                           cache_root=os.path.join(paths.PROJECT_DIRECTORY, moonshine["download_root"]))
EOF
    )
    done_ "Whisper, Moonshine and openWakeWord models are in place"
}

# Lets the assistant and Spotify share the speaker and microphone. Without it, whichever
# opens the sound card first locks the others out - and ALSA's own default output is
# usually HDMI rather than the speaker.
write_asound_conf() {
    step "Shared audio (/etc/asound.conf)"
    local playback capture
    # Leaves out the Pi's own HDMI outputs.
    playback=$(aplay -l 2> /dev/null | grep "^card" | grep -viE "vc4hdmi|bcm2835" \
        | sed -E 's/^card [0-9]+: ([^ ]+) .*/\1/' | sort -u | head -1 || true)
    # Prefers a microphone which isn't also the speaker - a headset's halves share a name.
    capture=$(arecord -l 2> /dev/null | grep "^card" | grep -viE "vc4hdmi|bcm2835" \
        | sed -E 's/^card [0-9]+: ([^ ]+) .*/\1/' | sort -u | grep -v "^${playback}$" | head -1 || true)
    if [ -z "$playback" ] || [ -z "$capture" ]; then
        problem "Couldn't find a USB speaker and microphone - plug them in and run this again."
        return
    fi
    if [ -f /etc/asound.conf ] && grep -q "pcm.shared_out" /etc/asound.conf; then
        # One written before the shorter bursts is worth writing again.
        local again="n" question="/etc/asound.conf is already set up. Write it again? (y/n)"
        if ! grep -q "period_size" /etc/asound.conf; then
            again="y"
            question="/etc/asound.conf is from before Nova's shorter audio bursts. Write it again? (y/n)"
        fi
        if [ "$(ask "$question" "$again")" != "y" ]; then
            skipped "Left as it is"
            return
        fi
    fi
    playback=$(ask "Speaker card" "$playback")
    capture=$(ask "Microphone card" "$capture")
    if [ -f /etc/asound.conf ]; then
        sudo cp /etc/asound.conf "/etc/asound.conf.backup.$(date +%Y%m%d%H%M%S)"
    fi
    sudo tee /etc/asound.conf > /dev/null <<EOF
# Lets several programs - Spotify playing music, the assistant speaking - share the
# speaker and microphone at once, rather than whoever opens one first locking the
# others out. ipc_perm 0666 lets the root-owned raspotify service share them too.
#
# Both work in 1024-frame (21ms) bursts. ALSA's own choice was 6000 (125ms), which
# was how late the microphone's audio reached the wake word, and how long a sound
# Nova had stopped carried on playing - measured on the Pi, 130ms and 240ms
# typically, against 50ms and under 200ms with these. The 8192-frame buffer behind
# them is only slack, and adds no delay of its own. Changes here take effect once
# everything using the speaker and microphone - Nova and raspotify - has restarted.
pcm.!default {
    type asym
    playback.pcm "plug:shared_out"
    capture.pcm "plug:shared_in"
}

pcm.shared_out {
    type dmix
    ipc_key 2048
    ipc_perm 0666
    slave {
        pcm "hw:CARD=${playback},DEV=0"
        rate 48000
        format S16_LE
        channels 2
        period_size 1024
        buffer_size 8192
    }
}

pcm.shared_in {
    type dsnoop
    ipc_key 2049
    ipc_perm 0666
    slave {
        pcm "hw:CARD=${capture},DEV=0"
        rate 48000
        format S16_LE
        channels 1
        period_size 1024
        buffer_size 8192
    }
}

ctl.!default {
    type hw
    card ${playback}
}
EOF
    done_ "Speaker ${playback}, microphone ${capture} (the old one is kept as /etc/asound.conf.backup.*)"
    if systemctl is-active -q raspotify 2> /dev/null; then
        sudo systemctl restart raspotify
    fi
}

# Sets (or replaces) a key in /etc/raspotify/conf, whether it's currently missing,
# commented out, or set to something else.
set_raspotify_conf() {
    local key="$1" value="$2"
    if sudo grep -qE "^#?${key}=" /etc/raspotify/conf; then
        sudo sed -i "s|^#\?${key}=.*|${key}=${value}|" /etc/raspotify/conf
    else
        echo "${key}=${value}" | sudo tee -a /etc/raspotify/conf > /dev/null
    fi
}

install_raspotify() {
    step "Spotify Connect (raspotify)"
    if [ "$WITH_SPOTIFY" = false ]; then
        skipped "Left out (--no-spotify)"
        return
    fi
    if [ -f /etc/raspotify/conf ]; then
        skipped "Already installed"
        return
    fi
    if [ "$(ask "Install raspotify, so the Pi shows up as a Spotify speaker? Needs Premium (y/n)" "n")" != "y" ]; then
        skipped "Not installed"
        return
    fi
    local speaker
    speaker=$(cd "$PROJECT_DIRECTORY" && "$PYTHON" -c 'from voice_assistant.config import Config; print(Config().section("spotify")["device_name"])')
    # The key apt trusts for raspotify's packages, so it's checked before it's trusted.
    local key
    key=$(mktemp)
    if ! curl -sSfL https://dtcooper.github.io/raspotify/key.asc -o "$key"; then
        rm -f "$key"
        problem "Couldn't download raspotify's key - check the internet connection, and run this again."
        exit 1
    fi
    check_sha256 "$key" "$RASPOTIFY_KEY_SHA256"
    sudo install -m 644 "$key" /usr/share/keyrings/raspotify_key.asc
    rm -f "$key"
    echo 'deb [signed-by=/usr/share/keyrings/raspotify_key.asc] https://dtcooper.github.io/raspotify raspotify main' \
        | sudo tee /etc/apt/sources.list.d/raspotify.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq raspotify

    # The device name must match Nova's settings, or it won't recognise its own speaker.
    set_raspotify_conf "LIBRESPOT_NAME" "\"${speaker}\""
    # librespot's default logarithmic volume curve makes the bottom half of Spotify's
    # volume slider near-silent; linear behaves as expected.
    set_raspotify_conf "LIBRESPOT_VOLUME_CTRL" "\"linear\""
    # Credential caching must stay on, or the device logs out on every restart.
    sudo sed -i 's|^LIBRESPOT_DISABLE_CREDENTIAL_CACHE=|#LIBRESPOT_DISABLE_CREDENTIAL_CACHE=|' /etc/raspotify/conf
    # The shared output above, rather than the hardware directly, which would lock the
    # speaker and leave the assistant unable to speak while music plays.
    set_raspotify_conf "LIBRESPOT_DEVICE" "\"default\""
    # raspotify's packaged service sandbox doesn't grant /var/lib/raspotify, where
    # librespot keeps its Spotify login, so without this it stays logged out.
    sudo mkdir -p /etc/systemd/system/raspotify.service.d
    printf '[Service]\nStateDirectory=raspotify\n' | sudo tee /etc/systemd/system/raspotify.service.d/override.conf > /dev/null
    # librespot's connection can drop without the process exiting, leaving the device
    # silently missing from the account, so it's restarted every night.
    sudo cp "${PROJECT_DIRECTORY}/raspotify-restart.service" "${PROJECT_DIRECTORY}/raspotify-restart.timer" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now raspotify-restart.timer
    sudo systemctl restart raspotify
    done_ "raspotify plays as '${speaker}'"
    echo "  It needs signing in to Spotify once - see the README."
}

# Writes the user service from its template, with this install's paths filled in. Run
# by update.sh too, so a changed service file takes effect.
install_service() {
    step "Starting at boot"
    mkdir -p "$(dirname "$SERVICE_FILE")"
    sed -e "s|__PROJECT_DIRECTORY__|${PROJECT_DIRECTORY}|g" -e "s|__PYTHON__|${PYTHON}|g" \
        "$SERVICE_TEMPLATE" > "$SERVICE_FILE"
    systemctl --user daemon-reload
    systemctl --user enable -q "$SERVICE_NAME"
    # Lingering lets a user service start at boot without anyone logging in - the
    # whole point on a headless Pi.
    if ! loginctl show-user "$USER" -p Linger 2> /dev/null | grep -q "yes"; then
        sudo loginctl enable-linger "$USER"
    fi
    done_ "Installed as the ${SERVICE_NAME} user service"
}

main() {
    for argument in "$@"; do
        case "$argument" in
            --yes) ASSUME_YES=true ;;
            --no-spotify) WITH_SPOTIFY=false ;;
            --no-service) WITH_SERVICE=false ;;
            *) echo "Unknown option: ${argument} (see the top of setup.sh)" >&2; exit 1 ;;
        esac
    done

    echo "Installing Nova in ${PROJECT_DIRECTORY}"
    check_system
    install_system_packages
    install_python_packages
    move_old_files
    write_settings
    write_secrets
    find_wake_word_model
    download_models
    write_asound_conf
    install_raspotify
    if [ "$WITH_SERVICE" = true ]; then
        install_service
        systemctl --user restart "$SERVICE_NAME"
    else
        step "Starting at boot"
        skipped "Left out (--no-service) - run it by hand with: ${PYTHON} ${PROJECT_DIRECTORY}/main.py"
    fi

    echo ""
    echo "Done. Settings: ${SETTINGS_FILE} · keys: ${SECRETS_FILE}"
    echo "For Spotify, sign in once:  cd ${PROJECT_DIRECTORY} && venv/bin/python -m voice_assistant.spotify_controller"
    echo "Watch it working:           journalctl --user-unit ${SERVICE_NAME} -f"
    echo "Later, to update:           bash ${PROJECT_DIRECTORY}/update.sh"
}

# Run, unless update.sh or uninstall.sh is only borrowing the steps above.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
