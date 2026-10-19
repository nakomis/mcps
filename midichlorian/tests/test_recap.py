from pathlib import Path

from midichlorian.recap import Recap, parse

FIX = Path(__file__).parent / "fixtures"


def read(name):
    return (FIX / name).read_text()


def test_unreachable_run():
    r = parse(read("luke-unreachable.log"))
    assert r == Recap(
        ok=271, changed=21, unreachable=1, failed=0, skipped=117,
        failing_task="Restore / to read-only — OS-layer",
    )


def test_clean_run_has_no_failing_task():
    r = parse(read("leia-clean.log"))
    assert (r.ok, r.changed, r.unreachable, r.failed, r.skipped) == (273, 24, 0, 0, 80)
    assert r.failing_task is None


def test_failed_run():
    r = parse(read("rey-failed.log"))
    assert r.failed == 1
    assert r.failing_task == "Create the plex mount point"


def test_no_recap_gives_none_counts():
    r = parse("TASK [a]\nfatal: [h]: FAILED! => {}\n")
    assert (r.ok, r.changed, r.unreachable, r.failed, r.skipped) == (None,) * 5
    assert r.failing_task == "a"


def test_ansi_codes_are_stripped():
    text = (
        "\x1b[0;32mTASK [one]\x1b[0m ****\n"
        "\x1b[0;31mfatal: [h]: FAILED! => {}\x1b[0m\n"
        "PLAY RECAP ****\n"
        "\x1b[0;31mh : ok=1  changed=2  unreachable=0  failed=1  skipped=3  rescued=0  ignored=0\x1b[0m\n"
    )
    r = parse(text)
    assert (r.ok, r.changed, r.failed, r.skipped) == (1, 2, 1, 3)
    assert r.failing_task == "one"


def test_several_host_lines_are_summed():
    text = (
        "PLAY RECAP ****\n"
        "a : ok=1  changed=1  unreachable=0  failed=0  skipped=1  rescued=0  ignored=0\n"
        "b : ok=2  changed=0  unreachable=1  failed=1  skipped=0  rescued=0  ignored=0\n"
    )
    r = parse(text)
    assert (r.ok, r.changed, r.unreachable, r.failed, r.skipped) == (3, 1, 1, 1, 1)


RECAP_CLEAN = (
    "PLAY RECAP ****\n"
    "h : ok=3  changed=0  unreachable=0  failed=0  skipped=0  rescued=0  ignored=1\n"
)
RECAP_FAILED = RECAP_CLEAN.replace("failed=0", "failed=1")
IGNORED = 'TASK [x]\nfatal: [h]: FAILED! => {"msg": "no"}\n...ignoring\n'


def test_ignored_fatal_is_not_a_failure():
    r = parse(IGNORED + RECAP_CLEAN)
    assert r.failed == 0
    assert r.failing_task is None


def test_ignored_fatal_then_real_failure_names_the_later_task():
    text = IGNORED + "\nTASK [y]\nfatal: [h]: FAILED! => {}\n\n" + RECAP_FAILED
    assert parse(text).failing_task == "y"


def test_handler_failure_names_the_handler():
    text = (
        "TASK [a] ***\nok: [h]\n\n"
        "RUNNING HANDLER [Restart foo] ***\nfatal: [h]: FAILED! => {}\n\n" + RECAP_FAILED
    )
    assert parse(text).failing_task == "Restart foo"


def test_no_failing_task_when_recap_shows_no_failures():
    text = "TASK [a]\nfatal: [h]: FAILED! => {}\n" + RECAP_CLEAN
    assert parse(text).failing_task is None
