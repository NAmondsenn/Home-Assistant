#!/bin/bash
# Master Setup Script for the Voice Assistant.
# Works on Linux (Debian, Ubuntu, RHEL/Fedora, Arch, openSUSE) - ARM64 & x86_64
# This script keeps all file self-contained inside the project folder.

set -euo pipefail  # Exit on error, unhandled variable, or pipe failure

# Paths & Variables:
# PROJECT_DIRECTORY resolves to wherever this script actually lives, so the
# project can be cloned anywhere rather than just assuming ~/voice_assistant.
PROJECT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_DIRECTORY="${PROJECT_DIRECTORY}/models"
LOGS_DIRECTORY="${PROJECT_DIRECTORY}/logs"
TEMP_DIRECTORY="${PROJECT_DIRECTORY}/temp_audio"
BIN_DIRECTORY="${PROJECT_DIRECTORY}/bin"
VENV_DIRECTORY="${PROJECT_DIRECTORY}/venv"
CONFIG_FILE="${PROJECT_DIRECTORY}/config.yaml"
ENV_FILE="${PROJECT_DIRECTORY}/.env"

# systemd user service, used to optionally start the assistant on boot.
SERVICE_NAME="voice-assistant"
SERVICE_TEMPLATE="${PROJECT_DIRECTORY}/${SERVICE_NAME}.service"
SERVICE_DIRECTORY="${HOME}/.config/systemd/user"
SERVICE_INSTALLED=false

PIPER_VERSION="2023.11.14-2"
VOICE_MODEL="en_GB-alan-medium"
VOICE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alan/medium"

# Cleans previous files before setup.
if [[ "${1:-}" == "--clean" ]]; then
    echo "Cleaning previous setup files..."
    rm -rf "$VENV_DIRECTORY" "$TEMP_DIRECTORY" "$BIN_DIRECTORY"
    echo "Clean complete. Starting fresh setup..."
fi

echo "==========================================="
echo "       Voice Assistant Installer"
echo "==========================================="
echo ""

if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: config.yaml not found in: ${PROJECT_DIRECTORY}." >&2
    echo "Run this script from inside a full clone of the repository." >&2
    exit 1
fi

# 1. Searches for the assistants name and wake word for config.yaml
echo "[1/11] Assistant configuration"
CURRENT_NAME=$(grep -m1 "^  name:" "$CONFIG_FILE" | sed -E 's/^  name: *"?([^"]*)"?/\1/')
CURRENT_WAKE=$(grep -m1 "^  wake_word:" "$CONFIG_FILE" | sed -E 's/^  wake_word: *"?([^"]*)"?/\1/')

read -rp "Assistant name [${CURRENT_NAME}]: " ASSISTANT_NAME
ASSISTANT_NAME="${ASSISTANT_NAME:-$CURRENT_NAME}"

echo "Note: this phrase is only what gets shown / spoken out in prompts."
echo "Actual wake word detection depends on the .onnx model installed on the device."
read -rp "Wake word phrase [${CURRENT_WAKE}]: " WAKE_WORD
WAKE_WORD="${WAKE_WORD:-$CURRENT_WAKE}"

# Strips characters that would break the YAML quoting or the sed replacements below.
ASSISTANT_NAME=$(printf '%s' "$ASSISTANT_NAME" | tr -d '"\\|&')
WAKE_WORD=$(printf '%s' "$WAKE_WORD" | tr -d '"\\|&')

sed -i "s|^  name: .*|  name: \"${ASSISTANT_NAME}\"|" "$CONFIG_FILE"
sed -i "s|^  wake_word: .*|  wake_word: \"${WAKE_WORD}\"|" "$CONFIG_FILE"
echo "Saved '${ASSISTANT_NAME}' / '${WAKE_WORD}' to config.yaml"
echo ""

WAKE_WORD_KEY=$(echo "$WAKE_WORD" | tr '[:upper:]' '[:lower:]' | tr -s ' ' '_')

# Sets the Spotify Connect speaker name to the name stored in config.yaml
CURRENT_DEVICE=$(grep -m1 "^  device_name:" "$CONFIG_FILE" | sed -E 's/^  device_name: *"?([^"]*)"?/\1/')
read -rp "Spotify Connect speaker name [${CURRENT_DEVICE}]: " SPOTIFY_DEVICE_NAME
SPOTIFY_DEVICE_NAME="${SPOTIFY_DEVICE_NAME:-$CURRENT_DEVICE}"
SPOTIFY_DEVICE_NAME=$(printf '%s' "$SPOTIFY_DEVICE_NAME" | tr -d '"\\|&')

sed -i "s|^  device_name: .*|  device_name: \"${SPOTIFY_DEVICE_NAME}\"|" "$CONFIG_FILE"
echo ""

# 2. API keys, written to .env inside the project folder.
# The file is only written if it doesn't already exist, so re-running setup.sh
# never overwrites working credentials.
echo "[2/11] API keys"

if [ -f "$ENV_FILE" ]; then
    echo ".env already exists, leaving it untouched."
    echo "Delete ${ENV_FILE} and re-run setup.sh if you need to re-enter your keys."
else
    echo "Your keys are written to ${ENV_FILE}, which is gitignored and never committed."
    echo "Leave any of these blank to skip - the assistant runs without Spotify,"
    echo "and you can add them to .env by hand later."
    echo ""

    read -rp "Anthropic API key: " ANTHROPIC_KEY
    read -rp "Spotify client ID: " SPOTIFY_ID
    read -rp "Spotify client secret: " SPOTIFY_SECRET
    read -rp "Spotify redirect URI [http://127.0.0.1:8888/callback]: " SPOTIFY_URI
    SPOTIFY_URI="${SPOTIFY_URI:-http://127.0.0.1:8888/callback}"

    # Written with a restrictive umask so the credentials aren't world-readable.
    (
        umask 077
        cat > "$ENV_FILE" <<EOF
ANTHROPIC_API_KEY=${ANTHROPIC_KEY}
SPOTIFY_CLIENT_ID=${SPOTIFY_ID}
SPOTIFY_CLIENT_SECRET=${SPOTIFY_SECRET}
SPOTIFY_REDIRECT_URI=${SPOTIFY_URI}
EOF
    )
    echo "Saved credentials to ${ENV_FILE}"
fi
echo ""

# 3. Prepare Directory structure, all inside the project folder.
echo "[3/11] Preparing Directory structure..."
mkdir -p "$MODELS_DIRECTORY" "$LOGS_DIRECTORY" "$TEMP_DIRECTORY" "$BIN_DIRECTORY"

# 4. Locate and install the wake word model.
echo "[4/11] Wake word model"
read -rp "Have you already copied your wake word .onnx model onto this machine? [y/N]: " HAS_MODEL

if [[ "$HAS_MODEL" =~ ^[Yy]$ ]]; then
    echo "Searching common locations for .onnx files..."
    # openWakeWord ships its own .onnx files, and Piper's voice models are .onnx too.
    # They are not the wake word model, so they're filtered out.
    mapfile -t FOUND_MODELS < <(find "$HOME" /tmp /mnt /media -maxdepth 3 -iname "*.onnx" 2>/dev/null \
        | grep -viE "melspectrogram|embedding_model|silero_vad|alexa|hey_mycroft|hey_jarvis|hey_rhasspy|timer_v|weather_v|piper|en_[A-Z]{2}-")

    if [ ${#FOUND_MODELS[@]} -eq 0 ]; then
        read -rp "None found automatically. Enter the full path to your model: " SELECTED_MODEL
    elif [ ${#FOUND_MODELS[@]} -eq 1 ]; then
        SELECTED_MODEL="${FOUND_MODELS[0]}"
        echo "Found: ${SELECTED_MODEL}"
    else
        echo "Multiple .onnx files found, pick one:"
        select choice in "${FOUND_MODELS[@]}"; do
            if [ -n "$choice" ]; then
                SELECTED_MODEL="$choice"
                break
            fi
        done
    fi

    if [ -f "$SELECTED_MODEL" ]; then
        cp "$SELECTED_MODEL" "${MODELS_DIRECTORY}/${WAKE_WORD_KEY}.onnx"
        echo "Copied wake word model to ${MODELS_DIRECTORY}/${WAKE_WORD_KEY}.onnx"
    else
        echo "Warning: '${SELECTED_MODEL}' not found. You'll need to copy it in manually." >&2
    fi
else
    echo "Skipping. Copy your wake word .onnx model in before running the assistant, e.g.:"
    echo "  scp ${WAKE_WORD_KEY}.onnx ${USER}@$(hostname):${MODELS_DIRECTORY}/${WAKE_WORD_KEY}.onnx"
fi
echo ""

# 5. Detect OS package manager & install hardware dependencies.
echo "[5/11] Installing system dependencies..."

if command -v apt-get &> /dev/null; then
    echo "Debian / Ubuntu / Pi OS detected (apt)..."
    sudo apt-get update
    sudo apt-get install -y \
        python3-pip \
        python3-venv \
        portaudio19-dev \
        ffmpeg \
        alsa-utils \
        wget \
        tar \
        git \
        build-essential

elif command -v dnf &> /dev/null; then
    echo "Fedora / RHEL detected (dnf)..."
    sudo dnf install -y \
        python3-pip \
        portaudio-devel \
        ffmpeg \
        alsa-utils \
        wget \
        tar \
        git \
        gcc \
        gcc-c++ \
        make

elif command -v pacman &> /dev/null; then
    echo "Arch Linux detected (pacman)..."
    sudo pacman -Sy --needed --noconfirm \
        python-pip \
        portaudio \
        ffmpeg \
        alsa-utils \
        wget \
        tar \
        git \
        base-devel

elif command -v zypper &> /dev/null; then
    echo "openSUSE detected (zypper)..."
    sudo zypper install -y \
        python3-pip \
        portaudio-devel \
        ffmpeg \
        alsa-utils \
        wget \
        tar \
        git \
        patterns-devel-base-devel_basis

else
    echo "Warning: Could not detect a supported package manager (apt, dnf, pacman, zypper)." >&2
    echo "Please ensure PortAudio, ffmpeg, alsa-utils, and build tools are installed manually." >&2
fi

# 6. Create Python Virtual Environment & Install requirements.
echo "[6/11] Setting up Python virtual environment..."

if [ ! -f "${PROJECT_DIRECTORY}/requirements.txt" ]; then
    echo "Error: requirements.txt not found in ${PROJECT_DIRECTORY}." >&2
    echo "Run setup.sh from the root of the cloned repository." >&2
    exit 1
fi

if [ ! -d "$VENV_DIRECTORY" ]; then
    python3 -m venv "$VENV_DIRECTORY"
fi

source "${VENV_DIRECTORY}/bin/activate"

echo "Installing Python packages from requirements.txt..."
if ! pip install --upgrade pip setuptools wheel; then
    echo "Error: Failed to upgrade pip/setuptools." >&2
    echo "Removing corrupted virtual environment..." >&2
    rm -rf "$VENV_DIRECTORY"
    exit 1
fi

if ! pip install -r "${PROJECT_DIRECTORY}/requirements.txt"; then
    echo "Error: Failed to install Python dependencies from requirements.txt." >&2
    echo "Check build errors above." >&2
    echo "Removing corrupted virtual environment..." >&2
    rm -rf "$VENV_DIRECTORY"
    exit 1
fi

# openwakeword is installed without its declared dependencies, because it pins
# tflite-runtime on Linux which has no wheels for Python 3.12+. The assistant
# uses openwakeword's ONNX runtime instead, and everything openwakeword actually
# needs (onnxruntime, scipy, scikit-learn, tqdm, requests) is already installed
# by requirements.txt above.
echo "Installing openwakeword (without tflite-runtime)..."
if ! pip install --no-deps "openwakeword>=0.6.0"; then
    echo "Error: Failed to install openwakeword." >&2
    exit 1
fi

# Downloads openwakeword's shared preprocessing models (melspectrogram /
# embedding), which it needs on first run alongside the wake word model itself.
echo "Downloading openwakeword preprocessing models..."
python3 - <<'EOF'
from openwakeword.utils import download_models
download_models()
EOF

# 7. Detect Architecture and Install Piper TTS Binary.
echo "[7/11] Detecting platform architecture & installing Piper..."
ARCH=$(uname -m)
cd "$MODELS_DIRECTORY"

if [ ! -f "piper/piper" ]; then
    if [ "$ARCH" = "aarch64" ]; then
        PIPER_ARCH="piper_linux_aarch64.tar.gz"
    elif [ "$ARCH" = "x86_64" ]; then
        PIPER_ARCH="piper_linux_x86_64.tar.gz"
    else
        echo "Error: Unsupported architecture $ARCH for pre-built Piper binaries." >&2
        exit 1
    fi

    echo "Downloading Piper for $ARCH..."
    if ! wget -q --show-progress "https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/${PIPER_ARCH}" -O "$PIPER_ARCH"; then
        echo "Error: Failed to download Piper. Check your internet connection and try again." >&2
        rm -f "$PIPER_ARCH"
        exit 1
    fi

    if ! tar -xzf "$PIPER_ARCH"; then
        echo "Error: Failed to extract the Piper archive (corrupted download)." >&2
        rm -f "$PIPER_ARCH"
        rm -rf piper/
        exit 1
    fi
    rm -f "$PIPER_ARCH"
else
    echo "Piper is already installed. Skipping."
fi

# Convenience symlink kept inside the project's own bin/ folder.
if [ ! -L "${BIN_DIRECTORY}/piper" ]; then
    ln -s "${MODELS_DIRECTORY}/piper/piper" "${BIN_DIRECTORY}/piper"
fi

# 8. Download Piper TTS Voice Model
echo "[8/11] Downloading Piper voice model (${VOICE_MODEL})..."
if [ ! -f "${MODELS_DIRECTORY}/${VOICE_MODEL}.onnx" ]; then
    if ! wget -q --show-progress "${VOICE_URL}/${VOICE_MODEL}.onnx" -O "${MODELS_DIRECTORY}/${VOICE_MODEL}.onnx"; then
        echo "Error: failed to download voice model. Check your internet connection and try again." >&2
        rm -f "${MODELS_DIRECTORY}/${VOICE_MODEL}.onnx"
        exit 1
    fi
fi

if [ ! -f "${MODELS_DIRECTORY}/${VOICE_MODEL}.onnx.json" ]; then
    if ! wget -q --show-progress "${VOICE_URL}/${VOICE_MODEL}.onnx.json" -O "${MODELS_DIRECTORY}/${VOICE_MODEL}.onnx.json"; then
        echo "Error: failed to download voice model config. Check your internet connection and try again." >&2
        rm -f "${MODELS_DIRECTORY}/${VOICE_MODEL}.onnx.json"
        exit 1
    fi
fi


# 9. Gives the user the option to run the assistant on startup.

# 9. Shared audio devices. Without this, whichever program opens the sound card
# first locks the others out: music playing means the assistant can't speak, and
# ALSA's own default output is usually HDMI rather than the connected speaker.
echo ""
echo "[9/11] Shared audio setup"

# Lists sound cards, skipping the Pi's built-in HDMI outputs.
PLAYBACK_CARDS=$(aplay -l 2>/dev/null | grep "^card" | grep -viE "vc4hdmi|bcm2835" \
    | sed -E 's/^card [0-9]+: ([^ ]+) .*/\1/' | sort -u)
CAPTURE_CARDS=$(arecord -l 2>/dev/null | grep "^card" | grep -viE "vc4hdmi|bcm2835" \
    | sed -E 's/^card [0-9]+: ([^ ]+) .*/\1/' | sort -u)

DEFAULT_PLAYBACK=$(echo "$PLAYBACK_CARDS" | head -1)
# Prefers a capture card that isn't also the speaker, since a headset's playback
# and capture halves share a name and the microphone is usually a separate device.
DEFAULT_CAPTURE=$(echo "$CAPTURE_CARDS" | grep -v "^${DEFAULT_PLAYBACK}$" | head -1)
DEFAULT_CAPTURE="${DEFAULT_CAPTURE:-$(echo "$CAPTURE_CARDS" | head -1)}"

if [ -z "$DEFAULT_PLAYBACK" ] || [ -z "$DEFAULT_CAPTURE" ]; then
    echo "Warning: couldn't find a USB speaker and microphone, skipping shared audio setup." >&2
    echo "Connect them and re-run setup.sh, or write /etc/asound.conf by hand." >&2
else
    echo "Detected speaker: ${DEFAULT_PLAYBACK}"
    echo "Detected microphone: ${DEFAULT_CAPTURE}"
    read -rp "Write /etc/asound.conf so audio can be shared? [Y/n]: " WRITE_ASOUND

    if [[ ! "$WRITE_ASOUND" =~ ^[Nn]$ ]]; then
        read -rp "Speaker card [${DEFAULT_PLAYBACK}]: " PLAYBACK_CARD
        PLAYBACK_CARD="${PLAYBACK_CARD:-$DEFAULT_PLAYBACK}"
        read -rp "Microphone card [${DEFAULT_CAPTURE}]: " CAPTURE_CARD
        CAPTURE_CARD="${CAPTURE_CARD:-$DEFAULT_CAPTURE}"

        # Keeps a backup, since this replaces a system-wide audio configuration.
        if [ -f /etc/asound.conf ]; then
            sudo cp /etc/asound.conf "/etc/asound.conf.backup.$(date +%Y%m%d%H%M%S)"
            echo "Existing /etc/asound.conf backed up."
        fi

        sudo tee /etc/asound.conf > /dev/null <<EOF

# dmix and dsnoop allow the programs to share the speaker / microphone at once.
# Without this, the system would break as the current program using an I / O device locks it.
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
        pcm "hw:CARD=${PLAYBACK_CARD},DEV=0"
        rate 48000
        format S16_LE
        channels 2
    }
}

pcm.shared_in {
    type dsnoop
    ipc_key 2049
    ipc_perm 0666
    slave {
        pcm "hw:CARD=${CAPTURE_CARD},DEV=0"
        rate 48000
        format S16_LE
        channels 1
    }
}

ctl.!default {
    type hw
    card ${PLAYBACK_CARD}
}
EOF
        echo "Wrote /etc/asound.conf (speaker: ${PLAYBACK_CARD}, microphone: ${CAPTURE_CARD})."
    else
        echo "Skipping. Note that music and speech may not be able to play at the same time."
    fi
fi
echo ""

echo "[10/11] Spotify Connect playback (optional)"

if ! command -v apt-get &> /dev/null; then
    echo "raspotify only ships .deb packages, skipping on this distribution."
    echo "Install librespot manually if you want local Spotify playback."
else
    echo "This installs raspotify, so '${SPOTIFY_DEVICE_NAME}' shows up as a speaker in Spotify."
    echo "Note: Spotify Connect requires a Spotify Premium account."
    read -rp "Install Spotify Connect playback? [y/N]: " INSTALL_RASPOTIFY

    if [[ "$INSTALL_RASPOTIFY" =~ ^[Yy]$ ]]; then
        if [ ! -f /usr/bin/librespot ]; then
            echo "Adding the raspotify repository..."
            curl -sSL https://dtcooper.github.io/raspotify/key.asc | sudo tee /usr/share/keyrings/raspotify_key.asc > /dev/null
            sudo chmod 644 /usr/share/keyrings/raspotify_key.asc
            echo 'deb [signed-by=/usr/share/keyrings/raspotify_key.asc] https://dtcooper.github.io/raspotify raspotify main' \
                | sudo tee /etc/apt/sources.list.d/raspotify.list > /dev/null
            sudo apt-get update
        fi

        if sudo apt-get install -y raspotify; then
            if [ -f /etc/raspotify/conf ]; then
                # Sets (or replaces) a key in /etc/raspotify/conf, whether it's
                # currently missing, commented out, or set to something else.
                set_raspotify_conf() {
                    local key="$1" value="$2"
                    if sudo grep -qE "^#?${key}=" /etc/raspotify/conf; then
                        sudo sed -i "s|^#\?${key}=.*|${key}=${value}|" /etc/raspotify/conf
                    else
                        echo "${key}=${value}" | sudo tee -a /etc/raspotify/conf > /dev/null
                    fi
                }

                # The device name must match config.yaml, otherwise the assistant
                # won't recognise its own speaker.
                set_raspotify_conf "LIBRESPOT_NAME" "\"${SPOTIFY_DEVICE_NAME}\""

                # librespot's default logarithmic volume curve makes the bottom half
                # of Spotify's volume slider near-silent; linear behaves as expected.
                set_raspotify_conf "LIBRESPOT_VOLUME_CTRL" "\"linear\""

                # Credential caching must stay on, or the device logs out on every
                # restart and playback silently stops working.
                sudo sed -i 's|^LIBRESPOT_DISABLE_CREDENTIAL_CACHE=|#LIBRESPOT_DISABLE_CREDENTIAL_CACHE=|' /etc/raspotify/conf

                # Uses the system default, which the shared audio step points at the
                # speaker via dmix. Going direct to the hardware instead would lock
                # the speaker exclusively and leave the assistant unable to speak
                # while music is playing.
                set_raspotify_conf "LIBRESPOT_DEVICE" "\"default\""

                # raspotify's packaged service sandbox doesn't grant /var/lib/raspotify,
                # where librespot keeps its Spotify login. Without this override the
                # cached credentials can't be used and the device stays logged out.
                sudo mkdir -p /etc/systemd/system/raspotify.service.d
                printf '[Service]\nStateDirectory=raspotify\n' \
                    | sudo tee /etc/systemd/system/raspotify.service.d/override.conf > /dev/null
                sudo systemctl daemon-reload

                # Discovery-only mode needs a phone/PC on the same network to hand the
                # device a session, which fails on many networks. Once the device has
                # its own cached login, direct login mode is far more reliable.
                if sudo test -f /var/lib/raspotify/credentials.json; then
                    set_raspotify_conf "LIBRESPOT_DISABLE_DISCOVERY" ""
                    echo "Spotify credentials found - using direct login mode."
                else
                    echo ""
                    echo "No Spotify login found yet. One-time setup after this script finishes:"
                    echo "  1. From your PC:  ssh -L 5588:localhost:5588 ${USER}@$(hostname)"
                    echo "  2. On this machine:  sudo systemctl stop raspotify"
                    echo "     sudo librespot -n \"${SPOTIFY_DEVICE_NAME}\" -j -K 5588 --system-cache /var/lib/raspotify"
                    echo "  3. Open the printed URL in your PC browser and approve."
                    echo "  4. Ctrl+C librespot, then re-run setup.sh (it will detect the"
                    echo "     login and switch to direct mode automatically)."
                    echo ""
                fi

                # librespot's connection to Spotify can drop without the process
                # exiting, leaving the device silently missing from the account
                # while systemd still reports it healthy. A nightly restart keeps
                # that from going unnoticed for days.
                if [ -f "${PROJECT_DIRECTORY}/raspotify-restart.timer" ]; then
                    sudo cp "${PROJECT_DIRECTORY}/raspotify-restart.service" \
                            "${PROJECT_DIRECTORY}/raspotify-restart.timer" \
                            /etc/systemd/system/
                    sudo systemctl daemon-reload
                    sudo systemctl enable --now raspotify-restart.timer
                    echo "Raspotify will restart nightly to keep its connection alive."
                fi

                sudo systemctl restart raspotify
                echo "raspotify configured as '${SPOTIFY_DEVICE_NAME}'."
            else
                echo "Warning: /etc/raspotify/conf not found, configure raspotify by hand." >&2
            fi
        else
            echo "Warning: raspotify install failed. The assistant will still run," >&2
            echo "but music will play on whichever device Spotify picks." >&2
        fi
    else
        echo "Skipping. Music will play on whichever Spotify device is active."
    fi
fi
echo ""

# 10. Optionally install the systemd user service so the assistant starts on boot.
echo "[11/11] Start on boot (optional)"

if ! command -v systemctl &> /dev/null; then
    echo "systemd not found on this machine, skipping the boot service."
elif [ ! -f "$SERVICE_TEMPLATE" ]; then
    echo "Warning: ${SERVICE_TEMPLATE} not found, skipping the boot service." >&2
else
    read -rp "Start the assistant automatically on boot? [y/N]: " INSTALL_SERVICE

    if [[ "$INSTALL_SERVICE" =~ ^[Yy]$ ]]; then
        mkdir -p "$SERVICE_DIRECTORY"

        # Fills the project's real paths into the unit file template.
        sed -e "s|__PROJECT_DIRECTORY__|${PROJECT_DIRECTORY}|g" \
            -e "s|__PYTHON__|${VENV_DIRECTORY}/bin/python|g" \
            "$SERVICE_TEMPLATE" > "${SERVICE_DIRECTORY}/${SERVICE_NAME}.service"

        systemctl --user daemon-reload
        systemctl --user enable "$SERVICE_NAME"

        # Lingering lets the user service start at boot without anyone logging in,
        # which is the whole point on a headless Pi.
        if sudo loginctl enable-linger "$USER"; then
            echo "Enabled lingering for ${USER}, so the service starts without a login."
        else
            echo "Warning: couldn't enable lingering. The service will only start once you log in." >&2
        fi

        echo "Service installed and enabled."
        echo "Start it now with: systemctl --user start ${SERVICE_NAME}"
        SERVICE_INSTALLED=true
    else
        echo "Skipping. You can install it later by re-running setup.sh."
    fi
fi

echo ""
echo " ========================================="
echo "      Setup Completed Successfully!"
echo " ========================================="
echo ""
echo "Voice Assistant location: ${PROJECT_DIRECTORY}"
echo ""
echo "To activate your Python environment manually:"
echo "source ${VENV_DIRECTORY}/bin/activate"
echo ""
echo "To run your voice assistant:"
echo "${VENV_DIRECTORY}/bin/python ${PROJECT_DIRECTORY}/main.py"
echo ""
echo "If you're using Spotify, authorise it once before the first run:"
echo "${VENV_DIRECTORY}/bin/python ${PROJECT_DIRECTORY}/voice_assistant/spotify_controller.py"
echo ""

if [ "$SERVICE_INSTALLED" = true ]; then
    echo "The assistant is set to start on boot. Useful commands:"
    echo "  systemctl --user start ${SERVICE_NAME}     # start it now"
    echo "  systemctl --user stop ${SERVICE_NAME}      # stop it"
    echo "  systemctl --user status ${SERVICE_NAME}    # check if it's running"
    echo "  systemctl --user disable ${SERVICE_NAME}   # stop starting on boot"
    echo "  journalctl --user -u ${SERVICE_NAME} -f    # follow its output"
    echo ""
    echo "Note: stop the service before running main.py by hand, or the two"
    echo "will fight over the microphone."
    echo ""
fi
