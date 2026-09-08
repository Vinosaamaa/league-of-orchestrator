"""Fresh exact-runtime activity check before a notification wake.

An observation is not an atomic compare-and-send. Native transport race
acceptance remains required; never substitute the League request-turn marker.
"""

from typing import Any, Mapping

from .agent_adapters import adapter_kind_from_runtime
from .storage import StorageRefusal


def require_idle_receiver(multiplexer: Any, target: Mapping[str, Any]) -> Mapping[str, Any]:
    endpoint = target.get('endpoint') or target.get('locator')
    route = target.get('routing_name')
    session = target.get('session_ref')
    generation = target.get('runtime_generation') or target.get('generation')
    if not all(isinstance(value, str) and value for value in (endpoint, route, session, generation)):
        raise StorageRefusal('receiver_activity_unknown', 'wake requires exact runtime identity')
    if 'discover' not in multiplexer.capabilities:
        raise StorageRefusal('receiver_activity_unknown', 'transport cannot observe receiver activity')
    rows = [row for row in multiplexer.discover()
            if row.get('pane_id') == endpoint or row.get('name') == route]
    if len(rows) != 1:
        raise StorageRefusal('receiver_activity_unknown', 'wake receiver is absent or ambiguous')
    row = rows[0]
    native_session = row.get('agent_session')
    native_session = native_session.get('value') if isinstance(native_session, Mapping) else None
    if (row.get('pane_id') != endpoint or row.get('name') != route
        or native_session != session
        or row.get('agent') != adapter_kind_from_runtime(str(target.get('harness_kind', '')))
        or multiplexer.runtime_generation(row, session) != generation):
        raise StorageRefusal('receiver_activity_unknown', 'wake receiver identity changed')
    if row.get('agent_status') not in {'idle', 'done'}:
        code = 'receiver_busy' if row.get('agent_status') in {'working', 'blocked', 'waiting', 'active'} else 'receiver_activity_unknown'
        raise StorageRefusal(code, 'notification remains pending for the in-turn inbox')
    return row


def deliver_idle_notification(multiplexer: Any, target: Mapping[str, Any], body: str) -> None:
    observed = require_idle_receiver(multiplexer, target)
    if 'conditional_delivery' not in multiplexer.capabilities:
        raise StorageRefusal('receiver_activity_unknown', 'transport cannot conditionally wake an idle receiver')
    multiplexer.delivery_if_idle(str(target['routing_name']), body, observed=observed)
