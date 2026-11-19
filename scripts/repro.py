#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3>=1.44", "requests"]
# ///
"""Reproduce AWS Agent Registry as an Amazon Bedrock AgentCore Gateway MCP target.

Subcommands (run in order):
  setup         IAM-auth registry with one approved record, gateway role, gateway
  service-name  targets with no outbound auth / wrong SigV4 service / correct service
  permissions   minimum gateway role permissions per registry MCP tool
  jwt           JWT-auth registry: IAM outbound vs OAuth outbound (Cognito client credentials)
  cleanup       delete everything this script created

Uses your default AWS credentials. Creates billable resources; run cleanup afterwards.
"""
import argparse, json, os, secrets, sys, time

import boto3, requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
IAM_PROPAGATION_SECONDS = 30  # IAM changes are eventually consistent


def load():
    return json.load(open(STATE)) if os.path.exists(STATE) else {}


def save(s):
    json.dump(s, open(STATE, "w"), indent=2)


def clients(region):
    return (boto3.client("iam"),
            boto3.client("agent-registry-control", region_name=region),
            boto3.client("bedrock-agentcore-control", region_name=region),
            boto3.client("cognito-idp", region_name=region))


def wait(fn, done, timeout=600, every=5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if done(v):
            return v
        time.sleep(every)
    raise TimeoutError("timed out waiting for resource")


def registry_endpoint(region, rid):
    return f"https://agent-registry.{region}.api.aws/registry/{rid}/mcp"


def create_target(gc, s, name, endpoint, creds):
    kw = dict(gatewayIdentifier=s["gateway_id"], name=name,
              targetConfiguration={"mcp": {"mcpServer": {"endpoint": endpoint}}})
    if creds:
        kw["credentialProviderConfigurations"] = creds
    tid = gc.create_gateway_target(**kw)["targetId"]
    t = wait(lambda: gc.get_gateway_target(gatewayIdentifier=s["gateway_id"], targetId=tid),
             lambda t: t["status"] not in ("CREATING", "UPDATING"), every=4)
    s.setdefault("targets", {})[name] = tid
    save(s)
    reason = (t.get("statusReasons") or [""])[0]
    print(f"  target {name:<22} {t['status']:<10} {reason}")
    return tid


def iam_outbound(service, region):
    return [{"credentialProviderType": "GATEWAY_IAM_ROLE",
             "credentialProvider": {"iamCredentialProvider": {"service": service, "region": region}}}]


def put_role_policy(iam, s, name, statements):
    iam.put_role_policy(RoleName=s["role_name"], PolicyName=name,
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}))


def mcp_call(s, target, tool, args):
    """Call a tool through the gateway (gateway inbound auth is AWS_IAM, so SigV4 sign it)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": f"{target}___{tool}", "arguments": args}})
    req = AWSRequest(method="POST", url=s["gateway_url"], data=body,
                     headers={"Content-Type": "application/json",
                              "Accept": "application/json, text/event-stream"})
    SigV4Auth(boto3.Session().get_credentials().get_frozen_credentials(),
              "bedrock-agentcore", s["region"]).add_auth(req)
    res = requests.post(s["gateway_url"], data=body, headers=dict(req.headers), timeout=60).json()
    result = res.get("result", {})
    text = result.get("content", [{}])[0].get("text", json.dumps(res))[:90].replace("\n", " ")
    return ("ERROR " if result.get("isError") else "OK    ") + text


def sync(gc, s, tid):
    gc.synchronize_gateway_targets(gatewayIdentifier=s["gateway_id"], targetIdList=[tid])
    time.sleep(3)
    t = wait(lambda: gc.get_gateway_target(gatewayIdentifier=s["gateway_id"], targetId=tid),
             lambda t: "SYNCHRONIZING" not in t["status"], every=4)
    return t["status"]


def cmd_setup(a):
    iam, rc, gc, _ = clients(a.region)
    account = boto3.client("sts").get_caller_identity()["Account"]
    s = {"region": a.region, "account": account, "prefix": a.prefix}
    rid = rc.create_registry(name=f"{a.prefix}_iam",
                             discoveryConfiguration={"authorizerType": "AWS_IAM"})["registryArn"].split("/")[-1]
    s["iam_registry"] = rid
    save(s)
    wait(lambda: rc.get_registry(registryId=rid), lambda r: r.get("status") != "CREATING")
    rec = rc.create_registry_record(registryId=rid, name="weather_demo", recordType="CUSTOM",
                                    description="demo record",
                                    descriptors={"custom": {"data": json.dumps({"name": "weather_demo"})}})
    rec_id = rec["recordArn"].split("/")[-1]
    wait(lambda: rc.get_registry_record(registryId=rid, recordId=rec_id), lambda r: r.get("status") == "DRAFT", every=3)
    rc.submit_registry_record_for_approval(registryId=rid, recordId=rec_id)
    time.sleep(3)
    rc.update_registry_record_status(registryId=rid, recordId=rec_id, status="APPROVED", statusReason="demo")
    s["record_id"] = rec_id
    role = f"{a.prefix}-gateway-role"
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
             "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    iam.create_role(RoleName=role, AssumeRolePolicyDocument=json.dumps(trust))
    s["role_name"] = role
    save(s)
    put_role_policy(iam, s, "registry", [{"Effect": "Allow", "Action": "agent-registry:InvokeRegistryMcp",
                                          "Resource": registry_arn(s)}])
    time.sleep(IAM_PROPAGATION_SECONDS)
    g = gc.create_gateway(name=f"{a.prefix}-gw", roleArn=f"arn:aws:iam::{account}:role/{role}",
                          protocolType="MCP", authorizerType="AWS_IAM")
    s["gateway_id"], s["gateway_url"] = g["gatewayId"], g["gatewayUrl"]
    save(s)
    wait(lambda: gc.get_gateway(gatewayIdentifier=s["gateway_id"]), lambda x: x["status"] != "CREATING", every=6)
    print(f"setup done: registry {rid}, gateway {s['gateway_id']}")


def registry_arn(s, rid=None):
    return f"arn:aws:agent-registry:{s['region']}:{s['account']}:registry/{rid or s['iam_registry']}"


def cmd_service_name(a):
    _, _, gc, _ = clients(a.region)
    s = load()
    ep = registry_endpoint(s["region"], s["iam_registry"])
    print("IAM-auth registry, outbound auth variations:")
    create_target(gc, s, "no-auth", ep, None)
    create_target(gc, s, "svc-bedrock-agentcore", ep, iam_outbound("bedrock-agentcore", s["region"]))
    create_target(gc, s, "svc-agent-registry", ep, iam_outbound("agent-registry", s["region"]))


def cmd_permissions(a):
    iam, _, gc, _ = clients(a.region)
    s = load()
    tid, tname = s["targets"]["svc-agent-registry"], "svc-agent-registry"
    reg, records = registry_arn(s), registry_arn(s) + "/record/*"
    A = lambda *names: [f"agent-registry:{n}" for n in names]
    cases = [
        ("InvokeRegistryMcp only", [{"Effect": "Allow", "Action": A("InvokeRegistryMcp"), "Resource": reg}]),
        ("+ Search", [{"Effect": "Allow", "Action": A("InvokeRegistryMcp", "SearchDiscoverableRegistryRecords"), "Resource": reg}]),
        ("+ Search, List, BatchGet* on registry", [{"Effect": "Allow", "Action": A(
            "InvokeRegistryMcp", "SearchDiscoverableRegistryRecords", "ListDiscoverableRegistryRecords",
            "BatchGetDiscoverableRegistryRecord"), "Resource": reg}]),
        ("+ GetDiscoverableRegistryRecord on registry", [{"Effect": "Allow", "Action": A(
            "InvokeRegistryMcp", "SearchDiscoverableRegistryRecords", "ListDiscoverableRegistryRecords",
            "GetDiscoverableRegistryRecord"), "Resource": reg}]),
        ("recommended (two statements)", [
            {"Effect": "Allow", "Action": A("InvokeRegistryMcp", "SearchDiscoverableRegistryRecords",
                                            "ListDiscoverableRegistryRecords"), "Resource": reg},
            {"Effect": "Allow", "Action": A("GetDiscoverableRegistryRecord"), "Resource": records}]),
        ("recommended, scoped to a different registry", [
            {"Effect": "Allow", "Action": A("InvokeRegistryMcp", "SearchDiscoverableRegistryRecords",
                                            "ListDiscoverableRegistryRecords"), "Resource": registry_arn(s, "otherregistry00")},
            {"Effect": "Allow", "Action": A("GetDiscoverableRegistryRecord"), "Resource": registry_arn(s, "otherregistry00") + "/record/*"}]),
    ]
    for label, stmts in cases:
        put_role_policy(iam, s, "registry", stmts)
        time.sleep(IAM_PROPAGATION_SECONDS)
        print(f"\n[{label}]")
        print("  sync  :", sync(gc, s, tid))
        print("  search:", mcp_call(s, tname, "search_discoverable_registry_records", {"searchQuery": "weather"}))
        print("  list  :", mcp_call(s, tname, "list_discoverable_registry_records", {}))
        print("  batch :", mcp_call(s, tname, "batch_get_discoverable_registry_record", {"recordIds": [s["record_id"]]}))


def cmd_jwt(a):
    iam, rc, gc, cg = clients(a.region)
    s = load()
    R = s["region"]
    pool = cg.create_user_pool(PoolName=f"{s['prefix']}-pool")["UserPool"]["Id"]
    s["cognito_pool"] = pool
    save(s)
    domain = f"{s['prefix'].replace('_', '-')}-{secrets.token_hex(4)}"
    cg.create_user_pool_domain(UserPoolId=pool, Domain=domain)
    s["cognito_domain"] = domain
    cg.create_resource_server(UserPoolId=pool, Identifier="registry", Name="registry",
                              Scopes=[{"ScopeName": "read", "ScopeDescription": "read"}])
    client = cg.create_user_pool_client(UserPoolId=pool, ClientName="gateway-m2m", GenerateSecret=True,
                                        AllowedOAuthFlows=["client_credentials"],
                                        AllowedOAuthScopes=["registry/read"],
                                        AllowedOAuthFlowsUserPoolClient=True)["UserPoolClient"]
    discovery = f"https://cognito-idp.{R}.amazonaws.com/{pool}/.well-known/openid-configuration"
    jrid = rc.create_registry(name=f"{s['prefix']}_jwt", discoveryConfiguration={
        "authorizerType": "CUSTOM_JWT", "authorizerConfiguration": {"customJWTAuthorizer": {
            "discoveryUrl": discovery, "allowedClients": [client["ClientId"]]}}})["registryArn"].split("/")[-1]
    s["jwt_registry"] = jrid
    save(s)
    provider = gc.create_oauth2_credential_provider(
        name=f"{s['prefix']}-cognito", credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={"customOauth2ProviderConfig": {
            "oauthDiscovery": {"discoveryUrl": discovery},
            "clientId": client["ClientId"], "clientSecret": client["ClientSecret"]}})
    s["oauth_provider"] = provider["name"] if "name" in provider else f"{s['prefix']}-cognito"
    save(s)
    wait(lambda: rc.get_registry(registryId=jrid), lambda r: r.get("status") != "CREATING")
    time.sleep(20)  # new Cognito domain needs a moment before it issues tokens
    ep = registry_endpoint(R, jrid)
    print("JWT-auth registry, outbound auth variations:")
    create_target(gc, s, "jwt-iam-outbound", ep, iam_outbound("agent-registry", R))
    create_target(gc, s, "jwt-oauth-outbound", ep, [{"credentialProviderType": "OAUTH", "credentialProvider": {
        "oauthCredentialProvider": {"providerArn": provider["credentialProviderArn"],
                                    "scopes": ["registry/read"], "grantType": "CLIENT_CREDENTIALS"}}}])
    try:
        iam.delete_role_policy(RoleName=s["role_name"], PolicyName="identity")
    except iam.exceptions.NoSuchEntityException:
        pass
    time.sleep(IAM_PROPAGATION_SECONDS)
    print("\n[gateway role without AgentCore Identity token permissions]")
    print("  search:", mcp_call(s, "jwt-oauth-outbound", "search_discoverable_registry_records", {"searchQuery": "weather"}))
    put_role_policy(iam, s, "identity", [{"Effect": "Allow", "Action": [
        "bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetResourceOauth2Token",
        "secretsmanager:GetSecretValue"], "Resource": "*"}])
    time.sleep(IAM_PROPAGATION_SECONDS)
    print("[gateway role with AgentCore Identity token permissions]")
    print("  search:", mcp_call(s, "jwt-oauth-outbound", "search_discoverable_registry_records", {"searchQuery": "weather"}))


def delete_registry(rc, rid):
    """A registry can't be deleted while it still has records, so remove those first."""
    for r in rc.list_registry_records(registryId=rid).get("registryRecords", []):
        rc.delete_registry_record(registryId=rid, recordId=r["recordId"])
    wait(lambda: rc.list_registry_records(registryId=rid).get("registryRecords"), lambda recs: not recs)
    for _ in range(30):
        try:
            rc.delete_registry(registryId=rid)
            return
        except rc.exceptions.ConflictException:
            time.sleep(10)  # registry still CREATING/UPDATING or records still deleting
    print(f"WARNING: could not delete registry {rid}, delete it manually")


def cmd_cleanup(a):
    iam, rc, gc, cg = clients(a.region)
    s = load()
    if not s:
        print("nothing to clean up")
        return
    gid = s.get("gateway_id")
    if gid:
        for t in gc.list_gateway_targets(gatewayIdentifier=gid)["items"]:
            gc.delete_gateway_target(gatewayIdentifier=gid, targetId=t["targetId"])
        wait(lambda: gc.list_gateway_targets(gatewayIdentifier=gid)["items"], lambda items: not items, every=4)
        gc.delete_gateway(gatewayIdentifier=gid)
    if s.get("oauth_provider"):
        gc.delete_oauth2_credential_provider(name=s["oauth_provider"])
    for key in ("iam_registry", "jwt_registry"):
        if s.get(key):
            delete_registry(rc, s[key])
    if s.get("cognito_pool"):
        if s.get("cognito_domain"):
            cg.delete_user_pool_domain(UserPoolId=s["cognito_pool"], Domain=s["cognito_domain"])
        cg.delete_user_pool(UserPoolId=s["cognito_pool"])
    if s.get("role_name"):
        for p in iam.list_role_policies(RoleName=s["role_name"])["PolicyNames"]:
            iam.delete_role_policy(RoleName=s["role_name"], PolicyName=p)
        iam.delete_role(RoleName=s["role_name"])
    os.remove(STATE)
    print("cleanup done")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["setup", "service-name", "permissions", "jwt", "cleanup"])
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--prefix", default="regdemo", help="name prefix for created resources (letters, digits, _)")
    a = p.parse_args()
    {"setup": cmd_setup, "service-name": cmd_service_name, "permissions": cmd_permissions,
     "jwt": cmd_jwt, "cleanup": cmd_cleanup}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
