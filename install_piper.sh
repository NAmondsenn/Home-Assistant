#!/bin/bash
# Piper TTS Installation Script for Raspberry Pi 5 (ARM64)
# Run this on your Pi: bash install_piper.sh

set -e  # Exit on error

echo "=== Installing Piper TTS for Jarvis ==="
echo ""

# Create models directory if it doesn't exist
mkdir -p ~/models

# Download Piper binary for ARM64
echo "[1/3] Downloading Piper binary for ARM64..."
cd ~/models
wget -q --show-progress https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_aarch64.tar.gz

# Extract
echo "[2/3] Extracting binary..."
tar -xzf piper_linux_aarch64.tar.gz
rm piper_linux_aarch64.tar.gz

# Download en_GB-alan-medium voice model
echo "[3/3] Downloading en_GB-alan-medium voice model..."
wget -q --show-progress https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx
wget -q --show-progress https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json

echo ""
echo "✅ Installation complete!"
echo ""
echo "Piper binary location: ~/models/piper/piper"
echo "Voice model location: ~/models/en_GB-alan-medium.onnx"
echo ""
echo "Test it with:"
echo "  echo 'Hello, this is Jarvis speaking.' | ~/models/piper/piper --model ~/models/en_GB-alan-medium.onnx --output_file test.wav"
echo "  aplay test.wav"
