import logging
from datetime import datetime, timedelta, timezone

from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, Event
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import (
    issue_registry as ir, 
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.helpers.storage import Store

from .const import DOMAIN, CONF_TIMEOUT, CONF_EXCLUDE_LABEL, DEFAULT_TIMEOUT, DEFAULT_EXCLUDE_LABEL

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Unavailable Entity Monitor from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    pending_tasks = {}  # entity_id -> asyncio.TimerHandle

    # Setup persistent store for power switch mappings (using version 1)
    store = Store(hass, 1, f"{DOMAIN}_power_switches")
    stored_data = await store.async_load()
    power_switches = stored_data.get("power_switches", {}) if stored_data else {}
    
    hass.data[DOMAIN]["power_switches"] = power_switches
    hass.data[DOMAIN]["store"] = store

    def get_config(key, default):
        return entry.options.get(key, entry.data.get(key, default))

    label_name = get_config(CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL).lower()
    label_reg = lr.async_get(hass)
    target_label_id = None
    for label in label_reg.async_list_labels():
        if label.name.lower() == label_name:
            target_label_id = label.label_id
            break
    
    if not target_label_id:
        new_label = label_reg.async_create(get_config(CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL))
        target_label_id = new_label.label_id

    hass.data[DOMAIN]["target_label_id"] = target_label_id

    def _is_entity_excluded(entity_id: str) -> bool:
        """Check if the entity possesses the pre-cached exclusion label."""
        if not target_label_id:
            return False
            
        ent_reg = er.async_get(hass)
        entity_entry = ent_reg.async_get(entity_id)
        if entity_entry and entity_entry.labels:
            return target_label_id in entity_entry.labels
        return False

    def _cleanup_entity_tracking(entity_id: str):
        """Cancel timer handle and clear repair issues."""
        if entity_id in pending_tasks:
            pending_tasks[entity_id].cancel()
            pending_tasks.pop(entity_id, None)

        issue_id = f"unavailable_{entity_id.replace('.', '_')}"
        if (DOMAIN, issue_id) in ir.async_get(hass).issues:
            try:
                ir.async_delete_issue(hass, DOMAIN, issue_id)
            except KeyError:
                pass

    async def _handle_unavailable_entity(entity_id: str, timeout_mins: int):
        current_state = hass.states.get(entity_id)
        if current_state and current_state.state in (STATE_UNAVAILABLE):
            issue_id = f"unavailable_{entity_id.replace('.', '_')}"
            
            # Prevent duplicate issue spamming if it's already registered
            current_issues = ir.async_get(hass).issues
            if (DOMAIN, issue_id) not in current_issues:
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
        new_state = event.data.get("new_state")
        
        if not new_state:
            return

        new_is_unavailable = new_state.state == STATE_UNAVAILABLE
        old_state = event.data.get("old_state")

        # If we don't know the old state, don't shortcut
        if old_state:
            old_is_unavailable = old_state.state == STATE_UNAVAILABLE

            # If both are unavailable, entity remains down -> preserve existing task/issue
            if old_is_unavailable and new_is_unavailable:
                return

            # If neither is unavailable, normal state change -> ignore completely
            if not old_is_unavailable and not new_is_unavailable:
                return

        entity_id = event.data.get("entity_id")
        _cleanup_entity_tracking(entity_id)

        if new_is_unavailable:
            if _is_entity_excluded(entity_id):
                return

            timeout_mins = get_config(CONF_TIMEOUT, DEFAULT_TIMEOUT)
            tracking_delay = timedelta(minutes=timeout_mins)
            pending_tasks[entity_id] = hass.loop.call_later(
                tracking_delay.total_seconds(),
                lambda: hass.async_create_task(_handle_unavailable_entity(entity_id, timeout_mins))
            )

    # Startup inspection scan
    timeout_mins = get_config(CONF_TIMEOUT, DEFAULT_TIMEOUT)
    now = datetime.now(timezone.utc)
    target_delay = timedelta(minutes=timeout_mins)

    for state_obj in hass.states.async_all():
        entity_id = state_obj.entity_id
        if state_obj.state in (STATE_UNAVAILABLE):
            if _is_entity_excluded(entity_id):
                continue

            elapsed = now - state_obj.last_changed

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