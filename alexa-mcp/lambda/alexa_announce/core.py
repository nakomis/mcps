"""Pure logic for the alexa-announce Lambda: no I/O and no alexapy.

The payload shapes mirror what alexapy's send_announcement and send_tts build.
The announcement shape was proven against a real account in the 10 Oct 2026
spike.
"""

import json

LOCALE = "en-GB"
MAX_TEXT = 1000
ACTIONS = ("announce", "speak")

# Amazon's device list includes Fire TVs, the Alexa app itself, speaker groups
# and so on. Anything that can be told to speak has a volume.
SPEAKER_CAPABILITY = "VOLUME_SETTING"


class RequestError(Exception):
    """A request we refuse before talking to Amazon."""

    def __init__(self, error: str, detail):
        super().__init__(error)
        self.error = error
        self.detail = detail


def validate(event: dict) -> tuple[str, str, list[str]]:
    action = event.get("action")
    text = event.get("text")
    names = event.get("devices")
    if action not in ACTIONS:
        raise RequestError("bad_request", f"action must be one of {ACTIONS}, got {action!r}")
    if not isinstance(text, str) or not text.strip():
        raise RequestError("bad_request", "text must be a non-empty string")
    text = text.strip()
    if len(text) > MAX_TEXT:
        raise RequestError("bad_request", f"text is {len(text)} characters; the limit is {MAX_TEXT}")
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(n, str) and n.strip() for n in names)
    ):
        raise RequestError("bad_request", "devices must be a non-empty list of device names")
    return action, text, names


def _norm(name: str) -> str:
    # Phones type a curly apostrophe; Amazon's names use a straight one.
    return name.replace("’", "'").strip().casefold()


def resolve_devices(names: list[str], devices: list[dict]) -> list[dict]:
    """Map names to devices. Any unknown name fails the whole call, so nothing is sent."""
    speakers = [d for d in devices if SPEAKER_CAPABILITY in d.get("capabilities", [])]
    by_name = {_norm(d["accountName"]): d for d in speakers}
    resolved, unknown = [], []
    for name in names:
        device = by_name.get(_norm(name))
        if device is None:
            unknown.append(name)
        elif device not in resolved:
            resolved.append(device)
    if unknown:
        raise RequestError(
            "unknown_devices",
            {"unknown": unknown, "valid": sorted(d["accountName"] for d in speakers)},
        )
    return resolved


def _operation(kind: str, payload: dict) -> dict:
    return {
        "@type": "com.amazon.alexa.behaviors.model.OpaquePayloadOperationNode",
        "type": kind,
        "operationPayload": payload,
    }


def announce_node(text: str, devices: list[dict], customer_id: str) -> dict:
    """One AlexaAnnouncement: chime, then the text, in sync on every target."""
    first = devices[0]
    return _operation("AlexaAnnouncement", {
        "deviceType": first["deviceType"],
        "deviceSerialNumber": first["serialNumber"],
        "locale": LOCALE,
        "customerId": customer_id,
        "expireAfter": "PT5S",
        "content": [{
            "locale": LOCALE,
            "display": {"title": "Announcement", "body": text},
            "speak": {"type": "text", "value": text},
        }],
        "target": {
            "customerId": customer_id,
            "devices": [
                {"deviceSerialNumber": d["serialNumber"], "deviceTypeId": d["deviceType"]}
                for d in devices
            ],
        },
    })


def speak_node(text: str, devices: list[dict], customer_id: str) -> dict:
    """Plain speech with no chime. Alexa.Speak ignores multi-device targets, so use one node per device."""
    return {
        "@type": "com.amazon.alexa.behaviors.model.ParallelNode",
        "nodesToExecute": [
            _operation("Alexa.Speak", {
                "deviceType": d["deviceType"],
                "deviceSerialNumber": d["serialNumber"],
                "locale": LOCALE,
                "customerId": customer_id,
                "textToSpeak": text,
                "skillId": "amzn1.ask.1p.saysomething",
            })
            for d in devices
        ],
    }


def behaviour_body(start_node: dict) -> str:
    sequence = {"@type": "com.amazon.alexa.behaviors.model.Sequence", "startNode": start_node}
    return json.dumps({
        "behaviorId": "PREVIEW",
        "sequenceJson": json.dumps(sequence),
        "status": "ENABLED",
    })
