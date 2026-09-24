# Rebuild Guide — Private Container Apps Environment + Azure SQL

> **Scope note:** Companion to `private-network-issues.md`. This is a client architecture
> workstream and is unrelated to the `ai-chatbot-multi-llm` application code. No other file in
> this repo should be modified for this work.

**Context:** The Container Apps environment was deleted after a container app failed at startup
with a connection failure to Azure SQL. This guide rebuilds the environment and app from scratch
with private networking, verifying each dependency before the next is built.

**Design principle:** every phase is a gate. If a phase fails, the problem is in that phase — not
somewhere downstream. This turns "the app doesn't work" into a specific, named failure.

---

## Phase 0 — Set variables

```bash
RG=rg-foundry-container-apps-eus-nonprod
LOC=eastus
SUB=834bb184-249b-4d40-910e-1efcdf196905
VNET=<vnet-name>
VNET_RG=<vnet-rg>
INFRA_SUBNET=snet-aca-infra
ENV=cae-dab-eus-nonprod
APP=ca-dab-sql-agent-eus-nonprod
ACR=<acr-name>
SQL=lab-sql-server-001
SQL_RG=<sql-rg>
SQL_DB=<database-name>
UAMI=id-dab-sql-agent-eus-nonprod
LAW=<log-analytics-workspace-name>

az account set --subscription $SUB
```

---

## Phase 1 — Verify the network before building

**Do not skip this.** The original failure was network-level. Rebuilding compute will not fix a
DNS or routing problem, and you will end up rebuilding twice.

### 1.1 Infrastructure subnet

```bash
az network vnet subnet show -g $VNET_RG --vnet-name $VNET -n $INFRA_SUBNET \
  --query "{prefix:addressPrefix, delegations:delegations[].serviceName, rt:routeTable.id, nsg:networkSecurityGroup.id}"
```

| Requirement | Value |
|---|---|
| Size | `/23` or larger for workload profiles (`/27` for Consumption-only) |
| Delegation | `Microsoft.App/environments` |
| Shared with other resources | No — must be dedicated |

If a delegation lingers after the environment delete, clear and reapply it:

```bash
az network vnet subnet update -g $VNET_RG --vnet-name $VNET -n $INFRA_SUBNET --remove delegations
az network vnet subnet update -g $VNET_RG --vnet-name $VNET -n $INFRA_SUBNET \
  --delegations Microsoft.App/environments
```

### 1.2 SQL private endpoint

```bash
az sql server show -n $SQL -g $SQL_RG --query "{pna:publicNetworkAccess, fqdn:fullyQualifiedDomainName}"

az network private-endpoint list -g $SQL_RG \
  --query "[?contains(to_string(privateLinkServiceConnections[0].privateLinkServiceId),'$SQL')].{name:name, subnet:subnet.id}" -o table
```

Create one if absent:

```bash
SQLID=$(az sql server show -n $SQL -g $SQL_RG --query id -o tsv)

az network private-endpoint create -n pe-$SQL -g $SQL_RG \
  --vnet-name $VNET --subnet snet-pe \
  --private-connection-resource-id $SQLID \
  --group-id sqlServer --connection-name pe-$SQL-conn
```

### 1.3 Private DNS — most likely cause of the original failure

```bash
az network private-dns zone show -n privatelink.database.windows.net -g $VNET_RG
az network private-dns link vnet list -z privatelink.database.windows.net -g $VNET_RG -o table
az network private-dns record-set a list -z privatelink.database.windows.net -g $VNET_RG -o table
```

Create and link if missing:

```bash
az network private-dns zone create -n privatelink.database.windows.net -g $VNET_RG

az network private-dns link vnet create -n link-$VNET -g $VNET_RG \
  -z privatelink.database.windows.net --virtual-network $VNET --registration-enabled false

az network private-endpoint dns-zone-group create \
  -g $SQL_RG --endpoint-name pe-$SQL -n default \
  --private-dns-zone privatelink.database.windows.net --zone-name sql
```

**Gate:** an A record for `lab-sql-server-001` exists in the zone, and the zone is linked to
`$VNET`.

### 1.4 ACR private access

```bash
az acr show -n $ACR --query "{sku:sku.name, pna:publicNetworkAccess, dataEndpoint:dataEndpointEnabled}"
az network private-dns record-set a list -z privatelink.azurecr.io -g $VNET_RG -o table
```

Expect **two** A records: `$ACR` and `$ACR.$LOC.data`. A missing data-endpoint record lets
authentication succeed while blob layer pulls hang — a classic half-working symptom.

### 1.5 Egress path

```bash
az network vnet subnet show -g $VNET_RG --vnet-name $VNET -n $INFRA_SUBNET --query "routeTable.id"
```

If a route table is present, confirm the firewall permits:

- Port 1433 to the SQL private endpoint
- `login.microsoftonline.com:443` for Entra token acquisition
- The ACA platform dependency FQDNs (otherwise log streaming and the debug console break)

---

## Phase 2 — Create the managed identity first

Create the UAMI before the app so the same identity serves both ACR pull and SQL authentication.
Reusing one user-assigned identity avoids the common trap where a rebuild produces a new
system-assigned identity that was never granted in SQL.

```bash
az identity create -n $UAMI -g $RG -l $LOC

UAMI_ID=$(az identity show -n $UAMI -g $RG --query id -o tsv)
UAMI_PRINCIPAL=$(az identity show -n $UAMI -g $RG --query principalId -o tsv)
UAMI_CLIENT=$(az identity show -n $UAMI -g $RG --query clientId -o tsv)

ACR_ID=$(az acr show -n $ACR --query id -o tsv)

az role assignment create --assignee-object-id $UAMI_PRINCIPAL \
  --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope $ACR_ID
```

---

## Phase 3 — Create the environment

```bash
SUBNET_ID=$(az network vnet subnet show -g $VNET_RG --vnet-name $VNET -n $INFRA_SUBNET --query id -o tsv)
LAW_ID=$(az monitor log-analytics workspace show -g $RG -n $LAW --query customerId -o tsv)
LAW_KEY=$(az monitor log-analytics workspace get-shared-keys -g $RG -n $LAW --query primarySharedKey -o tsv)

az containerapp env create \
  -n $ENV -g $RG -l $LOC \
  --infrastructure-subnet-resource-id $SUBNET_ID \
  --internal-only true \
  --enable-workload-profiles true \
  --logs-destination log-analytics \
  --logs-workspace-id $LAW_ID \
  --logs-workspace-key $LAW_KEY
```

Takes 5–10 minutes.

| Setting | Why it matters |
|---|---|
| `--infrastructure-subnet-resource-id` | **Cannot be added later.** Without it there is no route to private endpoints |
| `--enable-workload-profiles` | Consumption-only environments cannot reach private endpoints |
| `--internal-only true` | No public ingress. Set only if that is the intent |
| Log Analytics at create time | So startup failures are readable without a shell |

Verify:

```bash
az containerapp env show -n $ENV -g $RG \
  --query "{state:properties.provisioningState, internal:properties.vnetConfiguration.internal, subnet:properties.vnetConfiguration.infrastructureSubnetId, profiles:properties.workloadProfiles[].name}"
```

**Gate:** `Succeeded`, `internal: true`, subnet set, workload profiles present.

---

## Phase 4 — Prove the network with a throwaway container

Do this **before** deploying the real app. It separates network problems from application
problems, and the ACR import plus successful pull simultaneously proves private registry access.

```bash
az acr import -n $ACR --source mcr.microsoft.com/cbl-mariner/base/core:2.0 -t nettest:latest

az containerapp create -n ca-nettest -g $RG --environment $ENV \
  --image $ACR.azurecr.io/nettest:latest \
  --registry-server $ACR.azurecr.io \
  --registry-identity $UAMI_ID \
  --user-assigned $UAMI_ID \
  --min-replicas 1 --max-replicas 1 \
  --command "/bin/sh" \
  --args "-c,echo '=== DNS ==='; getent hosts $SQL.database.windows.net; echo '=== 1433 ==='; timeout 5 bash -c 'cat < /dev/null > /dev/tcp/$SQL.database.windows.net/1433' && echo '1433 OPEN' || echo '1433 BLOCKED'; echo '=== AAD ==='; timeout 5 bash -c 'cat < /dev/null > /dev/tcp/login.microsoftonline.com/443' && echo 'AAD OPEN' || echo 'AAD BLOCKED'; sleep 3600"

sleep 60
az containerapp logs show -n ca-nettest -g $RG --tail 30
```

### Interpreting the result

| Output | Diagnosis | Action |
|---|---|---|
| `10.x` + `1433 OPEN` + `AAD OPEN` | Network is correct | Proceed to Phase 5 |
| Public IP returned | Private DNS zone not linked to this VNet | Return to 1.3 |
| `1433 BLOCKED` | NSG, UDR, or firewall dropping the traffic | Check route table and firewall rules |
| `AAD BLOCKED` | Egress to Entra blocked | Allow `login.microsoftonline.com:443` |
| No DNS answer | Custom DNS servers on the VNet cannot resolve | Check VNet DNS settings / forwarders |
| Image pull fails | ACR private endpoint or DNS incomplete | Return to 1.4, check the data-endpoint record |

This phase is the one most often skipped, and it is the one that converts a vague failure into a
specific one. Ten minutes here saves a rebuild.

---

## Phase 5 — Grant the identity in SQL

Connect to the database from a host that already has access, and run:

```sql
CREATE USER [id-dab-sql-agent-eus-nonprod] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [id-dab-sql-agent-eus-nonprod];
ALTER ROLE db_datawriter ADD MEMBER [id-dab-sql-agent-eus-nonprod];
```

Use the UAMI's **display name** exactly. This is the most commonly missed step in a rebuild: the
image pulls, the container starts, and authentication then fails in a way that resembles a
connection error.

Requires an Entra admin configured on the SQL server:

```bash
az sql server ad-admin list -s $SQL -g $SQL_RG -o table
```

---

## Phase 6 — Deploy the application

```bash
az containerapp create \
  -n $APP -g $RG --environment $ENV \
  --image $ACR.azurecr.io/dab-sql-agent:<tag> \
  --registry-server $ACR.azurecr.io \
  --registry-identity $UAMI_ID \
  --user-assigned $UAMI_ID \
  --target-port 8080 --ingress internal \
  --min-replicas 1 --max-replicas 3 \
  --workload-profile-name Consumption \
  --env-vars \
    "AZURE_CLIENT_ID=$UAMI_CLIENT" \
    "SQL_CONNECTION_STRING=Server=tcp:$SQL.database.windows.net,1433;Initial Catalog=$SQL_DB;Authentication=Active Directory Default;Encrypt=True;TrustServerCertificate=False;"
```

`AZURE_CLIENT_ID` is **required** when using a user-assigned identity. Without it
`DefaultAzureCredential` cannot determine which identity to present, and the resulting failure
looks like a connection error rather than an authentication one.

`--min-replicas 1` keeps a replica alive so troubleshooting is possible.

---

## Phase 7 — Verify

```bash
az containerapp revision list -n $APP -g $RG \
  --query "[].{name:name, active:properties.active, state:properties.provisioningState, healthy:properties.healthState}" -o table

az containerapp logs show -n $APP -g $RG --tail 50
```

In Log Analytics:

```kql
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "ca-dab-sql-agent-eus-nonprod"
| where TimeGenerated > ago(20m)
| project TimeGenerated, Log_s
| order by TimeGenerated asc
```

### SQL error numbers worth recognising

| Error | Meaning |
|---|---|
| `40615` | Firewall denied the client IP — the app is depending on a public path |
| `18456` | Login failed — authentication, not networking. Revisit Phase 5 |
| Timeout with no error number | Routing or port blocked — revisit Phase 1.5 |

---

## Phase 8 — Clean up and harden

```bash
az containerapp delete -n ca-nettest -g $RG --yes
az acr repository delete -n $ACR --repository nettest --yes
```

Then:

```bash
az sql server update -n $SQL -g $SQL_RG --enable-public-network false
az acr update -n $ACR --public-network-enabled false
```

Remaining hardening:

- Confirm no leftover ACR credential paths: `az acr show -n $ACR --query adminUserEnabled` should
  be `false`, and `az acr token list -r $ACR -o table` should return no non-system tokens.
- Build the Phase 4 self-check into the application image permanently, so future failures are
  diagnosable from logs alone.
- Provide operators a private path into the VNet (Bastion + jump host, or VPN with conditional DNS
  forwarding). Without one, the debug console and portal data-plane operations remain unusable and
  the public-access toggle will return.
- Apply the Azure Policy `deny` rules from §7.5 of `private-network-issues.md` **last**, once every
  legitimate path is proven.

---

## Sequencing logic

```
Phase 1  network verified
   └─ Phase 2  identity created
        └─ Phase 3  environment created
             └─ Phase 4  network proven from inside the environment
                  └─ Phase 5  SQL grant in place
                       └─ Phase 6  application deployed
                            └─ Phase 7  verified
                                 └─ Phase 8  hardened
```

Each phase gates the next. A failure at any point localises the problem to that phase rather than
leaving it distributed across the whole stack.

---

## Known traps

| Trap | Symptom | Prevention |
|---|---|---|
| Missing ACR data-endpoint DNS record | Auth succeeds, layer pull hangs | Phase 1.4 |
| Private DNS zone not linked to ACA VNet | FQDN resolves to a public IP | Phase 1.3 |
| Consumption-only environment | Cannot reach private endpoints; not fixable in place | Phase 3 |
| `infrastructureSubnetId` omitted | No route to private endpoints; requires rebuild | Phase 3 |
| UAMI not created as a SQL user | Starts, then fails authentication | Phase 5 |
| `AZURE_CLIENT_ID` not set with UAMI | Ambiguous credential; looks like a connection error | Phase 6 |
| Internal environment + Cloud Shell | `az containerapp debug` fails with a websocket error | Phase 8, private operator path |
| Entra egress blocked | Token acquisition fails, reported as a connection error | Phase 1.5 |
