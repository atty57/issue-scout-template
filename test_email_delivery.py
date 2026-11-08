"""Recipient-only delivery and signed workflow authorization, without sending mail."""
import base64
import io
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import jwt
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

import email_delivery
import email_relay
import scout
from test_scout_recovery import result_state


ENDPOINT = "https://mail.example.com/send"


@pytest.fixture(autouse=True)
def no_live_calls(monkeypatch):
    monkeypatch.setattr(requests, "get", Mock(side_effect=AssertionError("unexpected live GET")))
    monkeypatch.setattr(requests, "post", Mock(side_effect=AssertionError("unexpected live POST")))


@pytest.mark.parametrize("address", ["dev@example.com", "dev+scout@example.co.uk", "first.last@example.com"])
def test_valid_single_recipient_addresses(address):
    assert email_delivery.validate_email(address) == address


@pytest.mark.parametrize("address", ["", "missing-domain", "a@example.com\r\nBcc: b@example.com", "A <a@example.com>",
                                        "a@example.com,b@example.com", "a@example.com;b@example.com", "a@localhost",
                                        "a@@example.com", "a@-example.com", "a@exam_ple.com", ".a@example.com", "a..b@example.com"])
def test_reject_malformed_or_multiple_recipients(address):
    with pytest.raises(ValueError, match="destination email"):
        email_delivery.validate_email(address)


def test_recipient_setting_overrides_profile_without_any_password(monkeypatch):
    assert email_delivery.recipient_email({"email": "profile@example.com"}) == "profile@example.com"
    monkeypatch.setenv("SCOUT_RECIPIENT_EMAIL", "override@example.com")
    assert email_delivery.recipient_email({"email": "profile@example.com"}) == "override@example.com"
    message, _, _ = scout.compose(result_state())
    assert message["To"] == "override@example.com" and message["From"] is None


def test_no_configured_relay_previews_without_network_or_credentials():
    message, _, _ = scout.compose(result_state())
    assert email_delivery.send_email(message, {"email": "dev@example.com"}) == {"status": "dry_run", "attempts": 0}


def test_local_run_without_workflow_identity_preserves_delivery_error(monkeypatch):
    monkeypatch.setenv("SCOUT_EMAIL_RELAY_URL", ENDPOINT)
    message, _, _ = scout.compose(result_state())
    delivery = email_delivery.send_email(message, {"email": "dev@example.com"})
    assert delivery["status"] == "failed" and delivery["attempts"] == 0
    assert "GitHub Actions identity" in delivery["error"]


@pytest.mark.parametrize("url", ["http://mail.example/send", "https://user:secret@mail.example/send",
                                     "https://mail.example/send?key=secret", "https://mail.example/send#fragment"])
def test_relay_configuration_rejects_insecure_endpoints(monkeypatch, url):
    monkeypatch.setenv("SCOUT_EMAIL_RELAY_URL", url)
    with pytest.raises(ValueError, match="HTTPS"):
        email_delivery.relay_url()


def test_github_identity_uses_relay_audience_without_leaking_request_token(monkeypatch):
    url = "https://vstoken.actions.githubusercontent.com/token?api-version=2.0"
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", url)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "private-runner-token")
    get = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"value": "signed-workflow-token"}))
    monkeypatch.setattr(requests, "get", get)
    assert email_delivery.github_identity(ENDPOINT) == "signed-workflow-token"
    assert get.call_args.kwargs["params"] == {"audience": ENDPOINT}
    assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer private-runner-token"}
    assert get.call_args.kwargs["allow_redirects"] is False
    get.return_value = SimpleNamespace(status_code=401, json=lambda: {"error": "private-runner-token"})
    with pytest.raises(ValueError) as error:
        email_delivery.github_identity(ENDPOINT)
    assert "private-runner-token" not in str(error.value)


def test_identity_token_cannot_be_sent_to_arbitrary_url(monkeypatch):
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://attacker.example/token")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "private-runner-token")
    with pytest.raises(ValueError):
        email_delivery.github_identity(ENDPOINT)


def test_relay_payload_contains_report_html_and_safe_patch_without_sender():
    message, body, patch = scout.compose(result_state())
    payload = email_delivery.message_payload(message, "dev@example.com")
    assert payload["to"] == "dev@example.com" and body in payload["text"]
    assert "<!doctype html>" in payload["html"]
    assert base64.b64decode(payload["attachments"][0]["content"]).decode() == patch
    assert "from" not in payload


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def claims(**changes):
    now = int(time.time())
    return {"iss": email_relay.ISSUER, "aud": ENDPOINT, "sub": "repo:alice/scout:ref:refs/heads/main",
            "iat": now, "nbf": now - 1, "exp": now + 300, "repository": "alice/scout",
            "repository_id": "12345678", "repository_owner_id": "9876", "ref": "refs/heads/main",
            "workflow_ref": "alice/scout/.github/workflows/daily.yml@refs/heads/main",
            "event_name": "workflow_dispatch", "run_id": "123", "run_attempt": "1"} | changes


def payload(**changes):
    return {"to": "dev@example.com", "subject": "Issue Scout draft", "text": "Report for human review",
            "html": "<p>Report for human review</p>", "attachments": [
                {"filename": "alice-core-1.patch", "content": base64.b64encode(b"+fixed\n").decode()}]} | changes


@pytest.fixture
def relay(signing_key):
    jwks = Mock(get_signing_key_from_jwt=Mock(return_value=SimpleNamespace(key=signing_key.public_key())))
    return email_relay.EmailRelay("private-resend-key", "drafts@example.com", ENDPOINT,
                                 {"12345678": {"owner_id": "9876", "email": "dev@example.com"}}, jwks)


def invoke(relay, token, body=None, **changes):
    data = json.dumps(payload() if body is None else body).encode()
    environ = {"PATH_INFO": "/send", "REQUEST_METHOD": "POST", "HTTP_AUTHORIZATION": f"Bearer {token}",
               "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(data)), "wsgi.input": io.BytesIO(data)} | changes
    start = Mock()
    response = json.loads(b"".join(relay(environ, start)))
    return start.call_args.args[0], response


def test_signed_authorized_run_uses_operator_sender_and_resend_key(monkeypatch, relay, signing_key):
    post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"id": "email-id"}))
    monkeypatch.setattr(requests, "post", post)
    token = jwt.encode(claims(), signing_key, algorithm="RS256", headers={"kid": "test-key"})
    status, result = invoke(relay, token)
    assert status == "200 OK" and result == {"status": "sent"}
    assert post.call_args.args == ("https://api.resend.com/emails",)
    request = post.call_args.kwargs
    assert request["json"]["from"] == "drafts@example.com" and request["json"]["to"] == ["dev@example.com"]
    assert request["headers"]["Authorization"] == "Bearer private-resend-key"
    assert request["allow_redirects"] is False
    assert base64.b64decode(request["json"]["attachments"][0]["content"]) == b"+fixed\n"


@pytest.mark.parametrize("overrides", [
    {"aud": "https://attacker.example"}, {"iss": "https://attacker.example"}, {"exp": 1},
    {"nbf": 9_999_999_999}, {"iat": 9_999_999_999},
])
def test_invalid_signed_claims_are_rejected_before_resend(relay, signing_key, overrides):
    token = jwt.encode(claims(**overrides), signing_key, algorithm="RS256")
    assert invoke(relay, token)[0] == "401 Unauthorized"


def test_forged_signature_is_rejected(relay):
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(claims(), attacker, algorithm="RS256")
    assert invoke(relay, token)[0] == "401 Unauthorized"


@pytest.mark.parametrize("overrides", [
    {"repository_id": "different-repo"}, {"repository_owner_id": "different-owner"},
    {"ref": "refs/heads/untrusted"}, {"event_name": "pull_request"},
    {"workflow_ref": "alice/scout/.github/workflows/other.yml@refs/heads/main"},
])
def test_only_enrolled_repository_owner_branch_and_workflow_can_send(relay, signing_key, overrides):
    token = jwt.encode(claims(**overrides), signing_key, algorithm="RS256")
    assert invoke(relay, token)[0] == "403 Forbidden"


def test_enrolled_repo_cannot_send_to_another_recipient_or_choose_sender(relay, signing_key):
    token = jwt.encode(claims(), signing_key, algorithm="RS256")
    assert invoke(relay, token, payload(to="someone-else@example.com"))[0] == "403 Forbidden"
    assert invoke(relay, token, payload(**{"from": "attacker@example.com"}))[0] == "400 Bad Request"


def test_retries_and_replays_share_one_idempotency_key_per_run(monkeypatch, relay, signing_key):
    post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"id": "email-id"}))
    monkeypatch.setattr(requests, "post", post)
    token = jwt.encode(claims(), signing_key, algorithm="RS256")
    invoke(relay, token)
    invoke(relay, token)
    invoke(relay, token, payload(text="Changed report"))
    keys = [call.kwargs["headers"]["Idempotency-Key"] for call in post.call_args_list]
    assert len(set(keys)) == 1
    token = jwt.encode(claims(run_attempt="2"), signing_key, algorithm="RS256")
    invoke(relay, token)
    assert post.call_args.kwargs["headers"]["Idempotency-Key"] != keys[0]


@pytest.mark.parametrize("status,expected", [(401, "422"), (422, "422"), (429, "429"), (500, "503"), (302, "422")])
def test_upstream_errors_do_not_reveal_key_or_response_body(monkeypatch, relay, signing_key, status, expected):
    reply = SimpleNamespace(status_code=status, text="private-resend-key upstream response")
    monkeypatch.setattr(requests, "post", Mock(return_value=reply))
    token = jwt.encode(claims(), signing_key, algorithm="RS256")
    response_status, result = invoke(relay, token)
    assert response_status.startswith(expected) and "private-resend-key" not in json.dumps(result)


def test_provider_acknowledgement_cannot_echo_the_resend_key_to_users(monkeypatch, relay, signing_key):
    # Even a malformed upstream response is reduced to our own fixed acknowledgement.
    reply = SimpleNamespace(status_code=200, json=lambda: {"id": "private-resend-key", "debug": "private-resend-key"})
    monkeypatch.setattr(requests, "post", Mock(return_value=reply))
    token = jwt.encode(claims(), signing_key, algorithm="RS256")
    status, result = invoke(relay, token)
    assert status == "200 OK" and result == {"status": "sent"}


def test_transport_exception_cannot_echo_the_resend_key_to_users(monkeypatch, relay, signing_key):
    monkeypatch.setattr(requests, "post", Mock(side_effect=requests.ConnectionError("Bearer private-resend-key")))
    token = jwt.encode(claims(), signing_key, algorithm="RS256")
    status, result = invoke(relay, token)
    assert status == "503 Service Unavailable" and "private-resend-key" not in json.dumps(result)


def test_user_configuration_and_workflow_never_request_or_receive_the_resend_key():
    for filename in (".env.example", ".github/workflows/daily.yml", "profile.example.json"):
        assert "RESEND_API_KEY" not in (scout.ROOT / filename).read_text(encoding="utf-8")


@pytest.mark.parametrize("body", [
    payload(subject="Subject\r\nBcc: victim@example.com"), payload(text=""),
    payload(attachments=[{"filename": "../../escape.patch", "content": "aGVsbG8="}]),
    payload(attachments=[{"filename": "report.patch", "content": "not-base64"}]),
    payload(attachments=[{"filename": "script.exe", "content": "aGVsbG8="}]),
])
def test_relay_rejects_header_injection_or_invalid_attachments(relay, signing_key, body):
    token = jwt.encode(claims(), signing_key, algorithm="RS256")
    assert invoke(relay, token, body)[0] == "400 Bad Request"


def test_relay_limits_request_size_and_requires_authentication(relay, signing_key):
    token = jwt.encode(claims(), signing_key, algorithm="RS256")
    assert invoke(relay, token, CONTENT_LENGTH=str(email_relay.MAX_BODY + 1))[0] == "413 Content Too Large"
    assert invoke(relay, token, HTTP_AUTHORIZATION="")[0] == "401 Unauthorized"


def test_resend_and_workflow_credentials_are_redacted_and_not_passed_to_models(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_" + "secret" * 8)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "private-workflow-token")
    assert "resend key" in scout.scan_secrets("re_" + "unknown" * 8)
    for value in ("re_" + "secret" * 8, "private-workflow-token"):
        assert value not in scout.redact(value)
    from provider_config import ProviderConfig
    env = scout.model_providers.cli_environment(ProviderConfig("solver", "codex", auth="subscription"))
    assert "RESEND_API_KEY" not in env and "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in env
