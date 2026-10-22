"""The frontend's API paths and mirrored types against the backend's OpenAPI schema (AUDIT-4 A4-7, SG-P0)."""
import re
from pathlib import Path

import pytest

from app.main import app

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"
CALL = re.compile(r"api\.(get|post|put|patch|delete)(?:<[^>]*>)?\(\s*[\"`]([^\"`]+)[\"`]")
FIELD = re.compile(r"^\s*(\w+)(\??):\s*(.+?);?$")
# TS mirror -> pydantic schema
MIRRORS = {"EventIn": "EventIn", "TracePolicyCreate": "TracePolicyCreate", "TracePolicyUpdate": "TracePolicyUpdate",
           "TracePolicy": "TracePolicyOut", "PolicyValidateIn": "PolicyValidateIn",
           "PolicyValidateOut": "PolicyValidateOut", "Violation": "ViolationOut", "PolicyCheckIn": "PolicyCheckIn",
           "PolicyCheckOut": "PolicyCheckOut", "EventsAppendIn": "EventsAppendIn", "EventsAppendOut": "EventsAppendOut",
           # SG-P0 platform contracts
           "TokenPair": "TokenPair", "InviteRequest": "InviteRequest", "Invite": "InviteOut",
           "AcceptInviteRequest": "AcceptInviteRequest", "PasswordChangeRequest": "PasswordChangeRequest",
           "Integration": "IntegrationOut", "SigningSecret": "SigningSecretOut",
           "SlackOAuthStart": "SlackOAuthStartOut",
           "WebhookRunner": "WebhookRunner", "TracesRunner": "TracesRunner"}
OUTPUTS = {"TokenPair"}  # output schemas whose names don't end in Out


def _calls():
    for f in sorted((FRONTEND / "api").glob("*.ts")):
        text = f.read_text()
        assert len(CALL.findall(text)) == len(re.findall(r"\bapi\.(get|post|put|patch|delete)\b", text)), \
            f"{f.name}: a call this test cannot parse"
        for method, path in CALL.findall(text):
            yield f.name, method, re.sub(r"\$\{[^}]+\}", "{}", path)


def _ts_types() -> dict[str, dict[str, bool]]:
    """Each `export type X = { ... }` as {field: optional}; one-line bodies are split on `;`."""
    out = {}
    for f in sorted((FRONTEND / "types").glob("*.ts")):
        for name, body in re.findall(r"export type (\w+) = \{(.*?)\};", f.read_text(), re.S):
            fields = [FIELD.match(part) for part in re.split(r"[;\n]", body)
                      if part.strip() and not part.strip().startswith("//")]
            assert name not in out, f"{name} is defined twice"
            out[name] = {m[1]: bool(m[2]) for m in fields}
    return out


def test_every_api_call_matches_an_openapi_route():
    routes = {(m, re.sub(r"\{[^}]+\}", "{}", p)) for p, ops in app.openapi()["paths"].items() for m in ops}
    calls = list(_calls())
    assert {f for f, _, _ in calls} >= {"tracePolicies.ts", "traces.ts", "auth.ts", "evaluations.ts"}
    assert {c[1:] for c in calls} >= {("post", "/api/auth/accept-invite"), ("post", "/api/auth/password"),
                                      ("post", "/api/integrations/{}/signing-secret")}
    assert not [c for c in calls if c[1:] not in routes]


@pytest.mark.parametrize("ts,schema", MIRRORS.items())
def test_policy_types_mirror_the_schema(ts, schema):
    schemas = app.openapi()["components"]["schemas"]
    spec = schemas[schema]
    required = set(spec.get("required", []))
    # a field with a default is optional on input and always present on output
    output = schema.endswith("Out") or schema in OUTPUTS
    assert _ts_types()[ts] == {f: not output and f not in required for f in spec["properties"]}


def test_platform_types_extend_the_schema():
    """`IntegrationCreated` and `EvalSet` are intersections or carry client-only fields, so they are checked apart."""
    types = _ts_types()
    spec = app.openapi()["components"]["schemas"]
    assert set(spec["IntegrationCreatedOut"]["properties"]) - set(spec["IntegrationOut"]["properties"]) == \
        {"signing_secret"}
    assert "IntegrationCreated = Integration & { signing_secret: string | null }" in \
        (FRONTEND / "types/integration.ts").read_text()
    assert set(types["EvalSet"]) - {"cases"} == set(spec["EvalSetOut"]["properties"])
    assert not types["EvalSet"]["runner"]
    assert "EvalRunner = WebhookRunner | TracesRunner" in (FRONTEND / "types/evaluation.ts").read_text()
