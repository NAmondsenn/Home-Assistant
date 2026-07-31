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
echo "[1/9] Assistant configuration"
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

# 2. API keys, written to .env inside the project folder.
# The file is only written if it doesn't already exist, so re-running setup.sh
# never overwrites working credentials.
echo "[2/9] API keys"

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
echo "[3/9] Preparing Directory structure..."
mkdir -p "$MODELS_DIRECTORY" "$LOGS_DIRECTORY" "$TEMP_DIRECTORY" "$BIN_DIRECTORY"

# 4. Locate and install the wake word model.
echo "[4/9] Wake word model"
read -rp "Have you already copied your wake word .onnx model onto this machine? [y/N]: " HAS_MODEL

if [[ "$HAS_MODEL" =~ ^[Yy]$ ]]; then
    echo "Searching common locations for .onnx files..."
    mapfile -t FOUND_MODELS < <(find "$HOME" /tmp /mnt /media -maxdepth 3 -iname "*.onnx" 2>/dev/null)

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
echo "[5/9] Installing system dependencies..."

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
echo "[6/9] Setting up Python virtual environment..."

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
echo "[7/9] Detecting platform architecture & installing Piper..."
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
echo "[8/9] Downloading Piper voice model (${VOICE_MODEL})..."
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
echo ""
echo "[9/9] Start on boot (optional)"

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
echo "${VENV_DIRECTORY}/bin/python ${PROJECT_DIRECTORY}/voice_assistant/spotify_auth.py"
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
