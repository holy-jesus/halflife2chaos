from typing import Optional
import asyncio
from inspect import iscoroutinefunction
import traceback

import websockets.client
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedOK,
    ConnectionClosedError,
    InvalidHandshake,
)


class User:
    def __init__(self, event: dict) -> None:
        self.id: str = event["user-id"]
        self.name: str = event["nick"]
        self.mod: bool = event["mod"]
        self.subscriber: bool = event["subscriber"]
        self.turbo: bool = event["turbo"]
        self.display_name: str = event.get("display-name", event["nick"])
        self.color: Optional[str] = event.get("color", None)

class Room:
    def __init__(self, event: dict) -> None:
        self.id: str = event["room-id"]
        self.name: str = event["channel"]

class Message:
    def __init__(self, event: dict) -> None:
        self.id: str = event["id"]
        self.text: str = event["text"]
        self.tmi_sent_ts: int = event["tmi-sent-ts"]
        self.emotes: Optional[dict] = event["emotes"]
        self.first_msg: bool = event.get("first-msg", False)

        self.user: User = User(event)
        self.room: Room = Room(event)

class Chat:
    def __init__(self, channel: str, loop: asyncio.AbstractEventLoop) -> None:
        self._channel = channel
        self.loop = loop

        self.connected = asyncio.Event()
        self.joined_channel = asyncio.Event()
        self._channel_queue = asyncio.Queue()

        self._backoff = 0

        self.ws: websockets.client.WebSocketClientProtocol = None
        self._on_message = None
        self._thread_task: asyncio.Task = None
        self._channel_task: asyncio.Task = None
        self._internal_events = {
            "PRIVMSG": self._privmsg,
            "PING": self._ping,
            "NOTICE": self._notice,
            "001": self._connected,
            "JOIN": self._join,
            "RECONNECT": self._reconnect,
        }

    # Start

    async def start(self) -> None:
        if self.connected.is_set():
            self._thread_task.cancel()
            if self.ws.open:
                await self.ws.close()
            self.connected.clear()
        while True:
            try:
                self.ws = await websockets.client.connect(
                    "wss://irc-ws.chat.twitch.tv:443", ping_timeout=None, ping_interval=None
                )
                break
            except (OSError, TimeoutError, InvalidHandshake):
                if not self.connected.is_set():
                    print(f"Retrying connect in {self._backoff}...")
                    await asyncio.sleep(self._backoff)
                    self._backoff = min((self._backoff * 2) if self._backoff != 0 else 1, 60)
                self.connected.clear()
        await self._send(
            "CAP REQ :twitch.tv/commands twitch.tv/membership twitch.tv/tags"
        )
        await self._send(f"PASS kappa")
        await self._send(f"NICK justinfan1337")
        if self._channel:
            await self._send(f"JOIN #{self._channel}")
        self._thread_task = self.loop.create_task(self._thread())

    # Main

    async def _thread(self) -> None:
        try:
            while True:
                data = await self.ws.recv()
                if data:
                    events = data.split("\r\n")
                    for event in events:
                        if not event:
                            continue
                        self.loop.create_task(self._process_event(event))
        finally:
            if not self.connected.is_set():
                print(f"Retrying connect in {self._backoff}...")
                await asyncio.sleep(self._backoff)
                self._backoff = min((self._backoff * 2) if self._backoff != 0 else 1, 60)
            self.connected.clear()
            await self.start()

    async def _process_event(self, data: str) -> None:
        data = data.strip()
        parsed = self._parse_event(data)
        if not parsed:
            return
        command = parsed["command"]
        await self._call_function(self._internal_events[command], parsed)

    # Sending messages

    async def _send(self, data: str) -> None:
        try:
            await self.ws.send(data + "\r\n")
        except (ConnectionClosed, ConnectionClosedOK, ConnectionClosedError):
            pass

    # Parsing data

    def _parse_event(self, message: str) -> dict:
        idx = 0
        event = {"text": None}
        if message[idx] == "@":
            end_idx = message.find(" ")
            self._parse_tags(message[1:end_idx], event)
            idx = end_idx + 1
        if message[idx] == ":":
            idx += 1
            end_idx = message.find(" ", idx)
            self._parse_source(message[idx:end_idx], event)
            idx = end_idx + 1
        end_idx = message.find(":", idx)
        if end_idx == -1:
            end_idx = len(message)
        self._parse_command(message[idx:end_idx].strip(), event)
        if event["command"] is None:
            return None
        if end_idx != len(message):
            idx = end_idx + 1
            event["text"] = message[idx:]
        return event

    @staticmethod
    def _parse_tags(tags: str, event: dict) -> None:
        tags_to_ignore = ("client-nonce", "flags")
        parsed_tags = tags.split(";")
        for tag in parsed_tags:
            parsed_tag = tag.split("=")
            tag_value = parsed_tag[1]
            if parsed_tag[0] in ("badges", "badge-info"):
                if tag_value:
                    event[parsed_tag[0]] = tuple(
                        pair.split("/")[0] for pair in tag_value.split(",")
                    )
                else:
                    event[parsed_tag[0]] = None
            elif parsed_tag[0] == "emotes":
                if tag_value:
                    dict_emotes = {}
                    emotes = tag_value.split("/")
                    for emote in emotes:
                        emote_parts = emote.split(":")
                        text_positions = []
                        positions = emote_parts[1].split(",")
                        for position in positions:
                            position_parts = position.split("-")
                            text_positions.append(
                                {
                                    "start_position": position_parts[0],
                                    "end_position": position_parts[1],
                                }
                            )
                        dict_emotes[emote_parts[0]] = text_positions
                    event[parsed_tag[0]] = dict_emotes
                else:
                    event[parsed_tag[0]] = None
            elif parsed_tag[0] == "emote-sets":
                emote_set_ids = tag_value.split(",")
                event[parsed_tag[0]] = emote_set_ids
            elif parsed_tag[0] in (
                "first-msg",
                "mod",
                "subscriber",
                "turbo",
                "emote-only",
            ):
                event[parsed_tag[0]] = tag_value == "1"
            elif parsed_tag[0] in ("tmi-sent-ts"):
                event[parsed_tag[0]] = int(tag_value)
            else:
                if parsed_tag[0] not in tags_to_ignore:
                    event[parsed_tag[0]] = tag_value

    @staticmethod
    def _parse_command(raw_command_component: str, event: dict) -> None:
        command_parts = raw_command_component.split(" ")
        event["command"] = None
        if command_parts[0] in (
            "JOIN",
            "NOTICE",
            "PRIVMSG",
        ):
            event["command"] = command_parts[0]
            event["channel"] = command_parts[1].replace("#", "")
        elif command_parts[0] in ("PING", "001", "RECONNECT"):
            event["command"] = command_parts[0]

    @staticmethod
    def _parse_source(raw_source_component: str, event: dict) -> None:
        source_parts = raw_source_component.split("!")
        event["nick"] = source_parts[0] if len(source_parts) == 2 else None
        event["host"] = source_parts[1] if len(source_parts) == 2 else source_parts[0]

    # Events

    def _privmsg(self, parsed: dict) -> None:
        self._on_message(Message(parsed))

    async def _ping(self, _) -> None:
        await self._send("PONG :tmi.twitch.tv")

    def _notice(self, parsed: dict) -> None:
        print(f"Notice from Twitch: {parsed['text']}")

    def _connected(self, _) -> None:
        print("Successfully connected to Twitch.")
        self.connected.set()
        self._backoff = 0

    def _join(self, parsed: dict) -> None:
        if "justinfan1337" == parsed["nick"] and self._channel == parsed["channel"]:
            print(f"Successfully joined {self._channel} channel.")
            self.joined_channel.set()

    async def _reconnect(self, _) -> None:
        await self.close()
        await asyncio.sleep(2) # Just in case
        await self.start()

    # User events

    def on_message(self, func):
        self._on_message = func
        return func

    @staticmethod
    async def _call_function(func, *args, **kwargs):
        try:
            if iscoroutinefunction(func):
                return await func(*args, **kwargs)
            else:
                return func(*args, **kwargs)
        except Exception as e:
            print(str(e), "\n", "".join(traceback.format_tb(e.__traceback__)))

    # Closing

    async def close(self) -> None:
        if self._thread_task:
            self._thread_task.cancel()
        if self.ws.open:
            await self.ws.close()
        self.connected.clear()
        self.joined_channel.clear()

    # Misc

    def channel_update(self, channel: str):
        if self._channel.lower() != channel.lower():
            if not self._channel_task or self._channel_task.done():
                self._channel_task = self.loop.create_task(self.channel_loop())
            self._channel_queue.put_nowait(channel)

    async def channel_loop(self):
        try:
            while True:
                channel: str = await asyncio.wait_for(self._channel_queue.get(), 2)
        except TimeoutError:
            if channel and self._channel.lower() != channel.lower():
                if self._channel and self.joined_channel.is_set():
                    self.joined_channel.clear()
                    print(f"Left from {self._channel} channel.")
                    await self._send(f"PART #{self._channel}")
                self._channel = channel
                await self._send(f"JOIN #{channel}")
