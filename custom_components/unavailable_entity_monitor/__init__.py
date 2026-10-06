import logging
from datetime import timedelta

from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, Event
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import (
    issue_registry as ir, 
    entity_registry as er,
    device_registry as dr,
    label_registry as lr,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import DOMAIN, CONF_TIMEOUT, CONF_EXCLUDE_LABEL, DEFAULT_TIMEOUT, DEFAULT_EXCLUDE_LABEL

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Unavailable Entity Monitor from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    manager = UnavailableEntityMonitorManager(hass, entry)
    await manager.async_setup()
    
    hass.data[DOMAIN]["manager"] = manager
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    manager = hass.data.get(DOMAIN, {}).get("manager")
    if manager:
        await manager.async_unload()
    return True

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


class UnavailableEntityMonitorManager:
    """Manages tracking, timers, and issues for unavailable entities with storage persistence."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.pending_tasks: dict[str, callable] = {}  # entity_id -> cancel callback
        self.store = Store(hass, 1, f"{DOMAIN}_power_switches")
        self.power_switches: dict[str, str] = {}
        self.active_issues: dict[str, dict] = {}
        self.target_label_id: str | None = None

    def _get_config(self, key, default):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    async def async_setup(self) -> None:
        """Initialize storage, labels, and restore active issues."""
        stored_data = await self.store.async_load()
        if stored_data:
            self.power_switches = stored_data.get("power_switches", {})
            self.active_issues = stored_data.get("active_issues", {})

        self.hass.data[DOMAIN]["power_switches"] = self.power_switches
        self.hass.data[DOMAIN]["store"] = self.store

        label_name = self._get_config(CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL).lower()
        label_reg = lr.async_get(self.hass)
        
        for label in label_reg.async_list_labels():
            if label.name.lower() == label_name:
                self.target_label_id = label.label_id
                break
        
        if not self.target_label_id:
            new_label = label_reg.async_create(self._get_config(CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL))
            self.target_label_id = new_label.label_id

        self.hass.data[DOMAIN]["target_label_id"] = self.target_label_id

        # Restore persisted active issues instantly on startup
        timeout_mins = self._get_config(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        await self._restore_persisted_issues(timeout_mins)

        self.entry.async_on_unload(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self.async_state_listener)
        )

    async def _save_storage(self) -> None:
        """Save power switches and active issues to persistent storage."""
        await self.store.async_save({
            "power_switches": self.power_switches,
            "active_issues": self.active_issues,
        })

    def _is_entity_excluded(self, entity_id: str) -> bool:
        if not self.target_label_id:
            return False
        ent_reg = er.async_get(self.hass)
        entity_entry = ent_reg.async_get(entity_id)
        return bool(entity_entry and entity_entry.labels and self.target_label_id in entity_entry.labels)

    def _get_group_key(self, entity_id: str) -> str:
        ent_reg = er.async_get(self.hass)
        entity_entry = ent_reg.async_get(entity_id)
        if entity_entry and entity_entry.device_id:
            return f"device_{entity_entry.device_id}"
        return f"entity_{entity_id.replace('.', '_')}"

    async def _cleanup_entity_tracking(self, entity_id: str):
        if entity_id in self.pending_tasks:
            cancel_cb = self.pending_tasks.pop(entity_id)
            cancel_cb()

        group_key = self._get_group_key(entity_id)
        issue_id = f"unavailable_{group_key}"
        
        if group_key in self.active_issues:
            self.active_issues.pop(group_key)
            await self._save_storage()

        if (DOMAIN, issue_id) in ir.async_get(self.hass).issues:
            try:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            except KeyError:
                pass

    async def _handle_unavailable_entity(self, entity_id: str, timeout_mins: int):
        current_state = self.hass.states.get(entity_id)
        if current_state and current_state.state == STATE_UNAVAILABLE:
            if self._is_entity_excluded(entity_id):
                return

            ent_reg = er.async_get(self.hass)
            entity_entry = ent_reg.async_get(entity_id)
            device_id = entity_entry.device_id if entity_entry else None
            group_key = f"device_{device_id}" if device_id else f"entity_{entity_id.replace('.', '_')}"
            issue_id = f"unavailable_{group_key}"
            
            issue_data = {"entity_id": entity_id}
            display_name = entity_id
            if device_id:
                issue_data["device_id"] = device_id
                dev_reg = dr.async_get(self.hass)
                device_entry = dev_reg.async_get(device_id)
                if device_entry:
                    display_name = device_entry.name_by_user or device_entry.name or entity_id

            self.active_issues[group_key] = {
                "entity_id": entity_id,
                "device_id": device_id,
                "display_name": display_name,
                "timeout_mins": timeout_mins,
            }
            await self._save_storage()

            if (DOMAIN, issue_id) not in ir.async_get(self.hass).issues:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=True,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="entity_unavailable",
                    translation_placeholders={
                        "entity_id": display_name,
                        "state": current_state.state,
                        "timeout": str(timeout_mins),
                    },
                    data=issue_data,
                )
        self.pending_tasks.pop(entity_id, None)

    async def _restore_persisted_issues(self, timeout_mins: int):
        """Restore active issues from storage instantly on startup."""
        if not self.active_issues:
            return

        to_remove = []
        for group_key, info in list(self.active_issues.items()):
            entity_id = info.get("entity_id")
            device_id = info.get("device_id")
            display_name = info.get("display_name", entity_id)
            issue_id = f"unavailable_{group_key}"

            current_state = self.hass.states.get(entity_id)
            if current_state and current_state.state == STATE_UNAVAILABLE:
                if self._is_entity_excluded(entity_id):
                    to_remove.append(group_key)
                    continue

                issue_data = {"entity_id": entity_id}
                if device_id:
                    issue_data["device_id"] = device_id

                if (DOMAIN, issue_id) not in ir.async_get(self.hass).issues:
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        issue_id,
                        is_fixable=True,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key="entity_unavailable",
                        translation_placeholders={
                            "entity_id": display_name,
                            "state": current_state.state,
                            "timeout": str(timeout_mins),
                        },
                        data=issue_data,
                    )
            else:
                to_remove.append(group_key)
                if (DOMAIN, issue_id) in ir.async_get(self.hass).issues:
                    try:
                        ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                    except KeyError:
                        pass

        if to_remove:
            for gk in to_remove:
                self.active_issues.pop(gk, None)
            await self._save_storage()

    async def async_state_listener(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if not new_state:
            return

        new_is_unavailable = new_state.state == STATE_UNAVAILABLE
        old_state = event.data.get("old_state")

        if old_state:
            old_is_unavailable = old_state.state == STATE_UNAVAILABLE
            if old_is_unavailable and new_is_unavailable:
                return
            if not old_is_unavailable and not new_is_unavailable:
                return

        entity_id = event.data.get("entity_id")
        await self._cleanup_entity_tracking(entity_id)

        if new_is_unavailable:
            if self._is_entity_excluded(entity_id):
                return

            timeout_mins = self._get_config(CONF_TIMEOUT, DEFAULT_TIMEOUT)
            delay = timedelta(minutes=timeout_mins)
            
            async def _timer_callback(_):
                await self._handle_unavailable_entity(entity_id, timeout_mins)

            self.pending_tasks[entity_id] = async_call_later(self.hass, delay.total_seconds(), _timer_callback)

    async def async_unload(self):
        for cancel_cb in self.pending_tasks.values():
            cancel_cb()
        self.pending_tasks.clear()