#!/usr/bin/env python3
"""
Speech-to-Text Comparison
Measures speech-to-text models on recordings of your own voice, made with
scripts/record_samples.py, so the choice of model rests on how well each one hears
you rather than on what's newest. For each model: how many words it got wrong, how
many recordings it heard exactly, whether the everyday commands among them would
still have been handled on the Pi, how long you'd wait, and how much processing it
took.

    venv/bin/python scripts/compare_speech_to_text.py ~/.local/share/nova/voice-samples moonshine moonshine-live

The models it can try, each followed by a colon and a model name if not the usual one:

    moonshine[:small-streaming]           Moonshine on each whole recording, once it's over.
    moonshine-live[:small-streaming]      Moonshine as the recording is made, as Nova does. Fed
                                          at the pace the microphone makes it, so this takes as
                                          long as the recordings do, and timed from when each ends.
    whisper[:base.en]                     faster-whisper on each whole recording.
    parakeet[:nemo-parakeet-tdt-0.6b-v2]  NVIDIA's Parakeet, through onnx-asr, which isn't one of
                                          Nova's own packages: pip install "onnx-asr[cpu,hub]"
    nemotron[:<model folder>]             NVIDIA's streaming Nemotron Speech, Parakeet's sibling built
                                          to work as it hears, through sherpa-onnx: pip install
                                          sherpa-onnx. Like moonshine-live, it's fed at the
                                          microphone's pace and timed from the end of each recording.
                                          Its model isn't downloaded for you: unpack
                                          sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25.tar.bz2,
                                          from the asr-models release of k2-fsa/sherpa-onnx on GitHub,
                                          into the project's models folder.

Moonshine and Whisper are set up from Nova's settings, keyterms and all - point
NOVA_CONFIG at a copy of another machine's settings folder to use its. Each model is
kept in the project's models folder, downloaded there the first time.

Words are compared without case or punctuation, and with numbers as digits, so "Set a
timer for ten minutes." and "set a timer for 10 minutes" are the same.
"""

import argparse
import os
import re
import resource
import statistics
import sys
import time
import wave
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

# Add the project root to the path so the voice_assistant package imports work
# no matter where the script is run from.
PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIRECTORY)

from voice_assistant.config import Config
from voice_assistant.intents import LocalIntents
from voice_assistant.storage import load_json

RATE = 16000

# How much of a recording the microphone hands over at a time - see
# Microphone.record_until_silence.
FRAME = 480

# Seconds of silence a streaming model is given before each recording, and after it
# once it's over. Measured with Nemotron on recordings of the user: without the half
# second before, "Pause" came back as nothing at all, and it needed 1.2s after -
# rather than the 0.66s sherpa-onnx's own examples give - to finish "what time is it".
STREAM_LEAD_IN = 0.5
STREAM_TAIL = 1.2

NUMBERS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
           "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
           "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
           "seventy": 70, "eighty": 80, "ninety": 90}
TENS = {"twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"}

# Nova only cancels "the timer" itself when there's exactly one, so there's one here.
INTENTS = LocalIntents(clock=SimpleNamespace(timer_count=lambda: 1))

# What a model is, once it's set up: what it heard in a recording, and how long that
# left the user waiting.
Transcribe = Callable[[np.ndarray], Tuple[str, float]]


def words(text: str) -> List[str]:
    """
    The words in what was said, or heard, for comparing one with the other.

    Args:
        text: e.g. "Set a timer for twenty five minutes."

    Returns:
        Lower case, without punctuation, and with numbers as digits - e.g.
        ["set", "a", "timer", "for", "25", "minutes"].
    """
    text = text.lower().replace("o'clock", " oclock").replace("%", " percent")
    # "10pm" is "10 pm", and "6:30" is "6 30".
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)
    text = re.sub(r"[^a-z0-9' ]+", " ", text).replace("'", "")
    found: List[str] = []
    after_tens = False
    for word in text.split():
        if word == "hundred":
            # "A hundred" and "one hundred" are both 100.
            if found and found[-1] in ("a", "1"):
                found.pop()
            found.append("100")
            after_tens = False
            continue
        number = int(word) if word.isdigit() else NUMBERS.get(word)
        if number is None:
            found.append(word)
        elif after_tens and 0 < number < 10:
            # "Twenty five" is 25.
            found[-1] = str(int(found[-1]) + number)
        else:
            found.append(str(number))
        after_tens = word in TENS
    return found


def word_errors(said: List[str], heard: List[str]) -> int:
    """
    How many words were heard wrong, missed out or added - the fewest changes which
    turn one list into the other.

    Args:
        said: The words actually said, from words().
        heard: The words heard, from words().
    """
    previous = list(range(len(heard) + 1))
    for i, wanted in enumerate(said, 1):
        current = [i]
        for j, got in enumerate(heard, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (wanted != got)))
        previous = current
    return previous[-1]


def handled_the_same(said: str, heard: str) -> Optional[bool]:
    """
    Whether what was heard would have had Nova do just what was meant, for the
    everyday commands it handles itself - see intents.py.

    Args:
        said: What was actually said.
        heard: What speech-to-text made of it.

    Returns:
        None if what was said isn't one of those - Claude would have had it whatever
        was heard.
    """
    meant = INTENTS.match(said)
    if meant is None:
        return None
    got = INTENTS.match(heard)
    return got is not None and (got["actions"], got["response"]) == (meant["actions"], meant["response"])


def read_wav(path: str) -> np.ndarray:
    """
    A recording, as float32 samples at 16kHz.

    Args:
        path: A 16-bit WAV file, like the ones record_samples.py makes.

    Raises:
        ValueError if it isn't 16-bit.
    """
    with wave.open(path) as file:
        if file.getsampwidth() != 2:
            raise ValueError(f"{path} isn't a 16-bit recording")
        rate, channels = file.getframerate(), file.getnchannels()
        audio = np.frombuffer(file.readframes(file.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != RATE:
        import soxr
        audio = soxr.resample(audio, rate, RATE).astype(np.float32)
    return audio


def load_samples(folder: str) -> List[Dict]:
    """
    The recordings in a folder made by record_samples.py, each with what was said.

    Args:
        folder: Holding samples.json and the recordings it lists.

    Returns:
        One dictionary per recording: its "file", the "text" said, the "condition"
        it was said in, and its "audio".
    """
    samples = []
    for sample in load_json(os.path.join(folder, "samples.json"), default=[]):
        if sample.get("text", "").strip():
            samples.append(dict(sample, audio=read_wav(os.path.join(folder, sample["file"]))))
    return samples


def _processing() -> float:
    """Seconds of processor time this process has used, on every thread."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _timed(transcribe: Callable[[], str]) -> Tuple[str, float]:
    """What a whole-recording model heard, and how long it took."""
    started = time.monotonic()
    text = transcribe()
    return text, time.monotonic() - started


def _live(stt, audio: np.ndarray) -> Tuple[str, float]:
    """
    What Moonshine heard, given the recording at the pace the microphone makes it,
    and how long it took once the recording was over.
    """
    listening = stt.listen()
    started = time.monotonic()
    for start in range(0, len(audio), FRAME):
        listening.add(audio[start:start + FRAME])
        time.sleep(max(0.0, started + (start + FRAME) / RATE - time.monotonic()))
    stopped = time.monotonic()
    text = listening.finish(audio)["text"]
    return text, time.monotonic() - stopped


def _streamed(recognizer, audio: np.ndarray) -> Tuple[str, float]:
    """
    What a sherpa-onnx streaming model heard, working on the recording as the
    microphone makes it, and how long it took once the recording was over.
    """
    stream = recognizer.create_stream()
    stream.accept_waveform(RATE, np.zeros(int(STREAM_LEAD_IN * RATE), dtype=np.float32))
    started = time.monotonic()
    for start in range(0, len(audio), FRAME):
        stream.accept_waveform(RATE, audio[start:start + FRAME])
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        time.sleep(max(0.0, started + (start + FRAME) / RATE - time.monotonic()))
    stopped = time.monotonic()
    # Silence after the end too, so the model has what it looks ahead to for the
    # last words.
    stream.accept_waveform(RATE, np.zeros(int(STREAM_TAIL * RATE), dtype=np.float32))
    stream.input_finished()
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)
    text = recognizer.get_result(stream)
    return text, time.monotonic() - stopped


def _moonshine(model: str):
    """Nova's own Moonshine, set up from Nova's settings, and the model it's using."""
    from voice_assistant.speech_to_text import MoonshineSpeechToText

    settings = Config().section("moonshine")
    model = model or settings.get("model", "small-streaming")
    return model, MoonshineSpeechToText(
        model=model, download_root=os.path.join(PROJECT_DIRECTORY, settings.get("download_root", "models/moonshine")),
        keyterms=settings.get("keyterms"))


def make_model(choice: str) -> Tuple[str, Transcribe]:
    """
    Sets up one of the models to compare.

    Args:
        choice: e.g. "moonshine", or "parakeet:nemo-parakeet-tdt-0.6b-v3" - see the top
            of this file.

    Returns:
        What to call it, and the function which transcribes a recording with it.

    Raises:
        ValueError if it isn't one of them.
    """
    kind, _, model = choice.partition(":")
    if kind == "moonshine":
        model, stt = _moonshine(model)
        return f"Moonshine {model}", lambda audio: _timed(lambda: stt.transcribe(audio)["text"])
    if kind == "moonshine-live":
        model, stt = _moonshine(model)
        return f"Moonshine {model}, live", lambda audio: _live(stt, audio)
    if kind == "whisper":
        from voice_assistant.speech_to_text import SpeechToText

        settings = Config().section("whisper")
        model = model or settings.get("model", "base.en")
        stt = SpeechToText(model_size=model, compute_type=settings.get("compute_type", "int8"),
                           initial_prompt=settings.get("initial_prompt"), beam_size=settings.get("beam_size", 1),
                           download_root=os.path.join(PROJECT_DIRECTORY,
                                                      settings.get("download_root", "models/whisper")))
        return f"Whisper {model}", lambda audio: _timed(lambda: stt.transcribe(audio)["text"])
    if kind == "parakeet":
        import onnx_asr

        name = model or "nemo-parakeet-tdt-0.6b-v2"
        parakeet = onnx_asr.load_model(name, os.path.join(PROJECT_DIRECTORY, "models", name), quantization="int8")
        return f"Parakeet {name}", lambda audio: _timed(lambda: parakeet.recognize(audio, sample_rate=RATE))
    if kind == "nemotron":
        import sherpa_onnx

        name = model or "sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25"
        folder = os.path.join(PROJECT_DIRECTORY, "models", name)
        # It reads the rest of what it needs, like how many mel bands it hears in,
        # from the model itself.
        recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=os.path.join(folder, "tokens.txt"), encoder=os.path.join(folder, "encoder.int8.onnx"),
            decoder=os.path.join(folder, "decoder.int8.onnx"), joiner=os.path.join(folder, "joiner.int8.onnx"),
            num_threads=4, decoding_method="greedy_search")
        chunks = re.search(r"-(\d+ms)-", name)
        label = f"Nemotron streaming, {chunks[1]} chunks" if chunks else f"Nemotron {name}"
        return label, lambda audio: _streamed(recognizer, audio)
    raise ValueError(f"Not a model this can try: '{choice}' - see the top of {os.path.basename(__file__)}")


def measure(transcribe: Transcribe, samples: List[Dict]) -> List[Dict]:
    """
    Transcribes every recording with one model.

    Args:
        transcribe: From make_model.
        samples: From load_samples.

    Returns:
        For each recording: what was "said", what was "heard", the "condition", how
        long it left the user "waiting", and the "processing" it took in seconds of
        processor time.
    """
    # The first transcription can load things the rest don't need to, so it isn't counted.
    transcribe(samples[0]["audio"])
    results = []
    for sample in samples:
        before = _processing()
        heard, waiting = transcribe(sample["audio"])
        results.append({"said": sample["text"], "heard": heard, "condition": sample.get("condition", ""),
                        "waiting": waiting, "processing": _processing() - before,
                        "seconds": len(sample["audio"]) / RATE})
    return results


def summarise(results: List[Dict]) -> Dict:
    """
    How one model did over every recording.

    Args:
        results: From measure.

    Returns:
        "wrong" and "total" words, words wrong in each "condition", how many were heard
        "exactly", the everyday "commands" handled the same out of how many there
        were, the "typical" and "worst" waits, the "processing" for each second of
        recording, and the "mistakes" as (said, heard) pairs.
    """
    wrong = total = exactly = handled = commands = 0
    conditions: Dict[str, List[int]] = {}
    mistakes = []
    for result in results:
        said, heard = words(result["said"]), words(result["heard"])
        errors = word_errors(said, heard)
        wrong, total = wrong + errors, total + len(said)
        tally = conditions.setdefault(result["condition"], [0, 0])
        tally[0], tally[1] = tally[0] + errors, tally[1] + len(said)
        exactly += errors == 0
        if errors:
            mistakes.append((result["said"], result["heard"]))
        same = handled_the_same(result["said"], result["heard"])
        if same is not None:
            commands += 1
            handled += same
    waits = [result["waiting"] for result in results]
    return {"wrong": wrong, "total": total, "conditions": conditions, "exactly": exactly,
            "handled": handled, "commands": commands, "typical": statistics.median(waits), "worst": max(waits),
            "processing": sum(r["processing"] for r in results) / sum(r["seconds"] for r in results),
            "mistakes": mistakes}


def report(label: str, summary: Dict, recordings: int, live: bool) -> List[str]:
    """
    One model's results, as lines to print.

    Args:
        label: What the model's called, from make_model.
        summary: From summarise.
        recordings: How many there were.
        live: Whether its waits are only from the end of each recording.
    """
    def percent(wrong, total):
        return f"{100 * wrong / total:.1f}%" if total else "-"

    lines = [label, f"  words wrong        {percent(summary['wrong'], summary['total'])} "
                    f"({summary['wrong']} of {summary['total']})"]
    if len(summary["conditions"]) > 1:
        lines[-1] += " - " + ", ".join(f"{condition or 'unmarked'} {percent(*tally)}"
                                       for condition, tally in summary["conditions"].items())
    lines.append(f"  heard exactly      {summary['exactly']} of {recordings}")
    if summary["commands"]:
        lines.append(f"  everyday commands  {summary['handled']} of {summary['commands']} still handled on the Pi")
    after = " after the recording stopped" if live else ""
    lines.append(f"  waiting            {summary['typical']:.2f}s typically{after}, {summary['worst']:.2f}s at worst")
    lines.append(f"  processing         {summary['processing']:.2f}s for each second of recording")
    for said, heard in summary["mistakes"]:
        lines.append(f'    "{said}" heard as "{heard}"')
    return lines


def main(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="Measures speech-to-text models on recordings of your voice.")
    parser.add_argument("folder", help="the recordings, made by record_samples.py")
    parser.add_argument("models", nargs="+", help="e.g. moonshine moonshine-live parakeet")
    arguments = parser.parse_args(argv)

    samples = load_samples(os.path.expanduser(arguments.folder))
    if not samples:
        print(f"No recordings with their words in {arguments.folder} - make some with record_samples.py.")
        return
    seconds = sum(len(sample["audio"]) for sample in samples) / RATE
    print(f"\n{len(samples)} recordings, {seconds:.0f}s in all, from {arguments.folder}\n")
    for choice in arguments.models:
        label, transcribe = make_model(choice)
        summary = summarise(measure(transcribe, samples))
        live = choice.startswith(("moonshine-live", "nemotron"))
        print("\n".join(report(label, summary, len(samples), live=live)) + "\n")


if __name__ == "__main__":
    main()
