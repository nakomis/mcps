import pytest

from alexa_mcp import login


def test_session_state_has_exactly_the_keys_the_lambda_reads():
    assert login.session_state("a@b.c", "amazon.co.uk", "RT", "MD", "UU") == {
        "email": "a@b.c", "url": "amazon.co.uk",
        "refresh_token": "RT", "mac_dms": "MD", "uuid": "UU"}


def test_session_state_refuses_missing_token():
    with pytest.raises(ValueError):
        login.session_state("a@b.c", "amazon.co.uk", "", None, "UU")


def test_speaker_names_filters_and_sorts():
    devices = [
        {"accountName": "Shed Echo", "capabilities": ["VOLUME_SETTING"]},
        {"accountName": "This Device", "capabilities": []},
        {"accountName": "Bedroom Echo", "capabilities": ["VOLUME_SETTING"]},
    ]
    assert login.speaker_names(devices) == ["Bedroom Echo", "Shed Echo"]
    assert login.speaker_names(None) == []
