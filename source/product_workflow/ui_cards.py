"""Small Chainlit adapters for stage cards; business decisions stay in the UI workflow."""
import asyncio
import json
import math
import time

import chainlit as cl
from chainlit.types import AskElementSpec
from chainlit.emitter import ChainlitEmitter


def action_dict(action):
    return {key: getattr(action, key, None) for key in
            ('id', 'name', 'payload', 'label', 'tooltip', 'icon', 'forId')}


async def update_component(component, props):
    component.props = props
    component.content = json.dumps(props, ensure_ascii=False)
    await component.update()


class _DomainEmitter(ChainlitEmitter):
    """Record this Ask's socket reply before Chainlit updates the UI."""
    received_at = None

    async def emit_call(self, event, payload, timeout):
        reply = await super().emit_call(event, payload, timeout)
        if reply is not None:
            self.received_at = time.monotonic()
        return reply


class DomainAsk(cl.AskElementMessage):
    """One delivered component, one deadline shared by lease and socket Ask."""
    def __init__(self, *, element, ready, finished):
        super().__init__(content='', element=element, timeout=120, raise_on_timeout=False)
        self.ready, self.finished = ready, finished
        self.received_at = None

    async def send(self):
        self.wait_for_answer = True
        step = await self._create()
        try:
            await self.element.send(for_id=str(step['id']))
            if not await self.ready(self.element):
                return None
            # ready() verifies identity and creates this exact deadline.
            deadline = self.element.props.pop('_deadline')
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError('Domain confirmation expired before activation')
            spec = AskElementSpec(type='element', step_id=step['id'],
                                  timeout=max(1, math.ceil(remaining)), element_id=self.element.id)
            emitter = _DomainEmitter(cl.context.emitter.session)
            wait = asyncio.create_task(emitter.send_ask_user(step, spec, False))
            try:
                # The emitter enters emit_call immediately. Its Ask packet is
                # queued before the subsequent element update enables buttons.
                async def enable_and_wait():
                    await asyncio.sleep(0)
                    self.element.props['askActive'] = True
                    await update_component(self.element, self.element.props)
                    return await wait
                # The native Ask requires an integer; this absolute deadline
                # remains authoritative, including a slow enable transmission.
                delivery = asyncio.create_task(enable_and_wait())
                try:
                    done, _ = await asyncio.wait({delivery}, timeout=max(0, deadline-time.monotonic()))
                    if not done:
                        if emitter.received_at is None or emitter.received_at >= deadline:
                            raise asyncio.TimeoutError("Domain confirmation expired")
                        # A timely reply can still be inside native Ask cleanup.
                        try:
                            return await asyncio.wait_for(delivery, 5)
                        except asyncio.TimeoutError as exc:
                            raise RuntimeError("Domain reply cleanup timed out") from exc
                    return delivery.result()
                finally:
                    self.received_at = emitter.received_at
                    if not delivery.done():
                        delivery.cancel()
                        await asyncio.gather(delivery, return_exceptions=True)
            finally:
                if not wait.done():
                    wait.cancel()
                    await asyncio.gather(wait, return_exceptions=True)
        finally:
            self.wait_for_answer = False
            self.element.props['askActive'] = False
            try:
                await asyncio.wait_for(self.finished(self.element), 5)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("Domain card cleanup timed out") from exc

