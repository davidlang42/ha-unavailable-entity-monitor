import asyncio
import logging
from datetime import datetime, timezone
import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, label_registry as lr, issue_registry as ir
from homeassistant.helpers import selector
from .const import DOMAIN, CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL

_LOGGER = logging.getLogger(__name__)

async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None) -> RepairsFlow:
    """Create the fix flow for an unavailable entity issue."""
    return UnavailableEntityRepairFlow(data)

class UnavailableEntityRepairFlow(RepairsFlow):
    """Handler for the repair flow dialogs."""

    def __init__(self, data: dict | None) -> None:
        self.data = data or {}
        self.entity_id = self.data.get("entity_id")
        self._selected_switch = None

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Present options via a form choice instead of an unsupported menu."""
        if user_input is not None:
            action = user_input.get("action")
            if action == "exclude_entity":
                return await self.async_step_exclude_entity()
            elif action == "power_cycle":
                return await self.async_step_power_cycle()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("action", default="power_cycle"): vol.In({
                    "power_cycle": "Power cycle a corresponding switch",
                    "exclude_entity": "Add to exclusion list (ignore future unavailability)",
                })
            }),
            description_placeholders={"entity_id": self.entity_id},
        )

    def _get_configured_label_name(self) -> str:
        """Helper to fetch the configured exclusion label name."""
        entry = self.hass.config_entries.async_entries(DOMAIN)
        if entry:
            return entry[0].options.get(CONF_EXCLUDE_LABEL, entry[0].data.get(CONF_EXCLUDE_LABEL, DEFAULT_EXCLUDE_LABEL))
        return DEFAULT_EXCLUDE_LABEL

    async def async_step_exclude_entity(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Ask for confirmation before adding the exclusion label, then clear the repair."""
        if user_input is not None:
            if self.entity_id:
                ent_reg = er.async_get(self.hass)
                entity_entry = ent_reg.async_get(self.entity_id)
                
                if entity_entry:
                    label_name = self._get_configured_label_name()
                    label_reg = lr.async_get(self.hass)
                    target_label_id = next(
                        (l_id for l_id, l_obj in label_reg.async_list_labels() if l_obj.name.lower() == label_name.lower()),
                        None
                    )
                    
                    if not target_label_id:
                        new_label = label_reg.async_create(label_name)
                        target_label_id = new_label.label_id

                    current_labels = set(entity_entry.labels)
                    if target_label_id not in current_labels:
                        current_labels.add(target_label_id)
                        ent_reg.async_update_entity(self.entity_id, labels=current_labels)

            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return self.async_create_entry(title="", data={})

        label_name = self._get_configured_label_name()
        return self.async_show_form(
            step_id="exclude_entity",
            data_schema=vol.Schema({}),
            description_placeholders={
                "label_name": label_name,
                "entity_id": self.entity_id,
            },
        )

    async def async_step_power_cycle(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Step 1: Ask the user to select a switch entity, pre-filling the last known choice."""
        if user_input is not None:
            self._selected_switch = user_input.get("switch_entity")
            return await self.async_step_confirm_power_cycle()

        domain_data = self.hass.data.get(DOMAIN, {})
        power_switches = domain_data.get("power_switches", {})
        default_switch = power_switches.get(self.entity_id)

        return self.async_show_form(
            step_id="power_cycle",
            data_schema=vol.Schema({
                vol.Required("switch_entity", default=default_switch): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                )
            }),
        )

    async def async_step_confirm_power_cycle(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """Step 2: Show switch state and last changed time, then execute upon confirmation."""
        if user_input is not None:
            switch_entity_id = self._selected_switch
            if switch_entity_id and self.entity_id:
                domain_data = self.hass.data.get(DOMAIN, {})
                power_switches = domain_data.setdefault("power_switches", {})
                power_switches[self.entity_id] = switch_entity_id

                store = domain_data.get("store")
                if store:
                    await store.async_save({"power_switches": power_switches})

                async def _run_power_cycle():
                    _LOGGER.info("Power cycling %s via user-selected switch %s", self.entity_id, switch_entity_id)
                    await self.hass.services.async_call("switch", "turn_off", {"entity_id": switch_entity_id}, blocking=True)
                    await asyncio.sleep(10)
                    await self.hass.services.async_call("switch", "turn_on", {"entity_id": switch_entity_id}, blocking=True)

                self.hass.async_create_task(_run_power_cycle())

            return self.async_create_entry(title="", data={})

        switch_state = self.hass.states.get(self._selected_switch)
        state_str = switch_state.state if switch_state else "unknown"
        
        time_str = "an unknown time"
        if switch_state and switch_state.last_changed:
            diff = datetime.now(timezone.utc) - switch_state.last_changed
            seconds = int(diff.total_seconds())
            if seconds < 60:
                time_str = f"{seconds} seconds ago"
            elif seconds < 3600:
                time_str = f"{seconds // 60} minutes ago"
            elif seconds < 86400:
                time_str = f"{seconds // 3600} hours ago"
            else:
                time_str = f"{seconds // 86400} days ago"

        return self.async_show_form(
            step_id="confirm_power_cycle",
            data_schema=vol.Schema({}),
            description_placeholders={
                "switch_id": self._selected_switch,
                "state": state_str,
                "time_ago": time_str,
            },
        )