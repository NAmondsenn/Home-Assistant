"""
Setup Script Test
Checks setup.sh --new-password gives a set-up satellite the password the settings page
made for it - replacing only that line of its .env, keeping the file private, and
never showing the password - that setup sends the user to the page for one, and that
the brain's second speech model, Parakeet, is downloaded and checked as Nemotron is.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

SETUP = Path(__file__).parent.parent / "setup.sh"
# Long enough, and only token_urlsafe's letters - as the page makes them.
PASSWORD = "Abc_def-0123456789xyz"


# Programs which would change the machine the tests run on. run_setup shadows each, so a
# step which reaches one - as main would, if --new-password stopped being caught - runs
# a stand-in which notes the call and fails, rather than a real install.
INSTALLERS = ["sudo", "apt-get", "pip", "pip3", "curl", "wget"]


def run_setup(tmp_path, command, stdin="", role="satellite", restart_fails=False, as_root=False,
              script=SETUP):
    """
    Runs one of setup.sh's steps, sourced so its main flow doesn't run, against a
    scratch config folder and home, with a systemctl which only notes what it was
    asked - so neither the real ~/.config/nova nor the real service is touched. Each
    of INSTALLERS is shadowed by a stand-in which notes its call in installers.log and
    fails, so a step which reaches one never installs anything for real.

    Args:
        tmp_path: The test's own folder.
        command: The step to run, e.g. "new_password".
        stdin: What's typed in.
        role: NOVA_SETUP_ROLE, "satellite" or "brain".
        restart_fails: Whether the stand-in systemctl fails, as a restart can.
        as_root: Whether a stand-in id says the user is root.
        script: The setup.sh to source, which a test may swap for a broken copy.

    Returns:
        The finished process, with its output as text.
    """
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(exist_ok=True)
    systemctl = bin_directory / "systemctl"
    systemctl.write_text(f'#!/bin/bash\necho "$*" >> "{tmp_path / "systemctl.log"}"\n'
                         + ("exit 1\n" if restart_fails else ""))
    systemctl.chmod(0o755)
    for name in INSTALLERS:
        stand_in = bin_directory / name
        stand_in.write_text(f'#!/bin/bash\necho "{name} $*" >> "{tmp_path / "installers.log"}"\nexit 1\n')
        stand_in.chmod(0o755)
    if as_root:
        # id is a program rather than a builtin, so one earlier on PATH is the one used.
        stand_in = bin_directory / "id"
        stand_in.write_text('#!/bin/bash\n[ "$1" = -u ] && echo 0 || exec /usr/bin/id "$@"\n')
        stand_in.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_directory}:{os.environ['PATH']}", HOME=str(tmp_path),
               NOVA_CONFIG=str(tmp_path / "config"), NOVA_SETUP_ROLE=role)
    return subprocess.run(["bash", "-c", f'source "{script}"; {command}'], input=stdin, env=env,
                          capture_output=True, text=True)


def write_env(tmp_path, text):
    """Makes the scratch config folder with a .env holding the text, and returns its path."""
    config = tmp_path / "config"
    config.mkdir(mode=0o700, exist_ok=True)
    env = config / ".env"
    env.write_text(text)
    env.chmod(0o600)
    return env


def restarts(tmp_path):
    """What the stand-in systemctl was asked, one call a line."""
    log = tmp_path / "systemctl.log"
    return log.read_text().splitlines() if log.exists() else []


def test_new_password_replaces_only_the_token(tmp_path):
    env = write_env(tmp_path, "# Its password, from the brain\nNOVA_BACKEND_TOKEN=old\nOTHER=kept as it is \n")

    result = run_setup(tmp_path, "new_password", PASSWORD + "\n")

    assert result.returncode == 0, result.stderr
    assert env.read_text() == f"# Its password, from the brain\nNOVA_BACKEND_TOKEN={PASSWORD}\nOTHER=kept as it is \n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert restarts(tmp_path) == ["--user restart voice-assistant"]
    assert "Saved, and the satellite restarted" in result.stdout
    # Nothing left beside it.
    assert sorted(path.name for path in env.parent.iterdir()) == [".env"]


def test_new_password_adds_a_token_line_if_there_isnt_one(tmp_path):
    env = write_env(tmp_path, "OTHER=1\n")

    result = run_setup(tmp_path, "new_password", PASSWORD + "\n")

    assert result.returncode == 0, result.stderr
    assert env.read_text() == f"OTHER=1\nNOVA_BACKEND_TOKEN={PASSWORD}\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert restarts(tmp_path) == ["--user restart voice-assistant"]


def test_new_password_refuses_a_short_one(tmp_path):
    original = "NOVA_BACKEND_TOKEN=old\nOTHER=1\n"
    env = write_env(tmp_path, original)

    # Too short, empty, with a quote inside, and with a space inside.
    for typed in ["Abc_def-0123456", "", "Abc_def'0123456789xyz", "Abc_def 0123456789xyz"]:
        result = run_setup(tmp_path, "new_password", typed + "\n")

        assert result.returncode == 1, typed
        assert env.read_text() == original
        assert restarts(tmp_path) == []
        assert sorted(path.name for path in env.parent.iterdir()) == [".env"]


def test_new_password_takes_one_of_exactly_16_characters(tmp_path):
    env = write_env(tmp_path, "NOVA_BACKEND_TOKEN=old\n")

    result = run_setup(tmp_path, "new_password", "Abc_def-01234567\n")

    assert result.returncode == 0, result.stderr
    assert env.read_text() == "NOVA_BACKEND_TOKEN=Abc_def-01234567\n"


def test_new_password_says_so_if_the_restart_fails(tmp_path):
    env = write_env(tmp_path, "NOVA_BACKEND_TOKEN=old\n")

    result = run_setup(tmp_path, "new_password", PASSWORD + "\n", restart_fails=True)

    assert result.returncode == 1
    assert "Saved, but the satellite didn't restart" in result.stderr
    # Still saved, so only the restart is left to do.
    assert env.read_text() == f"NOVA_BACKEND_TOKEN={PASSWORD}\n"
    assert restarts(tmp_path) == ["--user restart voice-assistant"]


def test_new_password_is_used_on_its_own(tmp_path):
    env = write_env(tmp_path, "NOVA_BACKEND_TOKEN=old\n")

    result = run_setup(tmp_path, "main --new-password --yes", PASSWORD + "\n")

    assert result.returncode == 1
    assert "used on its own" in result.stderr
    assert env.read_text() == "NOVA_BACKEND_TOKEN=old\n"
    assert restarts(tmp_path) == []
    # And on its own, it's the same as the step itself.
    alone = run_setup(tmp_path, "main --new-password", PASSWORD + "\n")
    assert alone.returncode == 0, alone.stderr
    assert env.read_text() == f"NOVA_BACKEND_TOKEN={PASSWORD}\n"


def test_a_broken_new_password_never_reaches_the_install(tmp_path):
    write_env(tmp_path, "NOVA_BACKEND_TOKEN=old\n")
    # A copy in which main no longer catches --new-password, so it falls through to the
    # install steps as an option nobody handled would.
    source = SETUP.read_text()
    broken = source.replace("--new-password) renewing=true ;;", "--new-password) renewing=false ;;")
    assert broken != source
    script = tmp_path / "broken-setup.sh"
    script.write_text(broken)

    result = run_setup(tmp_path, "main --new-password", PASSWORD + "\n", script=script)

    # It did fall through, and only the stand-ins answered: with none on PATH this would
    # be a real sudo or apt-get on the machine running the tests.
    log = tmp_path / "installers.log"
    assert log.exists(), result.stdout + result.stderr
    # Only the install's first call: its stand-in fails, and the install stops there.
    assert log.read_text().splitlines() == ["sudo apt-get update -qq"]
    assert result.returncode != 0
    assert restarts(tmp_path) == []


def test_new_password_isnt_run_as_root(tmp_path):
    env = write_env(tmp_path, "NOVA_BACKEND_TOKEN=old\n")

    result = run_setup(tmp_path, "new_password", PASSWORD + "\n", as_root=True)

    assert result.returncode == 1
    assert "without sudo" in result.stderr
    assert env.read_text() == "NOVA_BACKEND_TOKEN=old\n"
    assert restarts(tmp_path) == []


def test_new_password_trims_a_pasted_carriage_return(tmp_path):
    env = write_env(tmp_path, "NOVA_BACKEND_TOKEN=old\n")

    # As a phone's copy can bring them.
    result = run_setup(tmp_path, "new_password", f"  {PASSWORD} \r\n")

    assert result.returncode == 0, result.stderr
    assert env.read_text() == f"NOVA_BACKEND_TOKEN={PASSWORD}\n"


def test_setup_sends_the_user_to_the_page_to_add_a_satellite(tmp_path):
    # The satellite's Keys step, left blank.
    keys = run_setup(tmp_path, "write_secrets", "\n")
    # The brain's closing hints, with nothing done.
    closing = run_setup(tmp_path, "say_whats_next", role="brain")

    assert keys.returncode == 0, keys.stderr
    assert "settings page, under Satellites" in keys.stdout
    assert "add-satellite" in keys.stdout
    assert "settings page, under Satellites" in closing.stdout
    assert "add-satellite" in closing.stdout


def test_new_password_isnt_for_the_brain(tmp_path):
    env = write_env(tmp_path, "ANTHROPIC_API_KEY=a-key\n")

    result = run_setup(tmp_path, "new_password", PASSWORD + "\n", role="brain")

    assert result.returncode == 1
    assert "bash setup.sh" in result.stderr
    assert env.read_text() == "ANTHROPIC_API_KEY=a-key\n"
    assert restarts(tmp_path) == []


def test_new_password_needs_a_set_up_satellite(tmp_path):
    result = run_setup(tmp_path, "new_password", PASSWORD + "\n")

    assert result.returncode == 1
    assert "bash setup.sh" in result.stderr
    assert not (tmp_path / "config" / ".env").exists()


def test_the_password_isnt_echoed(tmp_path):
    # This checks only what the script itself prints. Typed with no terminal, as here,
    # read never echoes, so whether -s is still on read can't be told from it.
    write_env(tmp_path, "NOVA_BACKEND_TOKEN=old\n")

    result = run_setup(tmp_path, "new_password", PASSWORD + "\n")

    assert result.returncode == 0, result.stderr
    assert PASSWORD not in result.stdout + result.stderr
    # Nor given to systemctl, where ps would show it.
    assert all(PASSWORD not in line for line in restarts(tmp_path))


# The brain's second speech model, Parakeet - downloaded, checked, unpacked and the
# archive removed, as Nemotron is.

def make_archive(tmp_path):
    """A stand-in for Parakeet's release archive, with its folder inside, and its SHA-256."""
    import hashlib
    import tarfile

    folder = tmp_path / "made" / "parakeet"
    folder.mkdir(parents=True)
    (folder / "encoder.int8.onnx").write_text("model")
    archive = tmp_path / "parakeet.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        tar.add(folder, arcname="./parakeet")
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def download_parakeet(tmp_path, sha256=None):
    """
    Runs download_parakeet into the test's own models folder, with a wget which copies
    in the stand-in archive.

    Args:
        tmp_path: The test's own folder.
        sha256: The SHA-256 setup.sh expects - or None for the stand-in's own.

    Returns:
        The finished process, and the models folder.
    """
    archive, own = make_archive(tmp_path)
    models = tmp_path / "models"
    models.mkdir()
    return run_setup(tmp_path, f'MODELS_DIRECTORY="{models}"; PARAKEET_NAME=parakeet; '
                               f'PARAKEET_SHA256={sha256 or own}; '
                               f'wget() {{ cp "{archive}" "$4"; }}; download_parakeet', role="brain"), models


def test_parakeet_is_downloaded_checked_and_unpacked(tmp_path):
    result, models = download_parakeet(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (models / "parakeet" / "encoder.int8.onnx").read_text() == "model"
    assert sorted(path.name for path in models.iterdir()) == ["parakeet"]


def test_a_parakeet_which_isnt_the_one_expected_is_never_unpacked(tmp_path):
    result, models = download_parakeet(tmp_path, "0" * 64)

    assert result.returncode != 0
    assert "SHA-256 doesn't match" in result.stderr
    assert list(models.iterdir()) == []


def test_parakeet_already_there_isnt_downloaded_again(tmp_path):
    models = tmp_path / "models"
    (models / "parakeet").mkdir(parents=True)
    (models / "parakeet" / "encoder.int8.onnx").write_text("model")
    result = run_setup(tmp_path, f'MODELS_DIRECTORY="{models}"; PARAKEET_NAME=parakeet; download_parakeet',
                       role="brain")

    assert result.returncode == 0
    assert not (tmp_path / "installers.log").exists()


def test_parakeets_pinned_release_is_the_one_measured():
    # The 110m model, which the trial on the brain found right where Nemotron heard
    # nothing - see .superpowers/sdd/second-model-spike.md.
    text = SETUP.read_text()
    assert 'PARAKEET_NAME="sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8"' in text
    assert 'PARAKEET_SHA256="f628312e9fdf8686374cb01a69425c41732529d540860311f16f37cbc32cfe9b"' in text


@pytest.mark.parametrize("role", ["brain", "satellite"])
def test_only_the_brain_downloads_parakeet(tmp_path, role):
    result = run_setup(tmp_path, f'MODELS_DIRECTORY="{tmp_path / "models"}"; PYTHON=true; download_voice() {{ :; }}; '
                                 'download_nemotron() { :; }; download_parakeet() { echo parakeet; }; download_models',
                       role=role)

    assert result.returncode == 0, result.stderr
    assert ("parakeet" in result.stdout.splitlines()) == (role == "brain")
