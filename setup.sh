#!/bin/bash
# Installs Nova, from inside a clone of the repository, in one of its two roles - see
# docs/decisions/0006. Safe to run again: anything already set up is left as it is.
#
#   bash setup.sh                 a satellite, on a Raspberry Pi (Raspberry Pi OS or
#                                 Debian): its microphone, speaker and wake word
#   bash setup.sh --brain         the brain, in its container on PVE2 (Debian): Piper,
#                                 Nemotron, Claude and everything else which thinks
#   bash setup.sh --yes           takes what it finds and the defaults, asking nothing
#   bash setup.sh --no-spotify    leaves Spotify out, for Navidrome only
#   bash setup.sh --no-service    doesn't start Nova at boot
#
# The role is kept in ~/.config/nova/role, so running this again without --brain on a
# brain, and update.sh, both know what they're looking after. --satellite changes an
# install back to a satellite.
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

# Who Nova runs as, and where their home is. Run by a normal user (the Pi), that's them.
# Run as root (the brain's container, which is set up from outside as root), it's the
# user who owns the clone, so everything Nova keeps - the venv, models, settings, keys
# and the role - is theirs, where an existing install's already are, rather than root's.
# Root itself does only the system steps: packages, the service and systemctl.
if [ "$(id -u)" = 0 ]; then
    SERVICE_USER="$(stat -c %U "$PROJECT_DIRECTORY")"
    SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
else
    SERVICE_USER="$(id -un)"
    SERVICE_HOME="$HOME"
fi
CONFIG_DIRECTORY="${NOVA_CONFIG:-${SERVICE_HOME}/.config/nova}"
SETTINGS_FILE="${CONFIG_DIRECTORY}/config.yaml"
SECRETS_FILE="${CONFIG_DIRECTORY}/.env"

ROLE_FILE="${CONFIG_DIRECTORY}/role"
# "satellite" or "brain" - worked out by decide_role, once any --brain or --satellite
# on the command line has been read.
ROLE="${ROLE:-}"

# The satellite is a user service, so it shares the audio devices' session. The brain is
# a system service, with no audio of its own to share.
SERVICE_NAME="voice-assistant"
SERVICE_TEMPLATE="${PROJECT_DIRECTORY}/${SERVICE_NAME}.service"
SERVICE_FILE="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"
BRAIN_SERVICE_NAME="nova-backend"
BRAIN_SERVICE_TEMPLATE="${PROJECT_DIRECTORY}/${BRAIN_SERVICE_NAME}.service"
BRAIN_SERVICE_FILE="/etc/systemd/system/${BRAIN_SERVICE_NAME}.service"

PIPER_VERSION="2023.11.14-2"
VOICE_MODEL="en_US-hfc_female-medium"
VOICE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_female/medium"

# The brain's speech-to-text: NVIDIA's Nemotron Speech Streaming, through sherpa-onnx,
# from the asr-models release of k2-fsa/sherpa-onnx - see voice_assistant/nemotron.py.
# Its folder is where backend.model in config.yaml points. It's under NVIDIA's Open
# Model License.
NEMOTRON_NAME="sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25"
NEMOTRON_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${NEMOTRON_NAME}.tar.bz2"
# The most the archive and what's unpacked from it need, in kilobytes. It's 464MB
# packed, and PVE2's storage is nearly full, so better to say so than to fill it.
NEMOTRON_KILOBYTES_NEEDED=1500000

# The SHA-256 of everything downloaded and then run or trusted, so a tampered
# release or mirror stops setup rather than being run on the Pi. Taken from the
# files themselves - Piper's on 20 October 2026, when the binary inside the aarch64
# archive matched the one Nova had been using, the voice's on 22 October 2026, when
# the model's matched Hugging Face's own, and Nemotron's on 22 October 2026, from the
# release's archive as downloaded that day. Changing PIPER_VERSION, VOICE_MODEL or
# NEMOTRON_NAME means updating these too.
PIPER_SHA256_AARCH64="fea0fd2d87c54dbc7078d0f878289f404bd4d6eea6e7444a77835d1537ab88eb"
PIPER_SHA256_X86_64="a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992"
VOICE_SHA256_MODEL="914c473788fc1fa8b63ace1cdcdb44588f4ae523d3ab37df1536616835a140b7"
VOICE_SHA256_CONFIG="03f1fa0622b80463283592d97aca9f6e89aec345a5c56b7257723e0093c58b6c"
NEMOTRON_SHA256="78e2b79fcf7271553a74402a76b771b09ea40117a39566a79f52235b23db6358"
RASPOTIFY_KEY_SHA256="36dd5aadd073950d55c404f0e0c6c2e0555259bf38bd8b6448c35591a3cc9cfd"

# These can come from the environment, as steps run as another user (see run_steps) are
# given what was chosen on the command line that way.
ASSUME_YES="${ASSUME_YES:-false}"
WITH_SPOTIFY="${WITH_SPOTIFY:-true}"
WITH_SERVICE="${WITH_SERVICE:-true}"

# How each step went, said the same way every time.
done_() { echo "  ✓ $*"; }
skipped() { echo "  – $*"; }
problem() { echo "  ✗ $*" >&2; }
step() { echo ""; echo "$*"; }

# Runs a command as root - through sudo, unless that's who's running this already, as
# in the brain's container, which may not have sudo at all.
root_do() {
    if [ "$(id -u)" = 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

# Runs a command as the service user, with their home, when this is root's run and they
# aren't root; otherwise just runs it.
as_user() {
    if [ "$(id -u)" = 0 ] && [ "$SERVICE_USER" != "root" ]; then
        # From the project folder, as root's own may be one the service user can't enter.
        (cd "$PROJECT_DIRECTORY" && runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" "$@")
    else
        "$@"
    fi
}

# Runs the named steps of this file, in order, as the service user - in a shell of their
# own when this is root's run, so every file they make is theirs. Otherwise, in this one.
run_steps() {
    if [ "$(id -u)" = 0 ] && [ "$SERVICE_USER" != "root" ]; then
        as_user env ROLE="$ROLE" ASSUME_YES="$ASSUME_YES" WITH_SPOTIFY="$WITH_SPOTIFY" WITH_SERVICE="$WITH_SERVICE" \
            bash -c 'source "$1"; shift; for step in "$@"; do "$step"; done' _ "${PROJECT_DIRECTORY}/setup.sh" "$@"
    else
        local step
        for step in "$@"; do
            "$step"
        done
    fi
}

# Works out which role this install has, if the command line didn't say. An install
# from before roles has no role file, so it's looked for by what's there: a brain has
# its system service, installed by hand, and anything else is a Pi, which today's
# installs all are. Guessing "satellite" for a brain would swap its packages and
# service for a satellite's, so the service is checked for first.
decide_role() {
    if [ -z "$ROLE" ]; then
        if [ -f "$ROLE_FILE" ]; then
            ROLE=$(tr -d '[:space:]' < "$ROLE_FILE")
        elif [ -f "$BRAIN_SERVICE_FILE" ]; then
            ROLE="brain"
        else
            ROLE="satellite"
        fi
    fi
    if [ "$ROLE" != "satellite" ] && [ "$ROLE" != "brain" ]; then
        problem "${ROLE_FILE} says '${ROLE}', which isn't a role - it should say satellite or brain."
        exit 1
    fi
    # The brain's service never runs as root, so a clone root owns can't be one.
    if [ "$ROLE" = "brain" ] && [ "$SERVICE_USER" = "root" ] && [ "$(id -u)" = 0 ]; then
        problem "${PROJECT_DIRECTORY} is owned by root, and the brain never runs as root. Clone it as the user it should run as, and run this as root from there."
        exit 1
    fi
}

# Keeps the role where update.sh will find it.
save_role() {
    mkdir -p -m 700 "$CONFIG_DIRECTORY"
    echo "$ROLE" > "$ROLE_FILE"
}

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
    done_ "$(python3 --version), $(uname -m), as a ${ROLE}"
    # The brain has no speaker or microphone of its own.
    if [ "$ROLE" = "brain" ]; then
        return
    fi
    if aplay -l 2> /dev/null | grep -q "^card" && arecord -l 2> /dev/null | grep -q "^card"; then
        done_ "Found a speaker and a microphone"
    else
        problem "No speaker or microphone found yet - plug them in before starting Nova (aplay -l, arecord -l)."
    fi
}

install_system_packages() {
    step "System packages"
    root_do apt-get update -qq
    if [ "$ROLE" = "brain" ]; then
        # ffmpeg decodes Navidrome's songs, bzip2 unpacks Nemotron, and with no audio
        # devices there's no PortAudio or ALSA.
        root_do apt-get install -y -qq python3-pip python3-venv libsndfile1 ffmpeg bzip2 wget curl tar git
    else
        root_do apt-get install -y -qq python3-pip python3-venv libportaudio2 libsndfile1 alsa-utils \
            curl tar git
    fi
    done_ "Installed"
}

# Installs the Python packages. The brain's are exactly those requirements-backend.txt
# lists, on Python 3.13, which is what its container has. The satellite's are those
# requirements.lock lists on Python 3.13, which is what the Pi has, or the newest which
# fit requirements.txt otherwise.
install_python_packages() {
    step "Python packages"
    if [ ! -x "$PYTHON" ]; then
        python3 -m venv "$VENV_DIRECTORY"
        "$PYTHON" -m pip install -q --upgrade pip
    fi
    if [ "$ROLE" = "brain" ]; then
        "$PYTHON" -m pip install -q --no-deps -r "${PROJECT_DIRECTORY}/requirements-backend.txt"
        done_ "Installed the versions in requirements-backend.txt"
    elif "$PYTHON" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))'; then
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
    local name wake_word speaker address=""
    echo "  The name and wake word only change what Nova says - what it listens for is"
    echo "  the wake word model's (see below)."
    name=$(ask "Assistant name" "Nova")
    wake_word=$(ask "Wake word" "Hey Nova")
    speaker=$(ask "Spotify Connect speaker name" "Voice-Assistant")
    if [ "$ROLE" = "satellite" ]; then
        address=$(ask "The brain's address, as name:port on your tailnet" "nova:10400")
    fi

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
        if [ -n "$address" ]; then
            printf 'backend:\n  address: "%s"\n' "${address//\"/}"
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
    # The satellite keeps no key but its own password: everything else is on the brain,
    # so a stolen Pi gives up nothing but a way to ask the brain to say things.
    if [ "$ROLE" = "satellite" ]; then
        local password=""
        if [ "$ASSUME_YES" = false ]; then
            echo "  This satellite's password comes from the brain: on the brain, in its clone, run"
            echo "  'venv/bin/python backend.py add-satellite NAME' as the brain's user - from root,"
            echo "  with 'runuser -u nova -- ' in front (nova being the user the brain runs as)."
            echo "  Leave it blank to add it to ${SECRETS_FILE} by hand later."
            read -rp "  Password: " password
        fi
        mkdir -p -m 700 "$CONFIG_DIRECTORY"
        (
            umask 077
            echo "NOVA_BACKEND_TOKEN=${password}" > "$SECRETS_FILE"
        )
        done_ "Saved to ${SECRETS_FILE}"
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

# Downloads Piper and its voice, if they aren't there yet.
download_voice() {
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
}

# Downloads Nemotron, the brain's speech-to-text, if it isn't there yet.
download_nemotron() {
    if [ -f "${MODELS_DIRECTORY}/${NEMOTRON_NAME}/encoder.int8.onnx" ]; then
        return
    fi
    local archive="${MODELS_DIRECTORY}/${NEMOTRON_NAME}.tar.bz2"
    if [ "$(df -Pk "$MODELS_DIRECTORY" | awk 'NR==2 {print $4}')" -lt "$NEMOTRON_KILOBYTES_NEEDED" ]; then
        problem "Nemotron needs about 1.5GB free in ${MODELS_DIRECTORY}, and there's less. Free some up - or make models/ a link to somewhere with room - and run this again."
        exit 1
    fi
    if ! wget -q "$NEMOTRON_URL" -O "${archive}.part"; then
        rm -f "${archive}.part"
        problem "Couldn't download Nemotron - check the internet connection, and run this again."
        exit 1
    fi
    check_sha256 "${archive}.part" "$NEMOTRON_SHA256"
    tar -xjf "${archive}.part" -C "$MODELS_DIRECTORY"
    rm -f "${archive}.part"
    done_ "Nemotron (${NEMOTRON_NAME})"
}

# Downloads whatever models aren't there yet. The brain needs Piper with its voice and
# Nemotron, and both roles openWakeWord's own - the satellite for the wake word, the
# brain for Silero's voice activity check. Run by update.sh too, for any new ones.
# models/ may be a link to somewhere with more room, which all of this follows.
download_models() {
    step "Models"
    mkdir -p "$MODELS_DIRECTORY"
    if [ "$ROLE" = "brain" ]; then
        download_voice
        download_nemotron
    fi
    # Now, rather than left to the first start, which would need the internet.
    (cd "$PROJECT_DIRECTORY" && "$PYTHON" - <<'EOF'
from openwakeword.utils import download_models

download_models()
EOF
    )
    done_ "openWakeWord models are in place"
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

# Writes the service from its template, with this install's paths filled in - the
# satellite's as a user service, the brain's as a system one. Run by update.sh too, so
# a changed service file takes effect.
install_service() {
    step "Starting at boot"
    if [ "$ROLE" = "brain" ]; then
        # Root's: a system service, which runs as this user. It's passed the home folder
        # too, as the only places it may write besides its own are its settings and data.
        sed -e "s|__PROJECT_DIRECTORY__|${PROJECT_DIRECTORY}|g" -e "s|__USER__|${SERVICE_USER}|g" \
            -e "s|__HOME__|${SERVICE_HOME}|g" "$BRAIN_SERVICE_TEMPLATE" | root_do tee "$BRAIN_SERVICE_FILE" > /dev/null
        # Both folders must be there for the service's sandbox to start.
        as_user mkdir -p -m 700 "$CONFIG_DIRECTORY" "${NOVA_DATA:-${SERVICE_HOME}/.local/share/nova}"
        root_do systemctl daemon-reload
        root_do systemctl enable -q "$BRAIN_SERVICE_NAME"
        done_ "Installed as the ${BRAIN_SERVICE_NAME} service"
        return
    fi
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
            --brain) ROLE="brain" ;;
            --satellite) ROLE="satellite" ;;
            *) echo "Unknown option: ${argument} (see the top of setup.sh)" >&2; exit 1 ;;
        esac
    done

    decide_role
    echo "Installing Nova's ${ROLE} in ${PROJECT_DIRECTORY}"
    check_system
    install_system_packages
    run_steps install_python_packages save_role move_old_files write_settings write_secrets download_models
    if [ "$ROLE" = "satellite" ]; then
        find_wake_word_model
        write_asound_conf
        install_raspotify
    fi
    if [ "$WITH_SERVICE" = true ]; then
        install_service
        if [ "$ROLE" = "brain" ]; then
            root_do systemctl restart "$BRAIN_SERVICE_NAME"
        else
            systemctl --user restart "$SERVICE_NAME"
        fi
    else
        step "Starting at boot"
        skipped "Left out (--no-service) - run it by hand with: $([ "$(id -u)" = 0 ] && [ "$SERVICE_USER" != "root" ] && echo "runuser -u ${SERVICE_USER} -- ")${PYTHON} ${PROJECT_DIRECTORY}/$([ "$ROLE" = "brain" ] && echo backend.py || echo satellite.py)"
    fi

    # Whoever reads these may be root, whose commands would otherwise write the brain's
    # passwords and tokens under /root, where its service never looks.
    local as_user_hint=""
    if [ "$(id -u)" = 0 ] && [ "$SERVICE_USER" != "root" ]; then
        as_user_hint="runuser -u ${SERVICE_USER} -- "
    fi
    echo ""
    echo "Done. Settings: ${SETTINGS_FILE} · keys: ${SECRETS_FILE}"
    if [ "$ROLE" = "brain" ]; then
        echo "Let a satellite in:         cd ${PROJECT_DIRECTORY} && ${as_user_hint}venv/bin/python backend.py add-satellite NAME"
        echo "For Spotify, sign in once:  cd ${PROJECT_DIRECTORY} && ${as_user_hint}venv/bin/python -m voice_assistant.spotify_controller"
        echo "Watch it working:           journalctl -u ${BRAIN_SERVICE_NAME} -f"
    else
        echo "Watch it working:           journalctl --user-unit ${SERVICE_NAME} -f"
    fi
    echo "Later, to update:           bash ${PROJECT_DIRECTORY}/update.sh"
}

# Run, unless update.sh or uninstall.sh is only borrowing the steps above.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
