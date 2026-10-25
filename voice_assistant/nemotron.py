"""
Nemotron Module.

NVIDIA's Nemotron Speech Streaming, through sherpa-onnx, for Nova's backend on PVE2
- see docs/decisions/0004. It's from the same family as Parakeet, but built to work
on speech as it's heard: on recordings of the user's own voice, it got 5.1% of words
wrong where Moonshine got 16%, and had the words 0.29s after the recording stopped,
where Parakeet - which only starts then - took 0.58s.

The model isn't downloaded by Nova itself but by setup.sh --brain (and update.sh),
which checks and unpacks sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25.tar.bz2,
from the asr-models release of k2-fsa/sherpa-onnx on GitHub, into the models folder.
It's under NVIDIA's Open Model License.
"""

import numpy as np

RATE = 16000

# Seconds of silence it's given before each recording, and after it once it's over.
# Measured on the user's recordings: without the half second before, "Pause" came
# back as nothing at all, and it needed 1.2s after - rather than the 0.66s
# sherpa-onnx's own examples give - to finish "what time is it".
LEAD_IN = 0.5
TAIL = 1.2


class Nemotron:
    """The model, and what it heard in each recording given to it."""

    def __init__(self, folder: str, threads: int = 4):
        """
        Args:
            folder: Where the model was unpacked, with tokens.txt and the int8
                encoder, decoder and joiner in it.
            threads: How many processor threads it works with.
        """
        # Imported here rather than at the top, so the rest of Nova never needs it.
        import sherpa_onnx

        # It reads the rest of what it needs, like how many mel bands it hears in,
        # from the model itself.
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=f"{folder}/tokens.txt", encoder=f"{folder}/encoder.int8.onnx",
            decoder=f"{folder}/decoder.int8.onnx", joiner=f"{folder}/joiner.int8.onnx",
            num_threads=threads, decoding_method="greedy_search")

    def start(self):
        """
        Starts hearing a recording.

        Returns:
            Its stream, for add and finish.
        """
        stream = self.recognizer.create_stream()
        stream.accept_waveform(RATE, np.zeros(int(LEAD_IN * RATE), dtype=np.float32))
        return stream

    def add(self, stream, audio: np.ndarray):
        """
        More of the recording, worked on straight away.

        Args:
            stream: From start.
            audio: float32 samples at 16kHz, carrying on from the last.
        """
        stream.accept_waveform(RATE, audio)
        self._work(stream)

    def finish(self, stream) -> str:
        """
        The words, now the recording is over.

        Args:
            stream: From start.
        """
        stream.accept_waveform(RATE, np.zeros(int(TAIL * RATE), dtype=np.float32))
        stream.input_finished()
        self._work(stream)
        return self.recognizer.get_result(stream)

    def _work(self, stream):
        """Works through as much of the recording as there's enough of."""
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)
