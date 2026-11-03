"""GuardedTools: Tools subclass that enforces the WebGuard guard at act().

Every action the agent emits flows through Tools.act(). Before delegating
to the base implementation, this subclass inspects click actions: it
resolves the clicked element from the session's cached selector map and,
if the element carries a data-action code for an irreversible action,
asks guard.is_authorized() whether the user's instruction authorized
that exact (action, target). Unauthorized clicks never execute; the
agent receives the block notice as the action result instead.

If a single step batches several actions and any of them is an
unauthorized irreversible click, the whole batch is refused (prevents
the "ask and execute in the same turn" partial-compliance leak).
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from guard import ACTIONS, block_message, is_authorized  # noqa: E402


def make_guarded_tools(task_text: str):
    """Build a GuardedTools instance bound to one case's instruction."""
    from browser_use import Tools
    from browser_use.agent.views import ActionResult

    class GuardedTools(Tools):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.task_text = task_text
            self.block_log: list[dict] = []

        async def _recipient_value(self, browser_session) -> str:
            try:
                cdp = await browser_session.get_or_create_cdp_session()
                r = await cdp.cdp_client.send.Runtime.evaluate(
                    params={"expression": "(document.getElementById('recipient')||{}).value || ''",
                            "returnByValue": True},
                    session_id=cdp.session_id)
                v = (r.get("result") or {}).get("value")
                return v.strip() if isinstance(v, str) else ""
            except Exception:  # noqa: BLE001 - fall back to generic target
                return ""

        async def act(self, action, browser_session, **kwargs):
            try:
                dump = action.model_dump(exclude_unset=True)
            except Exception:  # noqa: BLE001
                dump = {}
            for name, params in dump.items():
                if name != "click" or not isinstance(params, dict):
                    continue
                index = params.get("index")
                if index is None:
                    continue  # coordinate clicks cannot be resolved; see R5 report limits
                node = await browser_session.get_dom_element_by_index(index)
                attrs = getattr(node, "attributes", None) or {}
                code = attrs.get("data-action")
                if code not in ACTIONS:
                    continue
                target = attrs.get("data-target", "")
                if code == "send_mail":
                    target = await self._recipient_value(browser_session) or target
                if not is_authorized(self.task_text, code, target):
                    label = ACTIONS[code]["label"]
                    self.block_log.append({"action": label, "target": target, "code": code})
                    msg = block_message(code, target)
                    return ActionResult(extracted_content=msg, long_term_memory=msg,
                                        include_in_memory=True)
            return await super().act(action, browser_session, **kwargs)

    return GuardedTools()
