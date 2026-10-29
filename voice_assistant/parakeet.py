"""
Parakeet Module.

NVIDIA's Parakeet TDT 110m, through sherpa-onnx, for Nova's backend - a second, small
speech model, asked only when Nemotron hears nothing in a short recording of the user
talking (see speech_to_text.py). A lone "Pause." often comes back from Nemotron as
nothing; on the brain, of 54 recordings Nemotron lost, this heard 52 right in about
0.1s, where hearing them again with Nemotron got 48 in about a second - see
.superpowers/sdd/second-model-spike.md.

The model isn't downloaded by Nova itself but by setup.sh --brain (and update.sh),
which checks and unpacks sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8.tar.bz2,
from the asr-models release of k2-fsa/sherpa-onnx on GitHub, into the models folder.
"""

import os
import threading

import numpy as np

RATE = 16000

# What the unpacked model folder holds, which sherpa-onnx is given.
FILES = ("tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx")


class Parakeet:
    """The model, and what it hears in a whole recording given to it at once."""

    def __init__(self, path: str, threads: int = 4):
        """
        Args:
            path: Where the model was unpacked, with tokens.txt and the int8 encoder,
                decoder and joiner in it.
            threads: How many processor threads it works with.

        Raises:
            FileNotFoundError: One of the model's files isn't there.
            Exception: Whatever sherpa-onnx raises loading it.
        """
        # Checked first, since sherpa-onnx can stop the whole process over a missing
        # file rather than raise - and a brain without this model still hears.
        for name in FILES:
            if not os.path.isfile(os.path.join(path, name)):
                raise FileNotFoundError(f"{os.path.join(path, name)} isn't there")

        # Imported here rather than at the top, so the rest of Nova never needs it.
        import sherpa_onnx

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            tokens=f"{path}/tokens.txt", encoder=f"{path}/encoder.int8.onnx",
            decoder=f"{path}/decoder.int8.onnx", joiner=f"{path}/joiner.int8.onnx",
            num_threads=threads, decoding_method="greedy_search", model_type="nemo_transducer")
        # One recogniser serves every satellite, each decode with a stream of its own -
        # but sherpa-onnx doesn't promise its offline recogniser is safe to share between
        # threads, so decodes take turns. Each takes about 0.1s, and two satellites
        # hearing nothing at the same moment is rare.
        self._turn = threading.Lock()

    def transcribe(self, audio: np.ndarray) -> str:
        """
        The words in a whole recording.

        Args:
            audio: float32 samples at 16kHz - the speech, without the quiet it stopped
                after, which it doesn't need.

        Returns:
            What it heard, or '' for nothing.

        Raises:
            Exception: Whatever sherpa-onnx raises.
        """
        with self._turn:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(RATE, np.asarray(audio, dtype=np.float32))
            self.recognizer.decode_stream(stream)
            return str(stream.result.text).strip()
