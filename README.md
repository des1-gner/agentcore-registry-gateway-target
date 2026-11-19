# AWS Agent Registry as an AgentCore Gateway MCP target

How to add an [AWS Agent Registry](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry.html) MCP endpoint as an MCP server target on an [Amazon Bedrock AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html), why it often fails with an authorization error, and the minimum IAM permissions the gateway role needs.

Everything here was tested in `us-east-1` in November 2026 with boto3 1.44. Behaviour may change, so rerun the [reproduction script](#reproduce-it-yourself) if something differs.

## The symptom

Creating, updating or synchronising the target fails with:

```
Failed to connect and fetch tools from the provided MCP target server. Error - Authorization error when sending message
```

The target ends up in `FAILED`, `UPDATE_UNSUCCESSFUL` or `SYNCHRONIZE_UNSUCCESSFUL`.

The message is generic. It means the registry rejected the gateway's request, and it does not say why. Several different misconfigurations produce exactly the same text.

## Why it is confusing

1. **The console does not offer `agent-registry` as a SigV4 service name.** When you pick IAM outbound authorization for an MCP server target, the service name list does not include `agent-registry`. Picking `bedrock-agentcore` looks reasonable because the registry lives in the AgentCore docs, but it is the wrong signing service. You can type `agent-registry` into the field.
2. **The IAM outbound docs don't list Agent Registry.** The [MCP server targets page](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html) lists AgentCore Gateway, AgentCore Runtime, API Gateway and Lambda Function URLs as compatible with IAM outbound authorization. Agent Registry works too, as long as the service name is right.
3. **The registry's own auth type decides which outbound auth works.** A registry created with JWT authorization cannot be reached with IAM outbound auth at all, whatever the service name is.
4. **The batch get tool needs a differently named permission.** The MCP tool is `batch_get_discoverable_registry_record`, but the IAM action is `agent-registry:GetDiscoverableRegistryRecord`, on the record resource rather than the registry.
5. **A target can be `READY` and still fail on every tool call.** Sync only needs `InvokeRegistryMcp`, so a target can sync fine while each tool call fails with the same "Authorization error" message.

## The fix

### Step 1: check how your registry was created

Look at the registry's discovery (inbound) authorization in the console, or call `GetRegistry`. It is either `AWS_IAM` or `CUSTOM_JWT`. The two need different outbound auth on the gateway target.

### IAM registry (`AWS_IAM`)

Target configuration:

- Endpoint: `https://agent-registry.<region>.api.aws/registry/<registryId>/mcp`
- Outbound authorization: IAM (`GATEWAY_IAM_ROLE`)
- Service name: **`agent-registry`** (type it in if the console doesn't list it)
- Region: the registry's region

```json
"credentialProviderConfigurations": [
  {
    "credentialProviderType": "GATEWAY_IAM_ROLE",
    "credentialProvider": {
      "iamCredentialProvider": { "service": "agent-registry", "region": "us-east-1" }
    }
  }
]
```

Gateway service role permissions ([`policies/gateway-role-registry-iam.json`](policies/gateway-role-registry-iam.json)):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "agent-registry:InvokeRegistryMcp",
        "agent-registry:SearchDiscoverableRegistryRecords",
        "agent-registry:ListDiscoverableRegistryRecords"
      ],
      "Resource": "arn:aws:agent-registry:<REGION>:<ACCOUNT_ID>:registry/<REGISTRY_ID>"
    },
    {
      "Effect": "Allow",
      "Action": "agent-registry:GetDiscoverableRegistryRecord",
      "Resource": "arn:aws:agent-registry:<REGION>:<ACCOUNT_ID>:registry/<REGISTRY_ID>/record/*"
    }
  ]
}
```

The role must trust `bedrock-agentcore.amazonaws.com` ([`policies/gateway-trust-policy.json`](policies/gateway-trust-policy.json)). Leave out actions for tools you don't use.

### JWT registry (`CUSTOM_JWT`)

IAM outbound will not work. Use OAuth outbound instead:

1. In your identity provider, create a client that can get a token with the client credentials grant (machine to machine).
2. Add that client ID to the registry's `allowedClients`, or set `allowedAudience` to match the token's `aud` claim.
3. Create an AgentCore Identity OAuth2 credential provider for that client.
4. Set the target's outbound authorization to OAuth with that provider, grant type `CLIENT_CREDENTIALS`, and the scopes your identity provider needs.
5. Give the gateway role the AgentCore Identity token permissions ([`policies/gateway-role-oauth-outbound.json`](policies/gateway-role-oauth-outbound.json)):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetResourceOauth2Token",
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "*"
    }
  ]
}
```

Without step 5 the target still syncs, but every tool call returns `An internal error occurred. Please retry later.`

This was tested with an Amazon Cognito user pool as the identity provider. Other providers should work the same way, but the token claims (`client_id` or `aud`) must match what the registry's authorizer allows.

## Observed behaviour

### Outbound authorization against an IAM registry

| Target outbound auth | Target status | Message |
|---|---|---|
| None | `FAILED` | Authorization error when sending message |
| IAM, service `bedrock-agentcore` | `FAILED` | Authorization error when sending message |
| IAM, service `agent-registry`, role has no registry permissions (tested by hand, not in the script) | `FAILED` | Authorization error when sending message |
| IAM, service `agent-registry`, role has `InvokeRegistryMcp` | `READY` | |

### Outbound authorization against a JWT registry

| Target outbound auth | Target status | Tool call |
|---|---|---|
| IAM, service `agent-registry` | `FAILED`: Authorization error when sending message | n/a |
| OAuth client credentials, role without Identity token permissions | `READY` | `An internal error occurred. Please retry later.` |
| OAuth client credentials, role with Identity token permissions | `READY` | Works |

### Gateway role permissions per tool (IAM registry, service `agent-registry`)

| Gateway role policy | Sync | search | list | batch get |
|---|---|---|---|---|
| `InvokeRegistryMcp` | `READY` | Authorization error | Authorization error | Access denied |
| + `SearchDiscoverableRegistryRecords` | `READY` | Works | Authorization error | Access denied |
| + `ListDiscoverableRegistryRecords` + `BatchGetDiscoverableRegistryRecord` on the registry | `READY` | Works | Works | `Error: Access denied` |
| + `GetDiscoverableRegistryRecord` on the registry ARN only | `READY` | Works | Works | `Error: Access denied` |
| Recommended policy above (`GetDiscoverableRegistryRecord` on `registry/<id>/record/*`) | `READY` | Works | Works | Works |
| Recommended policy scoped to a different registry | `SYNCHRONIZE_UNSUCCESSFUL` | Authorization error | Authorization error | Authorization error |

Tool-call failures through the gateway show up as `McpException - MCP invocation failed: Authorization error when sending message`. The exception is batch get: when the role has access to the registry but not the record, it returns `Error: Access denied`. Search can return no matches for a newly approved record until it is indexed. List and batch get return it straight away.

The [Service Authorization Reference for AWS Agent Registry](https://docs.aws.amazon.com/service-authorization/latest/reference/list_agent-registry.html) maps the `BatchGetDiscoverableRegistryRecord` API to the `agent-registry:GetDiscoverableRegistryRecord` action on the `registry-record` resource. That is why granting `BatchGetDiscoverableRegistryRecord`, or granting `GetDiscoverableRegistryRecord` on the registry ARN, still fails.

## Reproduce it yourself

Requirements: [uv](https://docs.astral.sh/uv/), AWS credentials for a **test account** with permissions for IAM, Cognito, Agent Registry and AgentCore Gateway. The script creates billable resources, so run `cleanup` when you are done.

```bash
uv run scripts/repro.py setup          # IAM registry + approved record, gateway role, gateway
uv run scripts/repro.py service-name   # no auth / bedrock-agentcore / agent-registry targets
uv run scripts/repro.py permissions    # per-tool permission matrix
uv run scripts/repro.py jwt            # JWT registry: IAM vs OAuth outbound
uv run scripts/repro.py cleanup        # delete everything
```

Options: `--region` (default `us-east-1`) and `--prefix` (default `regdemo`). Resource IDs are kept in `scripts/state.json`, which is git-ignored, so `cleanup` knows what to delete.

The gateway in the script uses `AWS_IAM` inbound auth so the script can call tools with your own credentials. Inbound auth does not affect anything above, which is all about the gateway's outbound call to the registry.

## References

- [Using the Registry MCP endpoint](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-mcp-endpoint.html)
- [MCP server targets for AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html)
- [Actions, resources, and condition keys for AWS Agent Registry](https://docs.aws.amazon.com/service-authorization/latest/reference/list_agent-registry.html)
- [Create and manage registries](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-create-manage.html)

Not an official AWS project. Tested behaviour, not a guarantee.
