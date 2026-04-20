"""
Audio I/O Manager
Handles microphone capture, speaker playback, and audio resampling.
"""

import pyaudio
import numpy as np
import wave
import logging
from typing import Optional, Callable
import librosa

logger = logging.getLogger(__name__)


class AudioManager:
    """Manages audio input/output with proper device selection and routing."""
    
    def __init__(
        self,
        input_device: Optional[str] = None,
        output_device: Optional[str] = None,
        sample_rate: int = 16000,
        channels: int = 1,
        chunk_size: int = 1024
    ):
        """
        Initialize audio manager.
        
        Args:
            input_device: Name/index of input device (None = default)
            output_device: Name/index of output device (None = default)
            sample_rate: Sample rate in Hz (16000 for Whisper)
            channels: Number of audio channels (1 = mono)
            chunk_size: Frames per buffer
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_size = chunk_size
        self.pa = pyaudio.PyAudio()
        
        # Find devices
        self.input_device_index = self._find_device(input_device, is_input=True)
        self.output_device_index = self._find_device(output_device, is_input=False)
        
        logger.info(f"Audio initialized: {sample_rate}Hz, {channels}ch, chunk={chunk_size}")
        logger.info(f"Input device: {self.input_device_index}")
        logger.info(f"Output device: {self.output_device_index}")
        
    def _find_device(self, device_name: Optional[str], is_input: bool) -> Optional[int]:
        """
        Find audio device by name or use default.
         
        Args:
            device_name: Device name to search for (None = default)
            is_input: True for input device, False for output
            
        Returns:
            Device index or None for default
        """
        if device_name is None or device_name == "default":
            return None
            
        # Try to find by name
        for i in range(self.pa.get_device_count()):
            info = self.pa.get_device_info_by_index(i)
            if is_input and info['maxInputChannels'] > 0:
                if device_name.lower() in info['name'].lower():
                    logger.info(f"Found {'input' if is_input else 'output'} device: {info['name']}")
                    return i
            elif not is_input and info['maxOutputChannels'] > 0:
                if device_name.lower() in info['name'].lower():
                    logger.info(f"Found {'input' if is_input else 'output'} device: {info['name']}")
                    return i
                    
        logger.warning(f"Device '{device_name}' not found, using default")
        return None
        
    def list_devices(self):
        """Print all available audio devices."""
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
        
    def record(self, duration: float) -> np.ndarray:
        """
        Record audio for specified duration.
        
        Args:
            duration: Recording duration in seconds
            
        Returns:
            Audio data as numpy array (float32, -1 to 1)
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
        num_chunks = int(self.sample_rate / self.chunk_size * duration)
        
        for _ in range(num_chunks):
            data = stream.read(self.chunk_size, exception_on_overflow=False)
            frames.append(data)
            
        stream.stop_stream()
        stream.close()
        
        # Convert to numpy array
        audio_data = b''.join(frames)
        audio_array = np.frombuffer(audio_data, dtype=np.int16)
        
        # Normalize to float32 [-1, 1]
        audio_float = audio_array.astype(np.float32) / 32768.0
        
        logger.info(f"Recorded {len(audio_float)} samples")
        return audio_float
        
    def record_until_silence(
        self,
        timeout: float = 5.0,
        silence_threshold: float = 0.01,
        silence_duration: float = 2.0
    ) -> np.ndarray:
        """
        Record until silence is detected or timeout.
        
        Args:
            timeout: Maximum recording time in seconds
            silence_threshold: RMS threshold below which is considered silence
            silence_duration: Seconds of silence before stopping
            
        Returns:
            Audio data as numpy array
        """
        logger.info("Recording until silence...")
        
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
        
        for i in range(max_chunks):
            data = stream.read(self.chunk_size, exception_on_overflow=False)
            frames.append(data)
            
            # Check for silence
            audio_chunk = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            rms = np.sqrt(np.mean(audio_chunk**2))
            
            if rms < silence_threshold:
                silence_chunks += 1
                if silence_chunks >= silence_chunks_needed:
                    logger.info(f"Silence detected after {i * self.chunk_size / self.sample_rate:.1f}s")
                    break
            else:
                silence_chunks = 0
                
        stream.stop_stream()
        stream.close()
        
        # Convert to numpy array
        audio_data = b''.join(frames)
        audio_array = np.frombuffer(audio_data, dtype=np.int16)
        audio_float = audio_array.astype(np.float32) / 32768.0
        
        logger.info(f"Recorded {len(audio_float)} samples")
        return audio_float
        
    def play(self, audio: np.ndarray, sample_rate: Optional[int] = None):
        """
        Play audio through speakers.
        
        Args:
            audio: Audio data as numpy array (float32, -1 to 1)
            sample_rate: Sample rate of audio (None = use default)
        """
        if sample_rate is None:
            sample_rate = self.sample_rate
            
        logger.info(f"Playing audio: {len(audio)} samples at {sample_rate}Hz")
        
        # Convert to int16
        audio_int = (audio * 32767).astype(np.int16)
        
        stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=sample_rate,
            output=True,
            output_device_index=self.output_device_index,
            frames_per_buffer=self.chunk_size
        )
        
        stream.write(audio_int.tobytes())
        stream.stop_stream()
        stream.close()
        
    def save_wav(self, audio: np.ndarray, filename: str, sample_rate: Optional[int] = None):
        """
        Save audio to WAV file.
        
        Args:
            audio: Audio data as numpy array (float32)
            filename: Output filename
            sample_rate: Sample rate (None = use default)
        """
        if sample_rate is None:
            sample_rate = self.sample_rate
            
        # Convert to int16
        audio_int = (audio * 32767).astype(np.int16)
        
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(audio_int.tobytes())
            
        logger.info(f"Saved audio to {filename}")
        
    def resample(self, audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        """
        Resample audio to different sample rate.
        
        Args:
            audio: Audio data
            orig_sr: Original sample rate
            target_sr: Target sample rate
            
        Returns:
            Resampled audio
        """
        if orig_sr == target_sr:
            return audio
            
        logger.info(f"Resampling from {orig_sr}Hz to {target_sr}Hz")
        return librosa.resample(audio, orig_sr=orig_sr, target_sr=target_sr)
        
    def close(self):
        """Clean up audio resources."""
        self.pa.terminate()
        logger.info("Audio manager closed")


# Test function
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("=== Audio Manager Test ===\n")
    
    # Initialize
    audio_mgr = AudioManager()
    
    # List devices
    audio_mgr.list_devices()
    
    # Test recording
    print("Recording 3 seconds...")
    audio = audio_mgr.record(duration=3.0)
    
    # Save to file
    audio_mgr.save_wav(audio, "test_recording.wav")
    print("Saved to test_recording.wav")
    
    # Play it back (if you have speakers)
    # audio_mgr.play(audio)
    
    audio_mgr.close()
    print("\nTest complete!")
