# Unavailable Entity Monitor for Home Assistant

An advanced, highly performant custom integration for Home Assistant that automatically monitors all entities for unavailability (or unknown states), leverages the native **Repairs** system, and gives you interactive fix flows directly in the UI.

## Features
- **Zero Manual Maintenance:** Automatically tracks every entity in your instance. Newly added or renamed entities are monitored instantly.
- **Native Repairs Integration:** Registers issues under `Settings > System > Repairs` instead of cluttering your notification drawer. Issues auto-dismiss when entities come back online.
- **Configurable Timeout:** Easily set your desired offline threshold (e.g., 10 minutes) via the UI.
- **Label Exclusion Support:** Automatically bypasses any entities tagged with your chosen exclusion label (default: `Maybe Unavailable`).
- **Interactive Fix Actions:**
  - **Exclude Entity:** Safely tag the entity with your exclusion label directly from the repair card with a confirmation prompt.
  - **Power Cycle:** Choose a switch entity to cycle power (OFF for 10s, then ON). Remembers your choice for future occurrences and requires a confirmation step showing the switch's current state and last-changed timestamp.
- **High Performance:** Implements $O(1)$ in-memory caching to support large Home Assistant instances with thousands of entities without lagging the event loop.
- **UI Configuration & Options Flow:** Easily change settings at any time from **Settings > Devices & Services**.

## Installation via HACS
1. Open HACS in your Home Assistant instance.
2. Click the three dots in the top right corner and select **Custom repositories**.
3. Paste the URL of your GitHub repository, select category **Integration**, and click **Add**.
4. Find **Unavailable Entity Monitor** in HACS and click **Download**.
5. Restart Home Assistant.
6. Go to **Settings > Devices & Services > Add Integration** and search for **Unavailable Entity Monitor**.