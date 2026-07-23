"""
Audio Management Module.

This module handles real-time microphone recording, silence detection, audio playback,
WAV file saving, and sample rate conversion using PyAudio and Librosa.
"""

import wave
import logging
import pyaudio
import numpy as np
import librosa
from typing import Optional

# Logger setup.
logger = logging.getLogger(__name__)

class AudioManager:
    """
    Class for managing input and output audio devices, recording, playback, and sample rate conversion.
    This class uses PyAudio for input and output devices and Librosa for sample rate conversion.
    """
    def __init__(
        self,
        input_device: Optional[str] = None,
        output_device: Optional[str] = None,
        sample_rate: int = 16000,
        channels: int = 1,
        chunk_size: int = 1024
    ):
        """
        Initialise the audio manager and resolve I/O device indices.

        Args:
            input_device: Target recording device.
            output_device: Target playback device.
            sample_rate: Default sample rate in Hz (default: 16000).
            channels: Number of audio channels (default: 1 for mono).
            chunk_size: Number of frames per buffer read/write (default: 1024).
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_size = chunk_size
        self.pa = pyaudio.PyAudio()
        
        # Find I/O devices.
        self.input_device_index = self._find_device(input_device, is_input=True)
        self.output_device_index = self._find_device(output_device, is_input=False)

        # Logs device and audio information.
        logger.info(f"Audio initialised: {sample_rate}Hz, {channels}ch, chunk={chunk_size}")
        logger.info(f"Input device: {self.input_device_index}")
        logger.info(f"Output device: {self.output_device_index}")
        
    def _find_device(self, device_name: Optional[str], is_input: bool) -> Optional[int]:
        """
        Searches system hardware for a device matching the specified name.

        Args:
            device_name: Name substring to match, uses "default" if none provided.
            is_input: True if looking for an input device, False for output.

        Returns:
            Device index integer if found, or None to fall back to system default.
        """
        # Sets the device name to "default" if a dictionary is passed in as the argument.
        if isinstance(device_name, dict):
            device_name = "default"
        # If no device name or if device_name is "default", returns None to use the system default.
        if device_name is None or device_name == "default":
            return None

        # Iterates through available audio interfaces.
        for i in range(self.pa.get_device_count()):
            info = self.pa.get_device_info_by_index(i)
            channel_count = info['maxInputChannels'] if is_input else info['maxOutputChannels']

            if channel_count > 0 and device_name.lower() in info['name'].lower():
                device_type = 'input' if is_input else 'output'
                logger.info(f"Found {device_type} device: {info['name']} (Index {i})")
                return i

        # If no matching device found, log a warning and fall back to default.
        logger.warning(f"Device '{device_name}' not found, using default")
        return None
        
    def list_devices(self):
        """Prints all available audio devices."""
        print("\n=== Available Audio Devices ===")
        for i in range(self.pa.get_device_count()):
            info = self.pa.get_device_info_by_index(i)
            device_type = []
            if info['maxInputChannels'] > 0:
                device_type.append("INPUT")
            if info['maxOutputChannels'] > 0:
                device_type.append("OUTPUT")
            print(f"{i}: {info['name']} [{', '.join(device_type)}]")
        print("=" * 40 + "\n")

    def _bytes_to_float(self, raw_bytes: bytes) -> np.ndarray:
        """Helper method to convert raw int16 PCM bytes into normalized float32 array."""
        audio_int = np.frombuffer(raw_bytes, dtype=np.int16)
        # Normalise 16-bit signed integers to float32 range.
        return audio_int.astype(np.float32) / 32768.0

    def record(self, duration: float) -> np.ndarray:
        """
        Record audio from the selected input device for a fixed duration.

        Args:
            duration: Recording time in seconds.

        Returns:
           Numpy array of normalised float32 audio samples.
        """
        logger.info(f"Recording for {duration} seconds...")

        stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.sample_rate,
            input=True,
            input_device_index=self.input_device_index,
            frames_per_buffer=self.chunk_size
        )

        frames = []
        total_chunks = int(self.sample_rate / self.chunk_size * duration)

        try:
            for i in range(total_chunks):
                # exception_on_overflow=False prevents crashes if CPU lags during buffer reads.
                data = stream.read(self.chunk_size, exception_on_overflow=False)
                frames.append(data)
        finally:
            # Ensure the audio stream is properly closed even if interrupted by an error.
            stream.stop_stream()
            stream.close()

        # Join all recorded frames into a single numpy array.
        audio_float = self._bytes_to_float(b''.join(frames))
        # Logs the number of recorded samples.
        logger.info(f"Recorded {len(audio_float)} samples")
        # Return the normalised audio array.
        return audio_float

    def record_until_silence(
            self,
            timeout: float = 5.0,
            silence_threshold: float = 0.04,
            silence_duration: float = 2.0
    ) -> np.ndarray:
        """
        Record audio until silence is detected or the timeout is reached.

        Args:
            timeout: Maximum recording duration in seconds.
            silence_threshold: Threshold in which sound is considered silent.
            silence_duration: Duration of consecutive silence in seconds.

        Returns:
            Numpy array of normalised float32 audio samples.
        """
        logger.info("Recording until silence...")

        # Open the audio stream with the specified parameters.
        stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.sample_rate,
            input=True,
            input_device_index=self.input_device_index,
            frames_per_buffer=self.chunk_size
        )

        frames = []
        silence_chunks = 0
        silence_chunks_needed = int(silence_duration * self.sample_rate / self.chunk_size)
        max_chunks = int(timeout * self.sample_rate / self.chunk_size)

        # Read audio data until silence is detected or timeout is reached.
        try:
            for i in range(max_chunks):
                data = stream.read(self.chunk_size, exception_on_overflow=False)
                frames.append(data)

                # Calculate Root Mean Square (RMS) energy to measure audio volume level.
                audio_chunk = self._bytes_to_float(data)
                rms = np.sqrt(np.mean(audio_chunk ** 2))

                # Breaks recording if silence is detected.
                if rms < silence_threshold:
                    silence_chunks += 1
                    if silence_chunks >= silence_chunks_needed:
                        elapsed = i * self.chunk_size / self.sample_rate
                        logger.info(f"Silence detected after {elapsed:.1f}s")
                        break
                else:
                    silence_chunks = 0
        finally:
            # Safely stop the stream and close the audio device.
            stream.stop_stream()
            stream.close()

        # Join all recorded frames into a single numpy array.
        # This also logs the total number of recorded samples and returns the audio array.
        audio_float = self._bytes_to_float(b''.join(frames))
        logger.info(f"Recorded {len(audio_float)} samples")
        return audio_float

    def play(self, audio: np.ndarray, sample_rate: Optional[int] = None):
        """
        Plays audio through the target output device.

        Args:
            audio: Array of audio samples in float32 format.
            sample_rate: Override the sample rate in Hz if it differs from the default.
        """
        if sample_rate is None:
            sample_rate = self.sample_rate

        # Logs the audio details for debugging.
        logger.info(f"Playing audio: {len(audio)} samples at {sample_rate}Hz")

        # Converts float32 audio int16, clipping values to prevent distortion.
        audio_int = np.clip(audio * 32767, -32768, 32767).astype(np.int16)

        # Opens the audio stream with the specified parameters.
        stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=sample_rate,
            output=True,
            output_device_index=self.output_device_index,
            frames_per_buffer=self.chunk_size
        )

        # Plays the audio samples and handles any exceptions that may occur.
        try:
            stream.write(audio_int.tobytes())
        finally:
            stream.stop_stream()
            stream.close()

    def save_wav(self, audio: np.ndarray, filename: str, sample_rate: Optional[int] = None):
        """
        Exports audio to a WAV file on disk.

        Args:
            audio: Array of audio samples in float32 format.
            filename: Destination file path.
            sample_rate: Override the sample rate in Hz if it differs from the default.
        """
        if sample_rate is None:
            sample_rate = self.sample_rate

        # Converts a normalised float32 array to 16-bit signed integer values
        audio_int = np.clip(audio * 32767, -32768, 32767).astype(np.int16)

        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # 2 bytes = 16-bit precision
            wf.setframerate(sample_rate)
            wf.writeframes(audio_int.tobytes())

        # Logs the file path for confirmation.
        logger.info(f"Saved audio to {filename}")

    def resample(self, audio: np.ndarray, original_sample_rate: int, target_sample_rate: int) -> np.ndarray:
        """
        Resample an audio array from a source sample rate to a target sample rate.

        Args:
            audio: Array of audio samples.
            original_sample_rate: Source sample rate in Hz.
            target_sample_rate: Desired sample rate in Hz.

        Returns:
            Resampled numpy float32 array.
        """
        # Check if the sample rates are the same, if so, return the original audio array.
        if original_sample_rate == target_sample_rate:
            return audio

        # Logs the resampling operation for debugging and returns the resampled audio.
        logger.info(f"Resampling from {original_sample_rate}Hz to {target_sample_rate}Hz")
        return librosa.resample(audio, orig_sr=original_sample_rate, target_sr=target_sample_rate)

    def close(self):
        """Release underlying PyAudio resources and safely terminate the session."""
        self.pa.terminate()
        logger.info("Audio manager closed")

# Runs a test recording and saves it to disk if the script is run directly.
# This is useful for verifying the audio setup and recording functionality.
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== Audio Manager Test ===\n")

    audio_mgr = AudioManager()

    # Query and display available sound interfaces.
    audio_mgr.list_devices()

    # Run a short test recording and save to disk.
    print("Recording 3 seconds...")
    test_audio = audio_mgr.record(duration=3.0)

    audio_mgr.save_wav(test_audio, "test_recording.wav")
    print("Saved to test_recording.wav")

    # Clean up PyAudio resources and terminate the session.
    audio_mgr.close()
    print("\nTest complete!")
