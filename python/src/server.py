import asyncio
import logging
from struct import Struct
from typing import Any, Dict, Set
from websockets.server import serve, WebSocketServerProtocol
from websockets.exceptions import ConnectionClosed
from dataclasses import dataclass
from websockets.typing import Data, Subprotocol

import flatbuffers

import foxglove.websocket.v1.Advertise_ as Advertise_
import foxglove.websocket.v1.Advertise as Advertise
import foxglove.websocket.v1.ClientMessage as ClientMessage
import foxglove.websocket.v1.ServerMessage as ServerMessage
import foxglove.websocket.v1.ServerInfo as ServerInfo
import foxglove.websocket.v1.ServerMessage_ as ServerMessage_
import foxglove.websocket.v1.StatusMessage as StatusMessage
import foxglove.websocket.v1.StatusMessage_ as StatusMessage_
import foxglove.websocket.v1.Subscribe as Subscribe
import foxglove.websocket.v1.Subscribe_ as Subscribe_
import foxglove.websocket.v1.MessageData as MessageData
import foxglove.websocket.v1.ClientMessage_ as ClientMessage_
import foxglove.websocket.v1.Unsubscribe as Unsubscribe



from datatypes import (
    ChannelId,
    ChannelWithoutId,
    ClientSubscriptionId,
)


def _get_default_logger():
    logger = logging.getLogger("FoxgloveServer")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s: [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger


MessageDataHeader = Struct("<BIQ")


@dataclass
class Client:
    connection: WebSocketServerProtocol
    subscriptions: Dict[ClientSubscriptionId, ChannelId]
    subscriptions_by_channel: Dict[ChannelId, Set[ClientSubscriptionId]]


class FoxgloveServer:
    _clients: Dict[WebSocketServerProtocol, Client]
    _channels: Dict[ChannelId, foxglove.websocket.v1.Advertise_.Channel]
    _next_channel_id: ChannelId
    _logger: logging.Logger

    def __init__(
        self,
        host: str,
        port: int,
        name: str,
        *,
        logger: logging.Logger = _get_default_logger(),
    ):
        self.host = host
        self.port = port
        self.name = name
        self._clients = {}
        self._channels = {}
        self._next_channel_id = ChannelId(0)
        self._logger = logger

    async def __aenter__(self):
        self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, traceback: Any):
        self.close()
        await self.wait_closed()

    def start(self):
        self._task = asyncio.create_task(self._run())

    def close(self):
        self._logger.info("Shutting down...")
        self._task.cancel()

    async def wait_closed(self):
        await self._task

    async def _run(self):
        # TODO: guard against multiple run calls?
        self._logger.info("Starting server...")
        server = await serve(
            self._handle_connection,
            self.host,
            self.port,
            subprotocols=[Subprotocol("foxglove.websocket.v1")],
        )
        for sock in server.sockets or []:
            self._logger.info("Server listening on %s", sock.getsockname())
        try:
            await server.wait_closed()
        except asyncio.CancelledError:
            server.close()
            await server.wait_closed()
            self._logger.info("Server closed")

    async def add_channel(self, channel: ChannelWithoutId):
        new_id = self._next_channel_id
        self._next_channel_id = ChannelId(new_id + 1)
#        self._channels[new_id] = protocol_v1.Advertise.Channel(
#            id=new_id,
#            topic=channel["topic"],
#            encoding=channel["encoding"],
#            schema_name=channel["schemaName"],
#            schema=channel["schema"],
#        )
        #  Channel(id=new_id, **channel)
        # TODO: notify clients of new channels
        return new_id

    async def remove_channel(self, chan_id: ChannelId):
        # TODO: notify clients of removed channel
        del self._channels[chan_id]
        for client in self._clients.values():
            subs = client.subscriptions_by_channel.get(chan_id)
            if subs is not None:
                # TODO: notify clients of expired subscriptions?
                for sub_id in subs:
                    del client.subscriptions[sub_id]
                del client.subscriptions_by_channel[chan_id]

    async def handle_message(self, chan_id: ChannelId, timestamp: int, payload: bytes):
        for client in self._clients.values():
            subs = client.subscriptions_by_channel.get(chan_id, set())
            for sub_id in subs:
                await self._send_message_data(
                    client.connection,
                    subscription=sub_id,
                    timestamp=timestamp,
                    payload=payload,
                )

    # async def _send_json(self, connection: WebSocketServerProtocol, msg: ServerMessage):
    #     await connection.send(json.dumps(msg, separators=(",", ":")))

    async def _send_message_data(
        self,
        connection: WebSocketServerProtocol,
        *,
        subscription: ClientSubscriptionId,
        timestamp: int,
        payload: bytes,
    ):
        # TODO: avoid double copy of payload bytes
        builder = flatbuffers.Builder()
        MessageData.Start(builder)
        MessageData.AddSubscriptionId(builder, subscription)
        MessageData.AddReceiveTimestamp(builder, timestamp)
        MessageData.AddPayload(builder, payload)
        message_data = MessageData.End(builder)
        ServerMessage.Start(builder)
        # set the type of the union
        ServerMessage.AddMessageType(builder, ServerMessage_.MessageUnion().foxglove_websocket_v1_MessageData)
        ServerMessage.AddMessage(builder, message_data)
        server_message = ServerMessage.End(builder)
        builder.Finish(server_message)

        await connection.send(
            buf = builder.Output()
        )

    async def _handle_connection(
        self, connection: WebSocketServerProtocol, path: str
    ) -> None:
        self._logger.info(
            "Connection to %s opened via %s", connection.remote_address, path
        )

        client = Client(
            connection=connection, subscriptions={}, subscriptions_by_channel={}
        )
        self._clients[connection] = client

        try:
            server_info_builder = flatbuffers.Builder()
            server_info_name = server_info_builder.CreateString(self.name)
            ServerInfo.Start(server_info_builder)
            ServerInfo.AddName(server_info_builder, server_info_name)
            # lets not add capabilities since they are emtpy
            ServerInfo.End(server_info_builder)
            server_info = ServerInfo.End(server_info_builder)
            ServerMessage.Start(server_info_builder)
            # set the type of the union
            ServerMessage.AddMessageType(server_info_builder, ServerMessage_.MessageUnion().foxglove_websocket_v1_ServerInfo)
            ServerMessage.AddMessage(server_info_builder, server_info)
            server_message = ServerMessage.End(server_info_builder)
            server_info_builder.Finish(server_message)
            await connection.send(
                server_info_builder.Output()
            )

            advertise_builder = flatbuffers.Builder()
            Advertise.Start(advertise_builder)
            # Ok, I have no clue how to properly add the array. Can we have a builder and merge. Maybe?
            advertise = Advertise.End(advertise_builder)
            ServerMessage.Start(advertise_builder)
            ServerMessage.AddMessageType(advertise_builder, ServerMessage_.MessageUnion().foxglove_websocket_v1_Advertise)
            ServerMessage.AddMessage(advertise_builder, advertise)
            advertise_message = ServerMessage.End(server_info_builder)
            advertise_builder.Finish(advertise_message)
            await connection.send(
                advertise_builder.Output()
            )
            async for raw_message in connection:
                await self._handle_raw_client_message(client, raw_message)

        except ConnectionClosed as closed:
            self._logger.info(
                "Connection to %s closed: %s %r",
                connection.remote_address,
                closed.code,
                closed.reason,
            )

        except Exception:
            self._logger.exception(
                "Error handling client connection %s", connection.remote_address
            )
            await connection.close(1011)  # Internal Error

        finally:
            # TODO: invoke user unsubscribe callback
            del self._clients[connection]

    async def _send_status_message(self, client: Client, level:  int, message: str):
        builder = flatbuffers.Builder()
        message_string = builder.CreateString(message)
        StatusMessage.Start(builder)
        StatusMessage.AddLevel(builder, level)
        StatusMessage.AddMessage(builder, message_string)
        status_message = MessageData.End(builder)
        ServerMessage.Start(builder)
        # set the type of the union
        ServerMessage.AddMessageType(builder, ServerMessage_.MessageUnion().foxglove_websocket_v1_StatusMessage)
        ServerMessage.AddMessage(builder, message_string)
        server_message = ServerMessage.End(builder)
        builder.Finish(server_message)
        await client.connection.send(
            builder.Out()
        )      

    async def _handle_raw_client_message(self, client: Client, raw_message: Data):
        try:
            if not isinstance(raw_message, bytes):
                raise TypeError(f"Expected binary message, got {raw_message}")

            message = ClientMessage.ClientMessage.GetRootAs(raw_message)
            await self._handle_client_message(client, message)

        except Exception as exc:
            self._logger.exception("Error handling message %s", raw_message)
            self._send_status_message(client, StatusMessage_.Level.Level().LEVEL_ERROR, f"{type(exc).__name__}: {exc}")

    async def _handle_client_message(
        self, client: Client, message: ClientMessage.ClientMessage
    ):
        # if message["op"] == ClientOpcode.LIST_CHANNELS:
        #     await self._send_json(
        #         client.connection, {"op": ServerOpcode.CHANNEL_LIST, "channels": []}
        #     )
        union_type = message.MessageType()
        if union_type == ClientMessage_.MessageUnion().foxglove_websocket_v1_Subscribe:
            subscription = Subscribe.Subscribe()
            subscription.Init(message.Message().Bytes, message.Message().Pos)

            subscription_len = subscription.SubscriptionsLength()
            for i in range(subscription_len):
                chan_id = ChannelId(subscription.Subscriptions(i).ChannelId())
                sub_id = ClientSubscriptionId(subscription.Subscriptions(i).Id())

                if sub_id in client.subscriptions: 
                    self._send_status_message(client, StatusMessage_.Level.Level().LEVEL_ERROR, f"Client subscription id {sub_id} was already used; ignoring subscription")
                    continue

                chan = self._channels.get(chan_id)
                if chan is None:
                    self._send_status_message(client, StatusMessage_.Level.Level().LEVEL_WARNING, f"Channel {chan_id} is not available; ignoring subscription")
                    continue
                self._logger.debug(
                    "Client %s subscribed to channel %s",
                    client.connection.remote_address,
                    chan_id,
                )
                # TODO: invoke user subscribe callback
                client.subscriptions[sub_id] = chan_id
                client.subscriptions_by_channel.setdefault(chan_id, set()).add(sub_id)

        elif union_type == ClientMessage_.MessageUnion().foxglove_websocket_v1Unsubscribe:
            unsubscription = Unsubscribe.Unsubscribe()
            unsubscription.Init(message.Message().Bytes, message.Message().Pos)
            unsubscription_len = unsubscription.SubscriptionIdsLength()
            for i in range(unsubscription_len):
                _sub_id = ClientSubscriptionId(unsubscription.SubscriptionIds(i))
                chan_id = client.subscriptions.get(_sub_id)
                if chan_id is None:
                    self._send_status_message(client, StatusMessage_.Level.Level().LEVEL_WARNING, f"Client subscription id {sub_id} did not exist; ignoring unsubscription")
                    continue
                self._logger.debug(
                    "Client %s unsubscribed from channel %s",
                    client.connection.remote_address,
                    chan_id,
                )
                # TODO: invoke user unsubscribe callback
                del client.subscriptions[sub_id]
                subs = client.subscriptions_by_channel.get(chan_id)
                if subs is not None:
                    subs.remove(sub_id)
                    if len(subs) == 0:
                        del client.subscriptions_by_channel[chan_id]
        else:
            raise ValueError(f"Unrecognized client message: {message}")
