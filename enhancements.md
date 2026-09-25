I want you to create a revised version of `rebuild-guide.md` in this repository:

https://github.com/RyanMicrosoftContosoUniversity/ai-chatbot-multi-llm

The existing file is:

rebuild-guide.md

IMPORTANT:
- Read the CURRENT `rebuild-guide.md` from the main branch before making changes.
- This is a documentation/architecture update. Do not deploy or modify Azure resources.
- Modify only `rebuild-guide.md` unless there is an unavoidable reason to touch another file.
- Preserve the useful "each phase is a gate" philosophy of the existing guide.
- Do not blindly trust every technical assertion in the current guide. Validate Azure Container Apps, private networking, workload profile, managed identity, ACR, Azure SQL, and Foundry/MCP behavior against current Microsoft documentation before carrying statements forward.
- In particular, scrutinize statements such as "Consumption-only environments cannot reach private endpoints" or similar hard restrictions. If they are inaccurate or outdated, correct them.

==================================================
OVERALL ARCHITECTURE WE ARE TRYING TO DOCUMENT
==================================================

The application architecture is approximately:

Azure AI Foundry agent
        |
        | calls MCP
        v
Azure Container App
  - hosts the MCP server
  - lives inside an Azure Container Apps Environment
  - uses a user-assigned managed identity
        |
        | private connectivity
        v
Azure SQL Database

The Container App image is stored in Azure Container Registry.

The intended networking model is private-first.

We need to document TWO different communication directions clearly:

1. OUTBOUND:
   Azure Container App / MCP server -> Azure SQL

2. INBOUND:
   Azure AI Foundry agent -> MCP server running in Azure Container Apps

Do not mix these together. They have different networking and authentication concerns.

==================================================
NETWORK DESIGN
==================================================

The VNet design we discussed uses at least two dedicated subnets.

1. ACA infrastructure subnet

Example:
`snet-aca-infra`

Purpose:
- Used by the Azure Container Apps Environment.
- Dedicated to ACA infrastructure.
- Contains the appropriate ACA subnet delegation.
- The Container Apps Environment is attached to this subnet.

2. Private Endpoint subnet

Example:
`snet-private-endpoints` or `snet-pe`

Purpose:
- Holds private endpoints for PaaS resources.
- Azure SQL private endpoint.
- Azure Container Registry private endpoint.
- Potentially Key Vault, Storage, AI Search, etc. later.

It is perfectly reasonable for several private endpoints to coexist in this private endpoint subnet. They do not each require their own subnet.

Make the private endpoint subnet an explicit variable in Phase 0 instead of hard-coding `snet-pe` later.

Clearly explain what each subnet is responsible for.

==================================================
PHASE 1 — NETWORK FOUNDATION
==================================================

Phase 1 should be entirely about proving the network before creating compute.

Walk through:

1. Existing VNet verification / creation assumptions.
2. ACA infrastructure subnet.
3. ACA subnet delegation.
4. Private endpoint subnet.
5. Azure SQL private endpoint.
6. SQL private DNS.
7. ACR private endpoint/private DNS if private ACR is being used.
8. Routing / NSGs / firewall / DNS forwarders.
9. Required outbound connectivity.

SQL private DNS zone:

`privatelink.database.windows.net`

The VNet must be linked to the private DNS zone.

The important distinction is:

Seeing a VNet link in the Private DNS Zone is a CONFIGURATION check.

Running DNS resolution from inside the Container App Environment is a RUNTIME check.

The VNet link existing does NOT by itself guarantee that containers will successfully resolve the SQL server to the private address.

Things that can still break DNS include:
- Missing SQL A record.
- Broken/missing private endpoint DNS zone group.
- Custom DNS servers that are not forwarding private-link resolution correctly.
- Incorrect VNet linkage.
- Other DNS forwarding/configuration issues.

The expected runtime result should be that:

`<sql-server>.database.windows.net`

ultimately resolves to a private RFC1918 address, normally something like `10.x.x.x`.

If it resolves to a public address, the application is not following the intended private SQL path.

==================================================
PHASE 2 — IDENTITY
==================================================

Use a USER-ASSIGNED MANAGED IDENTITY (UAMI), not a system-assigned identity, for this design.

Explain WHY:

A user-assigned identity has a lifecycle independent of the Container App.

Therefore:
- The app can be deleted/recreated.
- The identity remains the same.
- SQL permissions remain associated with the same identity.
- ACR permissions remain associated with the same identity.
- Rebuilds do not generate a brand-new identity that needs to be reauthorized.

Create variables such as:

UAMI_ID
UAMI_PRINCIPAL
UAMI_CLIENT

Clearly explain each:

UAMI_ID
= Azure resource ID of the user-assigned managed identity.

UAMI_PRINCIPAL
= Entra service principal/object ID used for Azure RBAC assignments.

UAMI_CLIENT
= client ID used by SDK credential selection where appropriate.

The UAMI needs:

ACR:
- `AcrPull` on the Azure Container Registry.

Container App:
- The identity is ATTACHED to the Container App.
- It does NOT need some special Azure RBAC role "on the Container App" simply so the app can use it.

SQL:
- Azure SQL authorization occurs INSIDE THE DATABASE.
- Do not describe this as merely an Azure RBAC assignment.

Example:

CREATE USER [id-dab-sql-agent-eus-nonprod]
FROM EXTERNAL PROVIDER;

Then give only the database permissions required.

If application is read-only:

ALTER ROLE db_datareader
ADD MEMBER [id-dab-sql-agent-eus-nonprod];

Do NOT grant db_datawriter unless the application actually requires writes.

The original guide puts the SQL grant in a later phase. We discussed that there is no technical reason it cannot be performed immediately after the UAMI exists.

Consider moving the SQL user/grant into Phase 2, or clearly explaining that it can be completed here before ACA exists.

Also retain the requirement that Azure SQL has an Entra administrator configured so the external-provider user can be created.

==================================================
PHASE 3 — AZURE CONTAINER APPS ENVIRONMENT
==================================================

Phase 3 creates the Azure Container Apps ENVIRONMENT.

It does NOT create the production Container App yet.

Explain the distinction clearly:

Azure Container Apps Environment
= hosting/network/security boundary.

Azure Container App
= an application deployed INSIDE that environment.

The environment is attached to the ACA infrastructure subnet.

Discuss internal-only configuration.

For an internal Container Apps Environment:

- Inbound application traffic is private.
- The environment's inbound/static address should be private rather than a public 20.x-style address.
- Public outbound/egress IP addresses are a separate concept and may still exist depending on configuration.

We specifically discussed a previous ACA Environment where the static IP appeared as a `20.x.x.x` address.

Explain that this strongly suggests the previous environment was not configured as an internal-only environment, assuming the value being inspected was indeed the environment's inbound/static IP.

Do not confuse:
- inbound static/environment IP
with
- outbound public egress IPs.

==================================================
WORKLOAD PROFILES
==================================================

Explain this carefully.

There are two levels:

ENVIRONMENT:
Defines which workload profiles/capacity options exist.

CONTAINER APP:
Chooses the workload profile it runs on.

A Container App can therefore select a workload profile offered by its environment.

Explain the conceptual difference:

Consumption:
Azure-managed/serverless compute.

Dedicated:
Azure provisions dedicated compute capacity associated with that workload profile.

Verify the CURRENT Azure constraints before stating that one or the other is required for private networking.

The existing guide contains strong statements about Consumption/private endpoints. Confirm whether those statements are actually correct in current Azure Container Apps before keeping them.

==================================================
LOGGING
==================================================

Configure Log Analytics at the ACA Environment level so startup failures can be diagnosed even when interactive access is unavailable.

Phase 3 should finish with explicit verification that:

- Environment provisioning state is Succeeded.
- Correct infrastructure subnet is attached.
- Internal/private configuration matches intent.
- Workload profiles are what we intended.
- Logging is operational.

==================================================
PHASE 4 — NETWORK TEST CONTAINER
==================================================

Do NOT deploy the actual MCP/application container yet.

Create a disposable Container App whose only job is network troubleshooting.

Example resource name:

`ca-nettest`

Clarify naming:

Container App:
`ca-nettest`

Image in ACR:
something like
`nettest:latest`

These are not the same thing.

The current guide imports a Microsoft CBL-Mariner image.

Explain that CBL-Mariner/Azure Linux is simply a small Microsoft Linux container image being used as a disposable troubleshooting container.

However, verify that whichever image you select actually contains the utilities required for the proposed tests.

The current guide mixes `/bin/sh`, `bash`, `/dev/tcp`, `getent`, etc. Verify the image contains those tools.

It may be better to use a troubleshooting-oriented image or clearly document tool installation if necessary.

==================================================
MAKE PHASE 4 MORE INTERACTIVE
==================================================

We discussed simplifying the current approach.

Instead of putting every DNS/TCP diagnostic inside the Container App startup command, consider:

1. Create the temporary Container App.
2. Give it a long-running process.
3. Set `--min-replicas 1`.
4. Use `az containerapp exec` to open a shell.
5. Run the diagnostic commands manually one-by-one.

This is easier to understand and troubleshoot.

A container needs a live process in order for exec to attach.

A Container App Azure resource existing does NOT guarantee there is a running Linux process to exec into.

Possible keep-alive processes:

`sleep 3600`

or

`tail -f /dev/null`

Explain that the sleep itself has nothing to do with networking. It merely prevents the test container's PID 1 from exiting.

If an application naturally runs continuously, no artificial sleep is necessary.

==================================================
AZ CONTAINERAPP EXEC
==================================================

Add an explicit example of how to enter the container interactively with something similar to:

az containerapp exec \
  -n ca-nettest \
  -g <resource-group> \
  --command /bin/sh

Verify exact current CLI syntax.

Explain:

Once exec succeeds, the operator is now inside the Linux container.

Commands such as DNS lookups or TCP tests show their output immediately in the interactive terminal.

This is different from querying Container App logs.

==================================================
DNS TESTING
==================================================

Discuss both:

`getent hosts`

and optionally:

`nslookup`

Explain the distinction:

`nslookup`
queries DNS directly.

`getent hosts`
uses the Linux system resolver path that applications commonly rely on.

For this troubleshooting scenario, `getent hosts` is especially useful because it more closely represents what the application sees.

Example:

getent hosts lab-sql-server-001.database.windows.net

Expected:
private IP such as `10.x.x.x`.

If a public IP is returned:
investigate private DNS/private endpoint wiring.

==================================================
SQL PORT TEST
==================================================

Test TCP 1433 from inside the temporary Container App.

Provide the clearest available command based on the utilities in the chosen image.

Examples might include:

nc -vz <sql-fqdn> 1433

or, if bash `/dev/tcp` is available:

timeout 5 bash -c \
'cat < /dev/null > /dev/tcp/<sql-fqdn>/1433'

Do not blindly use `/dev/tcp` if the image only has a shell that does not support it.

Explain what this proves:

- DNS worked sufficiently to locate the destination.
- TCP network connectivity to SQL port 1433 exists.

It does NOT prove that SQL authentication succeeds.

==================================================
ENTRA EGRESS TEST
==================================================

Test outbound access from the Container App toward Entra.

The previous guide used a TCP 443 check to:

`login.microsoftonline.com`

Possible examples:

nc -vz login.microsoftonline.com 443

or a curl/HTTPS test if available.

Explain that this proves outbound network reachability to Entra.

It DOES NOT prove that managed identity token acquisition itself succeeds.

Those are separate tests.

If appropriate, add a later managed-identity token test so the guide distinguishes:

1. Internet/Entra endpoint connectivity.
2. Actual managed identity authentication/token acquisition.

==================================================
ACR TEST
==================================================

Creating `ca-nettest` from an image in the private ACR is itself useful evidence that:

- The Container App can authenticate to the registry.
- The `AcrPull` role is working.
- Registry DNS/private networking is functional enough to pull layers.

The guide currently uses the SAME UAMI both for:

1. `--registry-identity`
2. `--user-assigned`

Explain why:

`--registry-identity`
tells ACA which identity to use to pull the image.

`--user-assigned`
attaches that identity to the running Container App so the application can use it at runtime.

Using the same identity is a simplicity choice.

It is also possible to use separate identities if stronger separation of responsibilities is desired.

==================================================
AZURE SQL ERROR 40615
==================================================

Keep the troubleshooting table, but expand the explanation.

If Azure SQL reports error 40615 indicating the client IP is blocked by the firewall, and the client IP is a PUBLIC address, that is an important clue.

It usually means the application reached Azure SQL through its PUBLIC endpoint rather than through the intended private endpoint path.

Do NOT simply whitelist that public client IP as the first response.

Instead, troubleshoot:

- private DNS
- private endpoint
- DNS forwarding
- routing

If SQL traffic uses the private endpoint correctly, SQL should not see the ACA public egress IP as the client reaching its public endpoint.

Tie this directly back to the `getent hosts` test.

==================================================
PHASE ORDER
==================================================

A reasonable revised sequence is:

Phase 0
Variables and prerequisites.

Phase 1
Network foundation:
- VNet
- ACA subnet
- private endpoint subnet
- subnet delegation
- SQL private endpoint
- SQL private DNS
- ACR private endpoint/DNS
- routing/NSG/egress

Phase 2
Identity:
- create UAMI
- AcrPull
- optionally/immediately create SQL external user
- grant db_datareader, and only db_datawriter if needed

Phase 3
Create private/internal Azure Container Apps Environment.
- subnet integration
- workload profiles
- logging
- verify static/private configuration

Phase 4
Create temporary `ca-nettest`.
- image pulled from ACR
- attach UAMI
- min replicas = 1
- keep-alive process
- exec into it
- DNS test
- SQL 1433 test
- Entra 443 test
- optionally managed identity token test

Phase 5
Deploy actual MCP/application Container App.

Phase 6
Verify application -> SQL authentication and queries.

Phase 7
Verify Foundry -> MCP connectivity/authentication.

Phase 8
Hardening and cleanup.

You can renumber differently if you think another layout is clearer, but keep the "each phase is a gate" model.

==================================================
ACTUAL APPLICATION / MCP SERVER
==================================================

The production Azure Container App hosts an MCP server.

So the conceptual flow is:

Foundry Agent
   |
   | MCP
   v
Azure Container App / MCP Server
   |
   | UAMI + private SQL networking
   v
Azure SQL

The UAMI discussed earlier primarily handles the Container App's own downstream access.

For example:

Container App -> ACR
Container App -> SQL

Do NOT automatically assume that the same identity mechanism authenticates:

Foundry -> MCP Server

That is a different trust relationship.

==================================================
FOUNDRY + MCP ADDITION
==================================================

The current rebuild guide mostly validates ACA -> SQL.

The revised guide needs a NEW section covering:

Azure AI Foundry -> MCP server running in ACA.

Address both:

NETWORK:
How does Foundry reach an MCP endpoint hosted in an INTERNAL/private Container Apps Environment?

AUTHENTICATION:
How does the Foundry agent authenticate to the MCP server?

Do current Microsoft documentation research before prescribing the exact configuration.

We discussed Entra-based authentication as the preferred direction, but verify the exact supported Foundry MCP authentication patterns.

Do not simply say "Foundry can reach it because everything is Azure."

Private networking still has to be deliberately established.

If Foundry-hosted/containerized agents run on Microsoft-managed infrastructure, explain the implications for reaching an MCP server that has no public ingress.

If a managed VNet, private endpoint, private link, or some other Foundry networking construct is required, spell it out.

This should become a distinct gate:

"Can Foundry actually reach the MCP endpoint privately?"

That needs to be proven separately from:

"Can the MCP server reach SQL privately?"

==================================================
FOUNDRY-HOSTED CONTAINER AGENTS
==================================================

We also discussed an important distinction.

Foundry may support containerized/hosted agents on Microsoft-managed infrastructure.

Those should NOT be confused with the Azure Container Apps resources we manually create in our own ACA Environment.

If the guide mentions Foundry-hosted container agents, clearly distinguish:

OPTION A:
Foundry hosts/manages the agent/container.

OPTION B:
We host an MCP server ourselves in our Azure Container Apps Environment and Foundry calls it.

Our rebuild guide is primarily documenting OPTION B.

Do not imply that a Foundry-hosted container will automatically appear as a Container App resource inside our custom ACA Environment unless current documentation specifically says that.

==================================================
ACR ASSUMPTION
==================================================

The current guide assumes ACR already exists.

That is acceptable, but state it explicitly.

Phase 0 should identify whether these are prerequisites:

- VNet
- ACR
- SQL Server / Azure SQL Database
- Log Analytics workspace
- relevant resource groups

We do not necessarily need to make this guide create every upstream platform resource from scratch.

The scope can remain:

"Rebuild the private Container Apps environment and MCP application around existing foundational Azure services."

==================================================
PRIVATE DNS VS ROUTING
==================================================

Make this concept extremely clear:

Private Endpoint connectivity generally involves BOTH:

1. DNS deciding to use the private endpoint IP.
2. Network routing/security allowing traffic to that IP.

Successful DNS does not prove port 1433 is reachable.

Successful port 1433 does not prove SQL authentication works.

Successful SQL authentication does not prove Foundry can reach MCP.

Each test should isolate exactly one failure domain.

==================================================
STATIC IP EXPLANATION
==================================================

Include a small explanation of ACA Environment static IP behavior because this caused confusion.

Something similar to:

If the ACA Environment is configured with public/external ingress, the environment may expose a public static IP.

If configured as an internal environment, its inbound environment/static address should be private.

The environment may still have outbound public egress IPs depending on the networking design.

Therefore a `20.x.x.x` address must be interpreted in context:
- Is it the environment's inbound/static IP?
- Or one of its outbound egress IPs?

Do not treat every 20.x ACA-related IP as evidence of the same thing.

==================================================
SECURITY / LEAST PRIVILEGE
==================================================

Default toward least privilege.

SQL:
- db_datareader only if read-only.
- Do not add db_datawriter unnecessarily.

ACR:
- AcrPull, not Contributor.

ACA:
- Do not give the UAMI unnecessary Container App RBAC roles.

Foundry -> MCP:
- Design authentication separately and grant only what is required.

==================================================
HARDENING
==================================================

Do hardening LAST, after all legitimate private paths are proven.

Examples:

- Disable public SQL access.
- Disable public ACR access.
- Disable ACR admin user.
- Remove temporary troubleshooting container.
- Delete temporary nettest image if appropriate.
- Apply deny policies only after architecture is functional.
- Establish a private operator/debug path if the ACA Environment is internal-only.

The guide currently mentions Bastion/jump host/VPN.

Keep that concept, but explain WHY:

If the ACA Environment is fully internal, operators also need a network path from which interactive troubleshooting can reach the internal environment.

Cloud Shell on the public internet should not be assumed to have private network reachability.

==================================================
STYLE OF THE NEW GUIDE
==================================================

The new Markdown should be educational, not just a wall of CLI commands.

For every phase include:

1. Goal.
2. What resources/configuration are involved.
3. Why the step matters.
4. Commands.
5. Expected result.
6. Gate / pass condition.
7. Common failure symptoms.
8. What the failure implies.

The document should allow someone troubleshooting the environment to say things like:

"DNS is proven."
"Port 1433 is proven."
"Entra egress is proven."
"Managed identity authentication is proven."
"SQL authorization is proven."
"Foundry -> MCP is proven."

Avoid vague conclusions like:

"The app still doesn't work."

==================================================
FINAL DELIVERABLE
==================================================

Create a complete replacement version of:

`rebuild-guide.md`

Do not merely provide scattered suggestions.

Preserve good material from the existing guide, but restructure and expand it based on everything above.

Before finalizing:
- Validate current CLI syntax.
- Validate current ACA subnet requirements.
- Validate workload profile/private endpoint claims.
- Validate current ACR private DNS behavior.
- Validate managed identity requirements.
- Validate Azure SQL Entra authentication setup.
- Validate current Azure AI Foundry MCP networking/authentication capabilities.

Where something depends on a design decision we have not yet made, explicitly label it:

`DECISION REQUIRED`

Do not invent a decision.

Most importantly, maintain the troubleshooting philosophy:

Every phase is a gate.
Prove one layer before adding the next one.