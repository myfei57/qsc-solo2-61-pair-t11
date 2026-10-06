"""File backed persistence for the link monitor.

Each aggregate is one document kind under the shared :class:`Store`, so writes
inherit the store's atomic temp-file-and-rename guarantee.  Everything the
console and the audit API show is read back from these documents - there is no
second, in-memory truth.
"""

from __future__ import annotations

import threading
from typing import Iterable

from tunnelnet.store import Store

from .model import (
    DeviceConfig,
    DeviceState,
    Episode,
    EPISODE_CLOSED,
    LinkCommand,
    LinkConfig,
    LinkEvent,
    LinkState,
    Linkage,
    LinkageState,
)

LINK_CONFIG_KIND = "link-config"
DEVICE_CONFIG_KIND = "link-device"
DEVICE_STATE_KIND = "link-device-state"
LINK_STATE_KIND = "link-state"
LINKAGE_KIND = "link-linkage"
LINKAGE_STATE_KIND = "link-linkage-state"
EVENT_KIND = "link-event"
EPISODE_KIND = "link-episode"
COMMAND_KIND = "link-command"


def _load(store: Store, kind: str, record_id: str) -> dict[str, object] | None:
    return store.load(kind, record_id)


class LinkRepository:
    """Loads and saves every link document behind one re-entrant lock.

    The lock serialises monitor evaluations, operator actions and console
    reads within one process; durability across processes comes from the
    atomic file writes.
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self.lock = threading.RLock()

    # -- configs ------------------------------------------------------------

    def save_link_config(self, config: LinkConfig) -> None:
        self._store.save(LINK_CONFIG_KIND, config.id, config.as_dict())

    def load_link_config(self, link_id: str) -> LinkConfig | None:
        payload = _load(self._store, LINK_CONFIG_KIND, link_id)
        return LinkConfig.from_dict(payload) if payload else None

    def list_link_configs(self) -> list[LinkConfig]:
        return [config for config in (self.load_link_config(link_id) for link_id in self._store.list(LINK_CONFIG_KIND)) if config]

    def save_device_config(self, device: DeviceConfig) -> None:
        self._store.save(DEVICE_CONFIG_KIND, device.id, device.as_dict())

    def load_device_config(self, device_id: str) -> DeviceConfig | None:
        payload = _load(self._store, DEVICE_CONFIG_KIND, device_id)
        return DeviceConfig.from_dict(payload) if payload else None

    def list_device_configs(self, link_id: str | None = None) -> list[DeviceConfig]:
        devices = [
            device
            for device in (self.load_device_config(device_id) for device_id in self._store.list(DEVICE_CONFIG_KIND))
            if device is not None
        ]
        if link_id is not None:
            devices = [device for device in devices if device.link_id == link_id]
        return sorted(devices, key=lambda device: device.id)

    def save_linkage(self, linkage: Linkage) -> None:
        self._store.save(LINKAGE_KIND, linkage.id, linkage.as_dict())

    def load_linkage(self, linkage_id: str) -> Linkage | None:
        payload = _load(self._store, LINKAGE_KIND, linkage_id)
        return Linkage.from_dict(payload) if payload else None

    def list_linkages(self, link_id: str | None = None) -> list[Linkage]:
        linkages = [
            linkage
            for linkage in (self.load_linkage(linkage_id) for linkage_id in self._store.list(LINKAGE_KIND))
            if linkage is not None
        ]
        if link_id is not None:
            linkages = [linkage for linkage in linkages if linkage.link_id == link_id]
        return sorted(linkages, key=lambda linkage: linkage.id)

    # -- device state -------------------------------------------------------

    def save_device_state(self, state: DeviceState) -> None:
        self._store.save(DEVICE_STATE_KIND, state.id, state.as_dict())

    def load_device_state(self, device_id: str) -> DeviceState | None:
        payload = _load(self._store, DEVICE_STATE_KIND, device_id)
        return DeviceState.from_dict(payload) if payload else None

    def list_device_states(self, link_id: str | None = None) -> list[DeviceState]:
        states = [
            state
            for state in (self.load_device_state(device_id) for device_id in self._store.list(DEVICE_STATE_KIND))
            if state is not None
        ]
        if link_id is not None:
            states = [state for state in states if state.link_id == link_id]
        return sorted(states, key=lambda state: state.id)

    # -- link state ---------------------------------------------------------

    def save_link_state(self, state: LinkState) -> None:
        self._store.save(LINK_STATE_KIND, state.id, state.as_dict())

    def load_link_state(self, link_id: str) -> LinkState | None:
        payload = _load(self._store, LINK_STATE_KIND, link_id)
        return LinkState.from_dict(payload) if payload else None

    def list_link_states(self) -> list[LinkState]:
        return [
            state
            for state in (self.load_link_state(link_id) for link_id in self._store.list(LINK_STATE_KIND))
            if state is not None
        ]

    # -- linkage state ------------------------------------------------------

    def save_linkage_state(self, state: LinkageState) -> None:
        self._store.save(LINKAGE_STATE_KIND, state.id, state.as_dict())

    def load_linkage_state(self, linkage_id: str) -> LinkageState | None:
        payload = _load(self._store, LINKAGE_STATE_KIND, linkage_id)
        return LinkageState.from_dict(payload) if payload else None

    def list_linkage_states(self, link_id: str | None = None) -> list[LinkageState]:
        states = [
            state
            for state in (self.load_linkage_state(linkage_id) for linkage_id in self._store.list(LINKAGE_STATE_KIND))
            if state is not None
        ]
        if link_id is not None:
            states = [state for state in states if state.link_id == link_id]
        return sorted(states, key=lambda state: state.id)

    # -- events -------------------------------------------------------------

    def append_event(self, event: LinkEvent) -> None:
        self._store.save(EVENT_KIND, event.id, event.as_dict())

    def list_events(self, entity_type: str | None = None, entity_id: str | None = None) -> list[LinkEvent]:
        events = [
            event
            for event in (self._load_event(event_id) for event_id in self._store.list(EVENT_KIND))
            if event is not None
        ]
        if entity_type is not None:
            events = [event for event in events if event.entity_type == entity_type]
        if entity_id is not None:
            events = [event for event in events if event.entity_id == entity_id]
        events.sort(key=lambda event: (event.at, event.id))
        return events

    def _load_event(self, event_id: str) -> LinkEvent | None:
        payload = _load(self._store, EVENT_KIND, event_id)
        return LinkEvent.from_dict(payload) if payload else None

    # -- episodes -----------------------------------------------------------

    def save_episode(self, episode: Episode) -> None:
        self._store.save(EPISODE_KIND, episode.id, episode.as_dict())

    def load_episode(self, episode_id: str) -> Episode | None:
        payload = _load(self._store, EPISODE_KIND, episode_id)
        return Episode.from_dict(payload) if payload else None

    def list_episodes(self, link_id: str | None = None) -> list[Episode]:
        episodes = [
            episode
            for episode in (self.load_episode(episode_id) for episode_id in self._store.list(EPISODE_KIND))
            if episode is not None
        ]
        if link_id is not None:
            episodes = [episode for episode in episodes if episode.link_id == link_id]
        episodes.sort(key=lambda episode: (episode.opened_at, episode.id))
        return episodes

    def open_episode(self, link_id: str) -> Episode | None:
        return next(
            (episode for episode in reversed(self.list_episodes(link_id)) if episode.status != EPISODE_CLOSED),
            None,
        )

    def reopen_latest_episode(self, link_id: str, reason: str) -> Episode | None:
        """Reopen the most recently closed episode, or ``None`` if there is none.

        A failure during recovery belongs to the degradation episode that was
        optimistically closed when NORMAL appeared to be reached.
        """

        episodes = self.list_episodes(link_id)
        if not episodes:
            return None
        latest = episodes[-1]
        if latest.status != EPISODE_CLOSED:
            return latest
        reopened = Episode(
            id=latest.id,
            link_id=latest.link_id,
            status=EPISODE_OPEN,
            opened_at=latest.opened_at,
            closed_at=None,
            reason=latest.reason,
            close_reason="",
        )
        self.save_episode(reopened)
        return reopened

    # -- commands -----------------------------------------------------------

    def save_command(self, command: LinkCommand) -> None:
        self._store.save(COMMAND_KIND, command.id, command.as_dict())

    def load_command(self, command_id: str) -> LinkCommand | None:
        payload = _load(self._store, COMMAND_KIND, command_id)
        return LinkCommand.from_dict(payload) if payload else None

    def list_commands(self, link_id: str | None = None) -> list[LinkCommand]:
        commands = [
            command
            for command in (self.load_command(command_id) for command_id in self._store.list(COMMAND_KIND))
            if command is not None
        ]
        if link_id is not None:
            commands = [command for command in commands if command.link_id == link_id]
        commands.sort(key=lambda command: (command.issued_at, command.id))
        return commands

    def command_by_key(self, key: str) -> LinkCommand | None:
        return next((command for command in self.list_commands() if command.key == key), None)

    # -- bulk helpers -------------------------------------------------------

    def stats(self) -> dict[str, int]:
        return {
            "links": len(self.list_link_states()),
            "devices": len(self.list_device_states()),
            "linkages": len(self.list_linkage_states()),
            "events": len(self.list_events()),
            "episodes": len(self.list_episodes()),
            "commands": len(self.list_commands()),
        }

    def replace_all(
        self,
        *,
        link_configs: Iterable[LinkConfig] = (),
        device_configs: Iterable[DeviceConfig] = (),
        linkages: Iterable[Linkage] = (),
    ) -> None:
        """Seed helper used on first boot and by the test suite."""

        for config in link_configs:
            self.save_link_config(config)
        for device in device_configs:
            self.save_device_config(device)
        for linkage in linkages:
            self.save_linkage(linkage)
