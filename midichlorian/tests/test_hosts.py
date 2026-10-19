from midichlorian.hosts import HOSTS, printer_hold_applies


def test_registry_names():
    assert set(HOSTS) == {"luke", "leia", "cal", "mace", "rey", "cere"}
    assert HOSTS["luke"].ssh == "luke.local"
    assert HOSTS["cere"].wrapper == "scripts/cere-ansible.sh"
    assert HOSTS["cere"].linux is False and HOSTS["cere"].docker is False
    assert HOSTS["luke"].needs_backup_gate and not HOSTS["leia"].needs_backup_gate
    assert [h for h, s in HOSTS.items() if s.ro_root] == ["luke", "leia", "cal", "mace"]


def test_printer_hold():
    assert printer_hold_applies("rey", None) is True
    assert printer_hold_applies("leia", ["x"]) is True
    assert printer_hold_applies("luke", None) is True
    assert printer_hold_applies("luke", ["claude-chats"]) is False
    assert printer_hold_applies("luke", ["samba"]) is True
    assert printer_hold_applies("cal", None) is False
    assert printer_hold_applies("cere", None) is False


def test_controller_docker_hosts():
    # their playbooks build images on the runner (Luke's volatile-fs artefact,
    # Rey's images, Cal's and Leia's host-broker), so the runner's Docker must be up
    assert {h for h, s in HOSTS.items() if s.controller_docker} == {
        "luke",
        "leia",
        "cal",
        "rey",
    }
