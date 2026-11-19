from midichlorian.hosts import HOSTS, printer_hold_applies


def test_registry_names():
    assert set(HOSTS) == {"cal", "mace", "rey", "cere"}
    assert HOSTS["rey"].ssh == "rey.local"
    assert HOSTS["cere"].wrapper == "scripts/cere-ansible.sh"
    assert HOSTS["cere"].linux is False and HOSTS["cere"].docker is False
    assert [h for h, s in HOSTS.items() if s.ro_root] == ["cal", "mace"]


def test_printer_hold():
    assert printer_hold_applies("rey", None) is True
    assert printer_hold_applies("rey", ["x"]) is True
    assert printer_hold_applies("cal", None) is False
    assert printer_hold_applies("cal", ["x"]) is False
    assert printer_hold_applies("mace", None) is False
    assert printer_hold_applies("cere", None) is False


def test_controller_docker_hosts():
    # their playbooks build images on the runner (Rey's images, Cal's host-broker),
    # so the runner's Docker must be up
    assert {h for h, s in HOSTS.items() if s.controller_docker} == {"cal", "rey"}
