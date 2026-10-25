# Status-feed fixtures

Recorded copies of the public status feeds, served by `busybox httpd` in
`tests/fixtures.sh` so the region filters can be tested with no network.

Captured 2026-10-25 from:

| Fixture path                                     | Source                                                      |
| ------------------------------------------------ | ----------------------------------------------------------- |
| `snowflake/api/v2/status.json`                   | <https://status.snowflake.com/api/v2/status.json>           |
| `snowflake/api/v2/components.json`               | <https://status.snowflake.com/api/v2/components.json>       |
| `databricks/1.0/status/5d49ec10226b9e13cb6a422e` | <https://api.status.io/1.0/status/5d49ec10226b9e13cb6a422e> |
| `hashicorp/proxy/status.hashicorp.com`           | <https://status.hashicorp.com/proxy/status.hashicorp.com>   |
| `web/index.html`                                 | stand-in for the web check                                  |

## healthy/

Trimmed with jq so each file keeps the real shape but only the parts the checks read:

```sh
# Snowflake status.json: live file, forced healthy
jq '.status = {"indicator":"none","description":"All Systems Operational"}'

# Snowflake components.json: three Azure region groups, forced operational
jq '{page, components: [.components[]
      | select(.name=="Azure - East US (Virginia)" or .name=="Azure - East US 2 (Virginia)"
               or .name=="Azure - West Europe (Netherlands)")
      | .status="operational"]}'

# Databricks (status.io): Compute and Databricks SQL in three regions, forced Operational
jq '{result: {status_overall: (.result.status_overall | .status="Operational" | .status_code=100),
      status: [.result.status[] | select(.name=="Compute" or .name=="Databricks SQL")
        | .status="Operational" | .status_code=100
        | .containers |= map(select(.name=="East US" or .name=="East US 2" or .name=="West Europe")
            | .status="Operational" | .status_code=100)],
      incidents: [], maintenance: .result.maintenance}}'

# HashiCorp (incident.io): the three components the checks name, nothing affected
jq '{summary: {id: .summary.id, name: .summary.name,
      components: [.summary.components[] | select(.id=="01K7FBWXHZYYWNBR12D9AQQ7CE"
        or .id=="01K7FBWXHZDC8NV69JXVFBNESW" or .id=="01K7FBWXHZSMHNQWYKSQFKHVHN")],
      affected_components: [], ongoing_incidents: [], scheduled_maintenances: []}}'
```

## degraded/

`healthy/` with these edits, and no `web/` directory (the web check gets a 404):

- Snowflake: `indicator` `minor`; group `Azure - East US 2 (Virginia)` `degraded_performance`.
- Databricks: overall and `Compute` `Degraded Performance` (300); only `Compute` / `East US`
  degraded, `Compute` / `East US 2` stays `Operational`.
- HashiCorp: HCP Vault Dedicated Azure-East US 2 (`01K7FBWXHZDC8NV69JXVFBNESW`) in
  `affected_components`; one ongoing incident.

## Assumed shape

The HashiCorp feed had no affected components when it was captured. The entry shape
`{"component_id": "...", "status": "..."}` follows the incident.io widget format and is not yet
confirmed by a real incident. Replace the degraded entry with a real one when an incident is
observed.
