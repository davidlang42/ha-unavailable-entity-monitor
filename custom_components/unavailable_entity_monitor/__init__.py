import logging
from datetime import datetime, timedelta, timezone

from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, EVENT_HOMEASSISTANT_STARTED
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
    """Manages tracking, timers, and issues for unavailable entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.pending_tasks: dict[str, callable] = {}  # entity_id -> cancel callback
        self.store = Store(hass, 1, f"{DOMAIN}_power_switches")
        self.power_switches: dict[str, str] = {}
        self.target_label_id: str | None = None

    def _get_config(self, key, default):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    async def async_setup(self) -> None:
        """Initialize storage, labels, and listeners."""
        stored_data = await self.store.async_load()
        if stored_data:
            self.power_switches = stored_data.get("power_switches", {})

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

        # Initial scan and listener setup
        timeout_mins = self._get_config(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        await self._perform_startup_scan(timeout_mins)

        self.entry.async_on_unload(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self.async_state_listener)
        )

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

    def _cleanup_entity_tracking(self, entity_id: str):
        if entity_id in self.pending_tasks:
            cancel_cb = self.pending_tasks.pop(entity_id)
            cancel_cb()

        group_key = self._get_group_key(entity_id)
        issue_id = f"unavailable_{group_key}"
        if (DOMAIN, issue_id) in ir.async_get(self.hass).issues:
            try:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            except KeyError:
                pass

    async def _handle_unavailable_entity(self, entity_id: str, timeout_mins: int):
        current_state = self.hass.states.get(entity_id)
        if current_state and current_state.state == STATE_UNAVAILABLE:
            ent_reg = er.async_get(self.hass)
            entity_entry = ent_reg.async_get(entity_id)
            device_id = entity_entry.device_id if entity_entry else None
            group_key = f"device_{device_id}" if device_id else f"entity_{entity_id.replace('.', '_')}"
            issue_id = f"unavailable_{group_key}"
            
            if (DOMAIN, issue_id) not in ir.async_get(self.hass).issues:
                issue_data = {"entity_id": entity_id}
                display_name = entity_id
                if device_id:
                    issue_data["device_id"] = device_id
                    dev_reg = dr.async_get(self.hass)
                    device_entry = dev_reg.async_get(device_id)
                    if device_entry:
                        display_name = device_entry.name_by_user or device_entry.name or entity_id

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
        self._cleanup_entity_tracking(entity_id)

        if new_is_unavailable:
            if self._is_entity_excluded(entity_id):
                return

            timeout_mins = self._get_config(CONF_TIMEOUT, DEFAULT_TIMEOUT)
            delay = timedelta(minutes=timeout_mins)
            
            async def _timer_callback(_):
                await self._handle_unavailable_entity(entity_id, timeout_mins)

            self.pending_tasks[entity_id] = async_call_later(self.hass, delay.total_seconds(), _timer_callback)

    async def _perform_startup_scan(self, timeout_mins: int):
        async def _scan_after_started(event=None):
            now = datetime.now(timezone.utc)
            target_delay = timedelta(minutes=timeout_mins)

            # 1. Gather all entities that are currently unavailable and not excluded
            unavailable_entities = [
                state_obj.entity_id
                for state_obj in self.hass.states.async_all()
                if state_obj.state == STATE_UNAVAILABLE and not self._is_entity_excluded(state_obj.entity_id)
            ]

            if not unavailable_entities:
                return

            # 2. Query recorder database in a background thread
            def _fetch_history():
                from homeassistant.components.recorder import history
                # A 1-day lookback is highly efficient and more than enough for a 10-minute timeout
                start_time = now - timedelta(days=1)
                return history.get_significant_states(
                    self.hass,
                    start_time=start_time,
                    end_time=now,
                    entity_ids=unavailable_entities,
                    include_start_time_state=True,
                    no_attributes=True, # Vastly reduces memory usage for the query
                )

            try:
                history_data = await self.hass.async_add_executor_job(_fetch_history)
            except Exception as e:
                _LOGGER.error("Failed to fetch history for Unavailable Entity Monitor: %s", e)
                history_data = {}

            # 3. Process history to find true downtime
            for entity_id in unavailable_entities:
                current_state_obj = self.hass.states.get(entity_id)
                # Verify it didn't come back online while we were querying the DB
                if not current_state_obj or current_state_obj.state != STATE_UNAVAILABLE:
                    continue

                actual_last_changed = current_state_obj.last_changed
                states = history_data.get(entity_id, [])

                # Traverse backwards to find the oldest contiguous unavailable state
                found_time = None
                for state in reversed(states):
                    if state.state == STATE_UNAVAILABLE:
                        found_time = state.last_changed
                    else:
                        break # We hit a state where it was available

                if found_time:
                    actual_last_changed = found_time

                elapsed = now - actual_last_changed
                
                # 4. Fire issue immediately or set remaining timer
                if elapsed >= target_delay:
                    await self._handle_unavailable_entity(entity_id, timeout_mins)
                else:
                    remaining = target_delay - elapsed
                    async def _timer_callback(_, eid=entity_id, t=timeout_mins):
                        await self._handle_unavailable_entity(eid, t)
                    
                    if entity_id not in self.pending_tasks:
                        self.pending_tasks[entity_id] = async_call_later(
                            self.hass, remaining.total_seconds(), _timer_callback
                        )

        # Trigger immediately if HA is already running (e.g., config reload),
        # otherwise queue it for when the boot sequence finishes.
        if self.hass.is_running:
            self.hass.async_create_task(_scan_after_started())
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _scan_after_started)

    async def async_unload(self):
        for cancel_cb in self.pending_tasks.values():
            cancel_cb()
        self.pending_tasks.clear()