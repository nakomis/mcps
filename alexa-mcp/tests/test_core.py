import json

import pytest

from alexa_announce import core


def dev(name, serial, dtype="A3S5BH2HU6VAYF", caps=("VOLUME_SETTING",), online=True):
    return {
        "accountName": name,
        "serialNumber": serial,
        "deviceType": dtype,
        "capabilities": list(caps),
        "online": online,
    }


KITCHEN = dev("Martin's Kitchen Echo Dot", "S1")
SHOW = dev("Martin's Echo Show", "S2", dtype="AWZZ5CVHX2CD")
BEDROOM = dev("Bedroom Echo", "S3", online=False)
APP = dev("This Device", "S4", caps=())  # no VOLUME_SETTING: not a speaker
DEVICES = [KITCHEN, SHOW, BEDROOM, APP]


# ── validate ──────────────────────────────────────────────────────────────────

def test_validate_returns_trimmed_text():
    assert core.validate({"action": "announce", "text": "  hi  ", "devices": ["a"]}) == (
        "announce", "hi", ["a"])


@pytest.mark.parametrize("event", [
    {"action": "shout", "text": "hi", "devices": ["a"]},
    {"action": "announce", "text": "   ", "devices": ["a"]},
    {"action": "announce", "text": None, "devices": ["a"]},
    {"action": "announce", "text": "x" * 1001, "devices": ["a"]},
    {"action": "announce", "text": "hi", "devices": []},
    {"action": "announce", "text": "hi"},
    {"action": "announce", "text": "hi", "devices": ["  "]},
])
def test_validate_rejects_bad_requests(event):
    with pytest.raises(core.RequestError) as e:
        core.validate(event)
    assert e.value.error == "bad_request"


def test_validate_accepts_exactly_max_text():
    assert core.validate({"action": "speak", "text": "x" * 1000, "devices": ["a"]})[1] == "x" * 1000


# ── resolve_devices ───────────────────────────────────────────────────────────

def test_resolve_exact_names_in_request_order():
    assert core.resolve_devices(["Bedroom Echo", "Martin's Echo Show"], DEVICES) == [BEDROOM, SHOW]


def test_resolve_ignores_case_and_curly_apostrophes():
    assert core.resolve_devices(["martin’s kitchen echo dot"], DEVICES) == [KITCHEN]


def test_resolve_deduplicates():
    assert core.resolve_devices(["Bedroom Echo", "bedroom echo"], DEVICES) == [BEDROOM]


def test_resolve_includes_offline_devices():
    assert core.resolve_devices(["Bedroom Echo"], DEVICES) == [BEDROOM]


def test_resolve_unknown_lists_valid_speakers_only():
    with pytest.raises(core.RequestError) as e:
        core.resolve_devices(["Bedroom Echo", "Kitchen", "This Device"], DEVICES)
    assert e.value.error == "unknown_devices"
    assert e.value.detail == {
        "unknown": ["Kitchen", "This Device"],
        "valid": ["Bedroom Echo", "Martin's Echo Show", "Martin's Kitchen Echo Dot"],
    }


# ── sequences ─────────────────────────────────────────────────────────────────

def test_announce_node_targets_every_device():
    node = core.announce_node("Dinner", [KITCHEN, SHOW], "CUST")
    assert node["type"] == "AlexaAnnouncement"
    p = node["operationPayload"]
    assert p["customerId"] == "CUST"
    assert p["expireAfter"] == "PT5S"
    assert p["deviceSerialNumber"] == "S1"
    assert p["content"] == [{
        "locale": "en-GB",
        "display": {"title": "Announcement", "body": "Dinner"},
        "speak": {"type": "text", "value": "Dinner"},
    }]
    assert p["target"] == {"customerId": "CUST", "devices": [
        {"deviceSerialNumber": "S1", "deviceTypeId": "A3S5BH2HU6VAYF"},
        {"deviceSerialNumber": "S2", "deviceTypeId": "AWZZ5CVHX2CD"},
    ]}


def test_speak_node_is_parallel_per_device():
    node = core.speak_node("Hello", [KITCHEN, SHOW], "CUST")
    assert node["@type"] == "com.amazon.alexa.behaviors.model.ParallelNode"
    kids = node["nodesToExecute"]
    assert [k["type"] for k in kids] == ["Alexa.Speak", "Alexa.Speak"]
    assert [k["operationPayload"]["deviceSerialNumber"] for k in kids] == ["S1", "S2"]
    assert kids[0]["operationPayload"]["textToSpeak"] == "Hello"
    assert kids[0]["operationPayload"]["skillId"] == "amzn1.ask.1p.saysomething"


def test_behaviour_body_round_trips_awkward_text():
    text = 'He said "hi"\nthen 🐈 left — & stayed <away>'
    body = json.loads(core.behaviour_body(core.announce_node(text, [KITCHEN], "C")))
    assert body["behaviorId"] == "PREVIEW"
    assert body["status"] == "ENABLED"
    seq = json.loads(body["sequenceJson"])
    assert seq["@type"] == "com.amazon.alexa.behaviors.model.Sequence"
    assert seq["startNode"]["operationPayload"]["content"][0]["speak"]["value"] == text
