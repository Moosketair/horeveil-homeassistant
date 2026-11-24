"""Verify a synthetic Authlete/AndroidKeyStore request with the Python RFC 9421 library."""

from argparse import ArgumentParser
from base64 import b64decode
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_public_key
from http_message_signatures import HTTPMessageVerifier, HTTPSignatureKeyResolver, InvalidSignature, algorithms

BODY = b'{"probe":"qualification"}'
COMPONENTS = ("@method", "@target-uri", "content-digest", "content-type", "horeveil-server-id")


class ProbeKey(HTTPSignatureKeyResolver):
    def __init__(self, public_key):
        self.public_key = public_key

    def resolve_public_key(self, key_id):
        return self.public_key if key_id == "probe" else None


def main(path, expect_time_refusal):
    values = dict(line.removeprefix("INSTRUMENTATION_RESULT: probe_").split("=", 1)
                  for line in path.read_text("utf-8-sig").splitlines()
                  if line.startswith("INSTRUMENTATION_RESULT: probe_"))
    key = load_der_public_key(b64decode(values["spki"], validate=True))
    assert isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1)
    message = SimpleNamespace(method="POST", url=values["url"], headers={
        "Content-Type": "application/json", "Content-Digest": values["digest"],
        "Signature-Input": values["input"], "Signature": values["signature"], "Horeveil-Server-Id": values["server"],
    })

    def verify(request=message, body=BODY, public_key=key, tag="horeveil-enroll-v1"):
        verifier = HTTPMessageVerifier(signature_algorithm=algorithms.ECDSA_P256_SHA256,
                                       key_resolver=ProbeKey(public_key))
        return verifier.verify(request, max_age=timedelta(seconds=120), expect_tag=tag,
                               require_components=COMPONENTS, body=body)

    if expect_time_refusal:
        try:
            verify()
        except InvalidSignature as error:
            assert "Signature" in str(error) and ("time" in str(error) or "age" in str(error)), str(error)
            print("Horloge hors fenêtre refusée.")
            return
        raise AssertionError("Signature outside the time window was accepted")

    verified, = verify()
    assert verified.parameters["nonce"] and verified.parameters["expires"] > verified.parameters["created"]
    # Cryptographic validity alone never consumes a nonce.
    assert len(verify()) == 1
    probes = (
        lambda: verify(body=b"{}"),
        lambda: verify(SimpleNamespace(**(vars(message) | {"method": "PUT"}))),
        lambda: verify(SimpleNamespace(**(vars(message) | {"url": message.url + "-other"}))),
        lambda: verify(SimpleNamespace(**(vars(message) | {"url": message.url.replace("qualification.", "other.")}))),
        lambda: verify(SimpleNamespace(method=message.method, url=message.url,
                                       headers=message.headers | {"Horeveil-Server-Id": "other"})),
        lambda: verify(public_key=ec.generate_private_key(ec.SECP256R1()).public_key()),
        lambda: verify(tag="horeveil-calendar-v1"),
    )
    for invalid in probes:
        try:
            invalid()
        except InvalidSignature:
            continue
        raise AssertionError("An altered proof was accepted")
    print("Signature Authlete valide côté Python. Sept altérations refusées.")


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--expect-time-refusal", action="store_true")
    args = parser.parse_args()
    main(args.fixture, args.expect_time_refusal)
