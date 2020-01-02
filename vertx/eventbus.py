import asyncio
import threading
import json
import struct
import logging

from typing import Optional

LOGGER = logging.getLogger(__name__)


class Delivery:

    def __init__(self, type="ping", address=None, replyAddress=None, header=None, body=None):
        # type: (str, Optional[str], Optional[str], Optional[dict], Optional[dict]) -> None
        self.data = {"type": type}
        if address:
            self.data["address"] = address
        if replyAddress:
            self.data["replyAddress"] = replyAddress
        if header:
            self.data["header"] = header
        if body:
            self.data["body"] = body

    def __repr__(self):
        return json.dumps(self.data)

    def serialize(self):
        msg = self.__repr__().encode()
        return struct.pack("!i%ss" % len(msg), len(msg), msg)

    @staticmethod
    def deserialize(byte_array):
        # type: (bytes) -> dict
        length = int.from_bytes(byte_array[:4], 'big')
        text = byte_array[4: 4 + length].decode()
        return json.loads(text)


class EventBus:

    def __init__(self, host, port):
        # type: (str, int) -> None
        self.host = host
        self.port = port
        self.loop = asyncio.get_event_loop()
        self.daemon = threading.Thread(target=self.loop.run_forever, name="event-bus-async")
        self.stop_sign = self.loop.create_future()  # type: asyncio.Future[None]  # add a stop sign to control the loop
        self.inputs = asyncio.Queue(loop=self.loop)
        self.listen_funcs = {}

    async def _connect_then_listen(self):
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            while True:
                incoming = asyncio.ensure_future(reader.read(100000))
                outgoing = asyncio.ensure_future(self.inputs.get())
                done, pending = await asyncio.wait([incoming, outgoing, self.stop_sign],
                                                   return_when=asyncio.FIRST_COMPLETED)  # type: set[asyncio.Future], set[asyncio.Future]  # noqa
                # Cancel pending tasks to avoid leaking them.
                if incoming in pending:
                    incoming.cancel()
                if outgoing in pending:
                    outgoing.cancel()

                if outgoing in done:
                    msg = outgoing.result()
                    writer.write(msg.serialize())
                    await writer.drain()
                    LOGGER.debug(f"SEND: {msg}")

                if incoming in done:
                    byte = incoming.result()
                    LOGGER.debug(byte)
                    obj = Delivery.deserialize(byte)
                    self.listen(obj)

                if self.stop_sign in done:
                    break
        finally:
            await writer.close()
            self.disconnect()

    async def ping(self, ping_interval_by_seconds=20):
        # type: (int) -> None
        """ Use a ping operation to keep long polling """
        while True:
            self.send(Delivery())
            await asyncio.sleep(ping_interval_by_seconds)

    def send(self, payload):
        # type: (Delivery) -> None
        address = payload.data.get("address")
        if address and payload.data.get('type') == "register":
            self.listen_funcs[address] = lambda x: LOGGER.info(f'Address: {address}; Body: {x}')
        self.loop.call_soon_threadsafe(self.inputs.put_nowait, payload)

    def connect(self):
        self.loop.create_task(self._connect_then_listen())
        self.loop.create_task(self.ping())
        self.daemon.start()

    def disconnect(self):
        self.loop.call_soon_threadsafe(self.stop_sign.set_result, None)  # break the event loop
        self.loop.stop()  # stop the event loop
        self.daemon.join()  # stop the thread

    def listen(self, obj):
        # type: (dict) -> None
        if 'address' not in obj or 'body' not in obj:
            return
        address, body = obj["address"], obj["body"]
        func = self.listen_funcs.get(address)
        if func:
            func(body)

    def add_listen_func(self, address, func):
        self.listen_funcs[address] = func

    def delete_listen_func(self, address):
        try:
            del self.listen_funcs[address]
        except KeyError:
            LOGGER.error("There is the address listening function")
