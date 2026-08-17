"""Characterization tests for the Room Climate Boost blueprint.

These lock in the blueprint's *current* behavior as an executable spec, so the
symmetric-heating refactor can proceed without silently regressing cooling. Each
test instantiates the real blueprint as an automation (HA core's blueprint test
pattern), drives entity states, and asserts the ``fan.set_percentage`` the
automation commands.

The blueprint is YAML, so it does not move the ``custom_components`` coverage
number -- these tests gate correctness, not coverage.
"""

from __future__ import annotations

import pathlib
import shutil
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component

BLUEPRINT_SRC = (
    pathlib.Path(__file__).parents[1]
    / "blueprints/automation/smartcocoon/room_climate_boost.yaml"
)
BLUEPRINT_REL = "smartcocoon/room_climate_boost.yaml"

THERMOSTAT = "climate.t"
ROOM = "sensor.room"
FAN = "fan.test"

DEFAULT_INPUTS = {
    "thermostat": THERMOSTAT,
    "room_sensor": ROOM,
    "booster_fan": FAN,
}

# A future date (real "today" is 2026-08-xx) so freezing never moves the HA
# scheduler backwards. Day hour = 12 (outside 22:00-08:00), night hour = 23.
_DAY = "2026-09-15 12:00:00"
_NIGHT = "2026-09-15 23:00:00"


def _install_blueprint(hass: HomeAssistant) -> None:
    dest = pathlib.Path(
        hass.config.path("blueprints/automation/smartcocoon/room_climate_boost.yaml")
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT_SRC, dest)


async def _setup(hass: HomeAssistant, inputs: dict[str, Any] | None = None) -> None:
    """Install the blueprint and instantiate it as a single automation."""
    merged = {**DEFAULT_INPUTS, **(inputs or {})}
    # Pin the clock's timezone so frozen UTC times map predictably to the
    # blueprint's day/night window (the test harness defaults to US/Pacific).
    await hass.config.async_set_time_zone("UTC")
    _install_blueprint(hass)
    assert await async_setup_component(
        hass,
        "automation",
        {"automation": {"use_blueprint": {"path": BLUEPRINT_REL, "input": merged}}},
    )
    await hass.async_block_till_done()


async def _trigger(
    hass: HomeAssistant,
    calls: list[ServiceCall],
    freezer: FrozenDateTimeFactory,
    *,
    hvac_action: str,
    room: float,
    mode: str = "cool",
    when: str = _DAY,
    temperature: float | None = None,
    target_temp_high: float | None = None,
    target_temp_low: float | None = None,
    current_temperature: float | None = None,
    fan_state: str = "on",
    fan_pct: int = 50,
    helpers: dict[str, str] | None = None,
) -> None:
    """Set the world, then change the room sensor to fire the automation."""
    freezer.move_to(when)
    for entity, state in (helpers or {}).items():
        hass.states.async_set(entity, state)

    attrs: dict[str, Any] = {"hvac_action": hvac_action}
    for key, value in (
        ("temperature", temperature),
        ("target_temp_high", target_temp_high),
        ("target_temp_low", target_temp_low),
        ("current_temperature", current_temperature),
    ):
        if value is not None:
            attrs[key] = value
    hass.states.async_set(THERMOSTAT, mode, attrs)
    hass.states.async_set(
        FAN, fan_state, {} if fan_state == "off" else {"percentage": fan_pct}
    )
    hass.states.async_set(ROOM, "-999")  # prime
    await hass.async_block_till_done()
    calls.clear()

    hass.states.async_set(ROOM, str(room))  # the asserted trigger
    await hass.async_block_till_done()


def _last_pct(calls: list[ServiceCall]) -> int:
    assert calls, "expected a fan.set_percentage call, got none"
    return int(calls[-1].data["percentage"])


# --------------------------------------------------------------------------- #
# Smoke: the blueprint is a valid, loadable blueprint.
# --------------------------------------------------------------------------- #
async def test_blueprint_instantiates(hass: HomeAssistant) -> None:
    """The blueprint loads and produces an automation entity."""
    await _setup(hass)
    automations = hass.states.async_entity_ids("automation")
    assert automations, "blueprint did not instantiate an automation"
    state = hass.states.get(automations[0])
    assert state is not None
    assert state.state != "unavailable"


# --------------------------------------------------------------------------- #
# Cooling tier ladder.
# --------------------------------------------------------------------------- #
async def test_cooling_boost(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Actively cooling and >= boost threshold over setpoint -> boost (100)."""
    await _setup(hass)
    await _trigger(
        hass, calls, freezer, hvac_action="cooling", target_temp_high=22.0, room=24.0
    )
    assert _last_pct(calls) == 100


async def test_cooling_assist_daytime(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Cooling within threshold, daytime -> assist (17)."""
    await _setup(hass)
    await _trigger(
        hass, calls, freezer, hvac_action="cooling", target_temp_high=22.0, room=22.5
    )
    assert _last_pct(calls) == 17


async def test_fan_only_circulate_daytime(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Fan-only (blower on, AC idle), room over setpoint -> circulate (60)."""
    await _setup(hass)
    await _trigger(
        hass, calls, freezer, hvac_action="fan", target_temp_high=22.0, room=24.0
    )
    assert _last_pct(calls) == 60


async def test_baseline_when_idle(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Idle, room below setpoint -> baseline whisper (8)."""
    await _setup(hass)
    await _trigger(
        hass, calls, freezer, hvac_action="idle", target_temp_high=22.0, room=20.0
    )
    assert _last_pct(calls) == 8


# --------------------------------------------------------------------------- #
# HVAC-off equalizer (currently one-directional: only when room warmer than house).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("room", "expected"),
    [(24.1, 100), (23.2, 60), (22.6, 33), (22.2, 8)],
)
async def test_equalizer_tiers(
    hass: HomeAssistant,
    calls: list[ServiceCall],
    freezer: FrozenDateTimeFactory,
    room: float,
    expected: int,
) -> None:
    """No cool setpoint, blower circulating, home: equalize toward house temp."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        mode="fan_only",  # not 'cool' and no target_temp_high => cool_sp is None
        hvac_action="fan",
        current_temperature=22.0,  # house reference
        room=room,
    )
    assert _last_pct(calls) == expected


# --------------------------------------------------------------------------- #
# Night suppression.
# --------------------------------------------------------------------------- #
async def test_night_suppresses_cooling_assist(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """At night, cooling-within-threshold collapses to baseline."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        when=_NIGHT,
        hvac_action="cooling",
        target_temp_high=22.0,
        room=22.5,
    )
    assert _last_pct(calls) == 8


async def test_night_suppresses_fan_circulate(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """At night, fan-only circulate collapses to baseline."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        when=_NIGHT,
        hvac_action="fan",
        target_temp_high=22.0,
        room=24.0,
    )
    assert _last_pct(calls) == 8


async def test_night_still_allows_cooling_boost(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """At night, an active-cooling boost is still allowed (not night-gated)."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        when=_NIGHT,
        hvac_action="cooling",
        target_temp_high=22.0,
        room=24.0,
    )
    assert _last_pct(calls) == 100


# --------------------------------------------------------------------------- #
# Caps and floor.
# --------------------------------------------------------------------------- #
async def test_night_max_cap(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """night_max_speed caps an otherwise-100 boost during the night window."""
    await _setup(hass, {"night_max_speed": 8})
    await _trigger(
        hass,
        calls,
        freezer,
        when=_NIGHT,
        hvac_action="cooling",
        target_temp_high=22.0,
        room=24.0,
    )
    assert _last_pct(calls) == 8


async def test_max_speed_cap(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """max_speed caps the normal automatic speed."""
    await _setup(hass, {"max_speed": 60})
    await _trigger(
        hass, calls, freezer, hvac_action="cooling", target_temp_high=22.0, room=24.0
    )
    assert _last_pct(calls) == 60


async def test_speed_floor(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """An external speed floor raises an otherwise-baseline speed."""
    await _setup(hass, {"speed_floor_entity": "input_number.floor"})
    await _trigger(
        hass,
        calls,
        freezer,
        hvac_action="idle",
        target_temp_high=22.0,
        room=20.0,
        helpers={"input_number.floor": "40"},
    )
    assert _last_pct(calls) == 40


# --------------------------------------------------------------------------- #
# Force-max override and manual-off handling.
# --------------------------------------------------------------------------- #
async def test_force_max_overrides_everything(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """force_max boolean ON -> 100 regardless of an otherwise-idle state."""
    await _setup(hass, {"force_max_boolean": "input_boolean.max"})
    await _trigger(
        hass,
        calls,
        freezer,
        hvac_action="idle",
        target_temp_high=22.0,
        room=20.0,
        helpers={"input_boolean.max": "on"},
    )
    assert _last_pct(calls) == 100


async def test_manual_off_respected(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """A manually-off fan is left alone (no command) without a force flag."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        hvac_action="cooling",
        target_temp_high=22.0,
        room=24.0,
        fan_state="off",
    )
    assert len(calls) == 0


async def test_force_manage_ignores_manual_off(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """force_manage ON commands even a manually-off fan."""
    await _setup(hass, {"force_manage": True})
    await _trigger(
        hass,
        calls,
        freezer,
        hvac_action="idle",
        target_temp_high=22.0,
        room=20.0,
        fan_state="off",
    )
    assert _last_pct(calls) == 8


# --------------------------------------------------------------------------- #
# Sticky release hysteresis.
# --------------------------------------------------------------------------- #
async def test_no_premature_boost_below_threshold(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Not-yet-high + mid-band delta -> assist, not a premature boost."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        hvac_action="cooling",
        target_temp_high=22.0,
        room=22.5,  # delta 0.5, below boost threshold 1.0
        fan_pct=50,  # not currently high
    )
    assert _last_pct(calls) == 17


async def test_sticky_holds_high_within_band(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Currently-high + delta still above release -> stays 100 (no re-command)."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        hvac_action="cooling",
        target_temp_high=22.0,
        room=22.5,  # delta 0.5 > release 0.3
        fan_pct=98,  # currently high; desired stays 100, within 4% -> no call
    )
    assert len(calls) == 0


async def test_release_steps_down_below_release_threshold(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Currently-high + delta below release threshold -> steps down to assist."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        hvac_action="cooling",
        target_temp_high=22.0,
        room=22.2,  # delta 0.2 < release 0.3
        fan_pct=98,  # currently high
    )
    assert _last_pct(calls) == 17


# --------------------------------------------------------------------------- #
# Heating tier ladder (symmetric with cooling).
# --------------------------------------------------------------------------- #
async def test_heating_boost(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Actively heating and >= boost threshold below setpoint -> boost (100)."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        mode="heat",
        hvac_action="heating",
        target_temp_low=21.0,
        room=19.0,  # 2.0 below heat setpoint
    )
    assert _last_pct(calls) == 100


async def test_heating_assist_daytime(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Heating within threshold, daytime -> assist (17)."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        mode="heat",
        hvac_action="heating",
        target_temp_low=21.0,
        room=20.5,  # 0.5 below setpoint, within boost threshold
    )
    assert _last_pct(calls) == 17


async def test_fan_only_circulate_heating(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """Fan-only, room below heat setpoint -> circulate (60)."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        mode="heat",  # cool_sp is None; heat_sp comes from target_temp_low
        hvac_action="fan",
        target_temp_low=21.0,
        room=19.0,
    )
    assert _last_pct(calls) == 60


async def test_night_still_allows_heating_boost(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """At night, an active-heating boost is still allowed."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        when=_NIGHT,
        mode="heat",
        hvac_action="heating",
        target_temp_low=21.0,
        room=19.0,
    )
    assert _last_pct(calls) == 100


async def test_night_suppresses_heating_assist(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """At night, heating-within-threshold collapses to baseline."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        when=_NIGHT,
        mode="heat",
        hvac_action="heating",
        target_temp_low=21.0,
        room=20.5,
    )
    assert _last_pct(calls) == 8


# --------------------------------------------------------------------------- #
# Bidirectional equalizer: room COLDER than the house pulls warm air in too.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("room", "expected"),
    [(19.9, 100), (20.8, 60), (21.4, 33), (21.8, 8)],
)
async def test_equalizer_tiers_room_colder_than_house(
    hass: HomeAssistant,
    calls: list[ServiceCall],
    freezer: FrozenDateTimeFactory,
    room: float,
    expected: int,
) -> None:
    """No setpoint, blower circulating, home: equalize a cold room toward the house."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        mode="fan_only",
        hvac_action="fan",
        current_temperature=22.0,  # house is warmer than the room
        room=room,
    )
    assert _last_pct(calls) == expected


# --------------------------------------------------------------------------- #
# Enable toggles gate each direction.
# --------------------------------------------------------------------------- #
async def test_enable_cooling_false_leaves_baseline(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """With cooling management off, an otherwise-boost case stays at baseline."""
    await _setup(hass, {"enable_cooling": False})
    await _trigger(
        hass, calls, freezer, hvac_action="cooling", target_temp_high=22.0, room=24.0
    )
    assert _last_pct(calls) == 8


async def test_enable_heating_false_leaves_baseline(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """With heating management off, an otherwise-boost case stays at baseline."""
    await _setup(hass, {"enable_heating": False})
    await _trigger(
        hass,
        calls,
        freezer,
        mode="heat",
        hvac_action="heating",
        target_temp_low=21.0,
        room=19.0,
    )
    assert _last_pct(calls) == 8


# --------------------------------------------------------------------------- #
# Re-command threshold.
# --------------------------------------------------------------------------- #
async def test_no_recommand_within_threshold(
    hass: HomeAssistant, calls: list[ServiceCall], freezer: FrozenDateTimeFactory
) -> None:
    """No command when current speed is within one step (<=4%) of desired."""
    await _setup(hass)
    await _trigger(
        hass,
        calls,
        freezer,
        hvac_action="cooling",
        target_temp_high=22.0,
        room=24.0,  # desired 100
        fan_pct=97,  # within 4% of 100
    )
    assert len(calls) == 0
