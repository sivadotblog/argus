{
  "summary": {
    "id": "01K7FBWXHZPP52EWA3EGJ2SNVA",
    "name": "HashiCorp Services",
    "components": [
      {
        "id": "01K7FBWXHZYYWNBR12D9AQQ7CE",
        "name": "Azure-East US",
        "status_page_id": "01K7FBWXHZPP52EWA3EGJ2SNVA"
      },
      {
        "id": "01K7FBWXHZDC8NV69JXVFBNESW",
        "name": "Azure-East US 2",
        "status_page_id": "01K7FBWXHZPP52EWA3EGJ2SNVA"
      },
      {
        "id": "01K7FBWXHZSMHNQWYKSQFKHVHN",
        "name": "HCP Terraform",
        "status_page_id": "01K7FBWXHZPP52EWA3EGJ2SNVA"
      }
    ],
    "affected_components": [
      {
        "component_id": "01K7FBWXHZDC8NV69JXVFBNESW",
        "status": "degraded_performance"
      }
    ],
    "ongoing_incidents": [
      {
        "id": "fixture",
        "name": "Fixture incident",
        "status": "investigating"
      }
    ],
    "scheduled_maintenances": []
  }
}
