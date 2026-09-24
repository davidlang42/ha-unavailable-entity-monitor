import logging
from datetime import datetime, timedelta, timezone

from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNKNOWN, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, Event
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import (
    issue_registry as ir, 
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.helpers.storage import Store, STORAGE_VERSION

from .const import DOMAIN, CONF_TIMEOUT, CONF_EXCLUDE_LABEL, DEFAULT_TIMEOUT, DEFAULT_EXCLUDE_LABEL

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Unavailable Entity Monitor from a config entry with high performance caching."""
    hass.data.setdefault(DOMAIN, {})
    
    pending_tasks = {}  # entity_id -> asyncio.TimerHandle
    excluded_entity_ids = set()

    # Setup persistent store for power switch mappings
    store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_power_switches")
    stored_data = await store.async_load()
    power_switches = stored_data.get("power_switches", {}) if stored_data else {}
    
    hass.data[DOMAIN]["power_switches"] = power_switches
    hass.data[DOMAIN]["store"] = store

    def get_config(key, default):
        return entry.options.get(key, entry.data.get(key, default))

    def _refresh_exclusion_cache():
        """Compute and cache excluded entities to ensure O(1) lookup performance."""
        nonlocal excluded_entity_ids
        excluded_entity_ids.clear()
        
        label_name = get_config(CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL).lower()
        ent_reg = er.async_get(hass)
        label_reg = lr.async_get(hass)
        
        target_label_ids = {
            label.label_id for label in label_reg.async_list_labels()
            if label.name.lower() == label_name
        }
        
        if target_label_ids:
            for ent_entry in ent_reg.entities.values():
                if ent_entry.labels.intersection(target_label_ids):
                    excluded_entity_ids.add(ent_entry.entity_id)

    _refresh_exclusion_cache()

    def _cleanup_entity_tracking(entity_id: str):
        """Cancel timer handle and clear repair issues."""
        if entity_id in pending_tasks:
            pending_tasks[entity_id].cancel()
            pending_tasks.pop(entity_id, None)

        issue_id = f"unavailable_{entity_id.replace('.', '_')}"
        try:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
        except KeyError:
            pass

    async def _handle_unavailable_entity(entity_id: str, timeout_mins: int):
        current_state = hass.states.get(entity_id)
        if current_state and current_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            issue_id = f"unavailable_{entity_id.replace('.', '_')}"
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="entity_unavailable",
                translation_placeholders={
                    "entity_id": entity_id,
                    "state": current_state.state,
                    "timeout": str(timeout_mins),
                },
                data={"entity_id": entity_id},
            )
        pending_tasks.pop(entity_id, None)

    async def async_state_listener(event: Event) -> None:
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        
        if not new_state:
            return

        # O(1) performance check against pre-calculated exclusion set
        if entity_id in excluded_entity_ids:
            return

        timeout_mins = get_config(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        state_val = new_state.state

        if state_val in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _cleanup_entity_tracking(entity_id)

            tracking_delay = timedelta(minutes=timeout_mins)
            pending_tasks[entity_id] = hass.loop.call_later(
                tracking_delay.total_seconds(),
                lambda: hass.async_create_task(_handle_unavailable_entity(entity_id, timeout_mins))
            )
        else:
            _cleanup_entity_tracking(entity_id)

    # Startup inspection scan
    timeout_mins = get_config(CONF_TIMEOUT, DEFAULT_TIMEOUT)
    now = datetime.now(timezone.utc)

    for state_obj in hass.states.async_all():
        entity_id = state_obj.entity_id
        if state_obj.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            if entity_id in excluded_entity_ids:
                continue

            elapsed = now - state_obj.last_changed
            target_delay = timedelta(minutes=timeout_mins)

            if elapsed >= target_delay:
                await _handle_unavailable_entity(entity_id, timeout_mins)
            else:
                remaining_delay = target_delay - elapsed
                if entity_id not in pending_tasks:
                    pending_tasks[entity_id] = hass.loop.call_later(
                        remaining_delay.total_seconds(),
                        lambda eid=entity_id, t=timeout_mins: hass.async_create_task(_handle_unavailable_entity(eid, t))
                    )

    # Event listeners
    entry.async_on_unload(hass.bus.async_listen(EVENT_STATE_CHANGED, async_state_listener))
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # Cleanup function when unloading/reloading integration
    async def async_unload_cleanup():
        for task in pending_tasks.values():
            task.cancel()
        pending_tasks.clear()

    entry.async_on_unload(async_unload_cleanup)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)