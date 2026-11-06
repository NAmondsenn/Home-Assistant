"""
Setup Script Test
Checks setup.sh --new-password gives a set-up satellite the password the settings page
made for it - replacing only that line of its .env, keeping the file private, and
never showing the password - that setup sends the user to the page for one, and that
the brain's second speech model, Parakeet, is downloaded and checked as Nemotron is -
and that uninstall.sh leaves a brain alone, and takes raspotify's settings away with it.
"""

import os
import shutil
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


def test_a_satellite_installs_the_echo_cancellers_library(tmp_path):
    # Named rather than counted on the Pi already having it: without it "Hey Nova" isn't
    # heard over the music or Nova's own replies.
    result = run_setup(tmp_path, 'root_do() { echo "$*"; }; install_system_packages')

    assert result.returncode == 0, result.stderr
    installs = next(line for line in result.stdout.splitlines() if line.startswith("apt-get install"))
    assert "libspeexdsp1" in installs.split()


def ask_about_the_brain(tmp_path, stdin="\n", service_file=True, assume_yes=False, restart_fails=False):
    """
    Runs the step which asks whether to disable the brain's service, with a scratch file
    for the service and a root_do which runs systemctl's stand-in rather than sudo. With
    restart_fails the stand-in fails for every call, as for a service which is neither
    enabled nor running.

    Returns:
        The finished process, and what the stand-in systemctl was asked.
    """
    unit = tmp_path / "nova-backend.service"
    if service_file:
        unit.write_text("")
    result = run_setup(tmp_path, f'BRAIN_SERVICE_FILE="{unit}"; ASSUME_YES={str(assume_yes).lower()}; '
                                 'root_do() { "$@"; }; offer_to_disable_brain', stdin, restart_fails=restart_fails)
    return result, restarts(tmp_path)


def test_a_satellite_which_was_a_brain_is_asked_about_the_brains_service(tmp_path):
    # Enter takes the default, which is to leave it: stopping a brain is not something to
    # do on a bare Enter, and its data is kept either way.
    result, asked = ask_about_the_brain(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "nova-backend" in result.stdout
    assert not any("disable" in line for line in asked)
    # And says how to do it later.
    assert "systemctl disable --now nova-backend" in result.stdout


def test_a_satellite_which_was_a_brain_can_have_the_brains_service_disabled(tmp_path):
    result, asked = ask_about_the_brain(tmp_path, stdin="y\n")

    assert result.returncode == 0, result.stderr
    assert "disable -q --now nova-backend" in asked


def test_the_brains_service_is_left_alone_under_yes(tmp_path):
    result, asked = ask_about_the_brain(tmp_path, stdin="", assume_yes=True)

    assert result.returncode == 0, result.stderr
    assert not any("disable" in line for line in asked)
    assert "systemctl disable --now nova-backend" in result.stdout


def test_a_satellite_which_never_was_a_brain_isnt_asked(tmp_path):
    result, asked = ask_about_the_brain(tmp_path, service_file=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and asked == []


def test_a_brains_service_which_is_neither_enabled_nor_running_isnt_asked_about(tmp_path):
    # Left behind by an earlier change of role, and already dealt with.
    result, asked = ask_about_the_brain(tmp_path, restart_fails=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not any("disable" in line for line in asked)


def test_only_a_satellite_setup_asks_about_the_brains_service():
    main = SETUP.read_text().split("\nmain() {")[1]
    satellite = main[main.index('if [ "$ROLE" = "satellite" ]; then'):main.index('if [ "$WITH_SERVICE" = true ]')]

    assert main.count("offer_to_disable_brain") == 1
    assert "offer_to_disable_brain" in satellite


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


def download(tmp_path, model="parakeet", sha256=None, room=10 ** 9, wget_works=True):
    """
    Runs download_parakeet or download_nemotron into the test's own models folder, with
    a wget which copies in the stand-in archive and a df which says how much room there is.

    Args:
        tmp_path: The test's own folder.
        model: "parakeet" or "nemotron".
        sha256: The SHA-256 setup.sh expects - or None for the stand-in's own.
        room: Kilobytes free, as df says.
        wget_works: False for a wget which fails, as it does without the internet.

    Returns:
        The finished process, and the models folder.
    """
    archive, own = make_archive(tmp_path)
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    prefix = model.upper()
    copy = f'cp "{archive}" "$4"; echo "$2" >> "{tmp_path / "wget.log"}"' if wget_works else "return 4"
    return run_setup(tmp_path, f'MODELS_DIRECTORY="{models}"; {prefix}_NAME=parakeet; '
                               f'{prefix}_SHA256={sha256 or own}; '
                               f'wget() {{ {copy}; }}; '
                               f'df() {{ echo "Filesystem 1024-blocks Used Available Capacity Mounted"; '
                               f'echo "disk 0 0 {room} 0% /"; }}; download_{model}', role="brain"), models


def test_parakeet_is_downloaded_checked_and_unpacked(tmp_path):
    result, models = download(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (models / "parakeet" / "encoder.int8.onnx").read_text() == "model"
    assert sorted(path.name for path in models.iterdir()) == ["parakeet"]


@pytest.mark.parametrize("model", ["parakeet", "nemotron"])
def test_a_model_which_isnt_the_one_expected_is_never_unpacked(tmp_path, model):
    result, models = download(tmp_path, model, sha256="0" * 64)

    assert result.returncode != 0
    assert "SHA-256 doesn't match" in result.stderr
    assert list(models.iterdir()) == []


@pytest.mark.parametrize("model, stops", [("nemotron", True), ("parakeet", False)])
def test_without_room_only_nemotron_stops_setup(tmp_path, model, stops):
    # Nova can't hear without Nemotron - but without Parakeet only hears lone words
    # again by itself, so update.sh carries on and restarts it.
    result, models = download(tmp_path, model, room=1000)

    assert (result.returncode != 0) is stops
    assert "free in" in result.stderr
    assert not (tmp_path / "wget.log").exists()
    assert list(models.iterdir()) == []


@pytest.mark.parametrize("model, stops", [("nemotron", True), ("parakeet", False)])
def test_a_failed_download_only_stops_setup_for_nemotron(tmp_path, model, stops):
    result, models = download(tmp_path, model, wget_works=False)

    assert (result.returncode != 0) is stops
    assert "Couldn't download" in result.stderr
    assert list(models.iterdir()) == []


@pytest.mark.parametrize("model, stops", [("nemotron", True), ("parakeet", False)])
def test_a_model_cut_short_unpacking_is_never_taken_for_there(tmp_path, model, stops):
    # Unpacked to one side and moved into place once whole - so a broken archive, or
    # setup stopped part way, leaves no folder which a later run would skip. Only
    # Nemotron's stops setup: Parakeet's would otherwise make update.sh take back the
    # whole update over a model Nova works without.
    import hashlib
    import tarfile

    folder = tmp_path / "made" / "parakeet"
    folder.mkdir(parents=True)
    (folder / "encoder.int8.onnx").write_text("model")
    (folder / "joiner.int8.onnx").write_bytes(os.urandom(3_000_000))
    archive = tmp_path / "parakeet.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        tar.add(folder / "encoder.int8.onnx", arcname="./parakeet/encoder.int8.onnx")
        tar.add(folder / "joiner.int8.onnx", arcname="./parakeet/joiner.int8.onnx")
    # Cut off part way through the second file, after the first is whole.
    archive.write_bytes(archive.read_bytes()[:2_000_000])
    sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    models = tmp_path / "models"
    models.mkdir()
    prefix = model.upper()
    result = run_setup(tmp_path, f'MODELS_DIRECTORY="{models}"; {prefix}_NAME=parakeet; {prefix}_SHA256={sha256}; '
                                 f'wget() {{ cp "{archive}" "$4"; }}; '
                                 f'df() {{ echo "Filesystem 1024-blocks Used Available Capacity Mounted"; '
                                 f'echo "disk 0 0 {10 ** 9} 0% /"; }}; download_{model}', role="brain")

    assert (result.returncode != 0) is stops
    assert "Couldn't unpack" in result.stderr
    assert list(models.iterdir()) == []


def test_what_an_earlier_run_left_unpacking_is_cleared(tmp_path):
    (tmp_path / "models" / ".parakeet.unpacking" / "parakeet").mkdir(parents=True)
    (tmp_path / "models" / ".parakeet.unpacking" / "parakeet" / "half").write_text("")
    result, models = download(tmp_path)

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in models.iterdir()) == ["parakeet"]
    assert sorted(path.name for path in (models / "parakeet").iterdir()) == ["encoder.int8.onnx"]


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
    assert "PARAKEET_KILOBYTES_NEEDED=" in text


@pytest.mark.parametrize("role", ["brain", "satellite"])
def test_only_the_brain_downloads_parakeet(tmp_path, role):
    result = run_setup(tmp_path, f'MODELS_DIRECTORY="{tmp_path / "models"}"; PYTHON=true; download_voice() {{ :; }}; '
                                 'download_nemotron() { :; }; download_parakeet() { echo parakeet; }; download_models',
                       role=role)

    assert result.returncode == 0, result.stderr
    assert ("parakeet" in result.stdout.splitlines()) == (role == "brain")


# uninstall.sh

UNINSTALL = SETUP.parent / "uninstall.sh"


def run_uninstall(tmp_path, role, arguments=("--yes",), stdin="", change=None, service_home=None):
    """
    Runs a copy of uninstall.sh, in a project folder of its own with a venv and models to
    lose and a home of its own, against stand-ins for sudo, systemctl and apt-get which only
    note what they were asked - so nothing on the machine running the tests is touched.

    Args:
        tmp_path: The test's own folder.
        role: What the scratch install's role file says, "satellite" or "brain".
        arguments: What's given to uninstall.sh.
        stdin: What's typed in, for its questions.
        change: (old, new), swapped in the copy of the script - to point a path under /etc at
            the test's own folder.
        service_home: Runs it as root, as under sudo, with this as the home of the user who
            owns the clone, which the stand-in getent gives - and the scratch home above as root's.

    Returns:
        The finished process, the project folder, and the file that notes what the stand-ins
        were asked (which isn't there if they weren't).
    """
    project = tmp_path / "project"
    (project / "venv").mkdir(parents=True)
    (project / "venv" / "marker").write_text("")
    (project / "models").mkdir()
    shutil.copy(SETUP, project / "setup.sh")
    script = UNINSTALL.read_text()
    if change:
        assert change[0] in script
        script = script.replace(*change)
    (project / "uninstall.sh").write_text(script)

    home = tmp_path / "home"
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    (home / ".config" / "systemd" / "user" / "voice-assistant.service").write_text("")
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    (config / "role").write_text(role + "\n")

    log = tmp_path / "asked.log"
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for name in ["sudo", "systemctl", "apt-get"]:
        stand_in = bin_directory / name
        stand_in.write_text(f'#!/bin/bash\necho "{name} $*" >> "{log}"\n')
        stand_in.chmod(0o755)
    # Not root, whoever runs the tests - unless a home for the clone's owner says it is.
    stand_in = bin_directory / "id"
    stand_in.write_text(f'#!/bin/bash\n[ "$1" = -u ] && echo {0 if service_home else 1000} || exec /usr/bin/id "$@"\n')
    stand_in.chmod(0o755)
    if service_home:
        stand_in = bin_directory / "getent"
        stand_in.write_text(f'#!/bin/bash\necho "owner:x:1000:1000::{service_home}:/bin/bash"\n')
        stand_in.chmod(0o755)

    env = dict(os.environ, PATH=f"{bin_directory}:{os.environ['PATH']}", HOME=str(home), NOVA_CONFIG=str(config))
    for name in ("NOVA_SETUP_ROLE", "NOVA_DATA"):
        env.pop(name, None)
    result = subprocess.run(["bash", str(project / "uninstall.sh"), *arguments], input=stdin, env=env,
                            capture_output=True, text=True)
    return result, project, log


def test_uninstall_leaves_a_brain_alone(tmp_path):
    # It only knows a satellite: on a brain it would leave nova-backend enabled and look for
    # the settings in root's home - and, asked for its venv, delete it.
    result, project, log = run_uninstall(tmp_path, "brain")

    assert result.returncode == 1
    assert "This is a brain" in result.stderr
    assert "nova-backend" in result.stderr
    assert (project / "venv" / "marker").exists() and (project / "models").is_dir()
    assert not log.exists()


def test_uninstall_still_takes_a_satellite_off(tmp_path):
    result, project, log = run_uninstall(tmp_path, "satellite")

    assert result.returncode == 0, result.stderr
    assert not (project / "venv").exists()
    assert (project / "models").is_dir()
    assert "systemctl --user disable -q --now voice-assistant" in log.read_text()


def test_uninstall_purges_raspotify_so_its_settings_go_too(tmp_path):
    conf = tmp_path / "raspotify-conf"
    conf.write_text('LIBRESPOT_NAME="Voice-Assistant"\n')
    # Asked in order: the venv, the models, the settings and saved data, then raspotify.
    result, project, log = run_uninstall(tmp_path, "satellite", arguments=(), stdin="n\nn\nn\ny\nn\n",
                                         change=("/etc/raspotify/conf", str(conf)))

    assert result.returncode == 0, result.stderr
    asked = log.read_text().splitlines()
    # Removing it leaves /etc/raspotify/conf, which setup.sh then takes for raspotify
    # already being set up.
    assert "sudo apt-get purge -y -qq raspotify" in asked
    assert not any(line.startswith("sudo apt-get remove") for line in asked)
    assert (project / "venv" / "marker").exists()


def test_uninstall_deletes_the_saved_data_of_the_user_the_clone_belongs_to(tmp_path):
    # Under sudo HOME is root's, but the settings are looked for in the home of whoever owns
    # the clone - and so must be the saved data beside them, not root's empty ones.
    service_home = tmp_path / "service-home"
    data = service_home / ".local" / "share" / "nova"
    data.mkdir(parents=True)
    (data / "timers.json").write_text("{}")
    roots = tmp_path / "home" / ".local" / "share" / "nova"
    roots.mkdir(parents=True)
    (roots / "marker").write_text("")

    # Asked in order: the venv, the models, then the settings and saved data.
    result, project, log = run_uninstall(tmp_path, "satellite", arguments=(), stdin="n\nn\nyes\n",
                                         change=("/etc/raspotify/conf", str(tmp_path / "no-raspotify")),
                                         service_home=service_home)

    assert result.returncode == 0, result.stderr
    assert str(data) in result.stdout
    assert not data.exists()
    assert (roots / "marker").exists()
