import threading
from collections import Counter
from typing import Optional

# noinspection PyUnresolvedReferences
import obspython as obs
from rcon.source import rcon
from rcon.exceptions import WrongPassword
import asyncio
from chat import Chat, Message

TARGET_CHANNEL = ''
SOURCE_NAME = ''
RCON_HOST = "127.0.0.1"
RCON_PORT = "27015"
RCON_PASSWORD = ""

voteNumber = -1
# Counter(votes.values())
votes = {}
voteEffects = []
voteKeywords = []


def on_message(message: Message):
    global votes
    print(f"in {message.room.name}, {message.user.name} said: {message.text}")
    uppercase_keywords = [kw.upper() for kw in voteKeywords]
    text = message.text.upper()
    if text in uppercase_keywords:
        votes[message.user.id] = uppercase_keywords.index(text)

async def update_game_votes():
    global voteNumber, voteEffects, votes
    vote_counts = Counter(votes.values())
    vote_params = [""] * len(voteEffects)
    for i in range(len(voteEffects)):
        vote_params[i] = str(vote_counts.get(i) or 0)
    await rcon(
        'chaos_vote_internal_set', str(voteNumber), *vote_params,
        host=RCON_HOST, port=int(RCON_PORT), passwd=RCON_PASSWORD
    )

async def poll_game():
    global voteNumber, voteEffects, votes
    raw_resp = await rcon(
        'chaos_vote_internal_poll',
        host=RCON_HOST, port=int(RCON_PORT), passwd=RCON_PASSWORD
    )
    response = raw_resp.split("rcon from \"", 1)[0].strip()
    if response == "":
        return False  # the game is not quite ready yet.

    vote_number, *effects = response.split(";")
    vote_number = int(vote_number)

    if vote_number != voteNumber:
        voteNumber = vote_number
        voteEffects = effects
        votes = {}

    return True


async def game_loop():
    already_printed_err = False
    faulty_password = ""
    while True:
        await asyncio.sleep(1)
        if faulty_password == RCON_PASSWORD:
            continue  # wait for the user to change password
        else:
            faulty_password = ""
        try:
            if await poll_game():
                await update_game_votes()
            if already_printed_err:
                print("poll resumed as normal.")
            already_printed_err = False
        except ConnectionError as e:
            if not already_printed_err:
                print("poll failed", e)
                already_printed_err = True
        except WrongPassword:
            print("rcon wrong password")
            faulty_password = RCON_PASSWORD
        except Exception as e:
            print("poll unexpected exception:", e)  # i broke something. log and bail
            return


twitch: Optional[Chat] = None
poll_task: Optional[asyncio.Task] = None


async def try_starting_twitch():
    global twitch
    twitch = Chat(channel=TARGET_CHANNEL, loop=_LOOP)
    twitch.on_message(on_message)    
    await twitch.start()

async def shutdown_twitch():
    await twitch.close()


async def startup():
    global poll_task
    # since we're running in a thread, we need to wait for obs to set our properties.
    # TODO: change this to a future or some other kind of awaitable?
    await asyncio.sleep(1)
    loop = asyncio.get_event_loop()
    poll_task = loop.create_task(game_loop())
    await try_starting_twitch()
    print("startup done")


async def shutdown():
    global twitch, poll_task
    if poll_task:
        poll_task.cancel()
    await shutdown_twitch()
    print("shutdown done")


def set_text(source: str, text: str):
    s = obs.obs_get_source_by_name(source)
    if s:
        settings = obs.obs_data_create()
        obs.obs_data_set_string(settings, "text", text)
        obs.obs_source_update(s, settings)
        obs.obs_data_release(settings)
        obs.obs_source_release(s)


def update_source():
    if SOURCE_NAME == "":
        return
    output = f"Vote #{voteNumber}\n"
    vote_counts = Counter(votes.values())
    for i, voteEffect in enumerate(voteEffects):
        keyword = '?' if i > len(voteKeywords) - 1 else voteKeywords[i]
        vote_count = vote_counts.get(i) or 0
        output = output + f"{keyword} {voteEffect}: {vote_count}\n"
    set_text(SOURCE_NAME, output[:-1])  # exclude final newline


_LOOP: Optional[asyncio.AbstractEventLoop] = None
_THREAD: Optional[threading.Thread] = None

def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_text(props, "target_channel", "Target channel", obs.OBS_TEXT_DEFAULT)

    p = obs.obs_properties_add_list(props, "source", "Text Source", obs.OBS_COMBO_TYPE_EDITABLE,
                                    obs.OBS_COMBO_FORMAT_STRING)
    sources = obs.obs_enum_sources()
    if sources:
        for source in sources:
            source_id = obs.obs_source_get_unversioned_id(source)
            if source_id == "text_gdiplus" or source_id == "text_ft2_source":
                name = obs.obs_source_get_name(source)
                obs.obs_property_list_add_string(p, name, name)
    obs.source_list_release(sources)

    obs.obs_properties_add_text(props, "rcon_host", "RCON host", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "rcon_password", "RCON password", obs.OBS_TEXT_PASSWORD)
    obs.obs_properties_add_editable_list(props, "vote_keywords", "Vote keywords", obs.OBS_EDITABLE_LIST_TYPE_STRINGS,
                                         None, None)
    async def reconnect():
        print("reconnecting to twitch")
        await shutdown_twitch()
        await try_starting_twitch()

    def call_reconnect(props, p):
        _LOOP.call_soon_threadsafe(lambda l: asyncio.ensure_future(reconnect()), _LOOP)

    obs.obs_properties_add_button(props, "reconnect_button", "Reconnect to twitch", call_reconnect)
    return props


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, "rcon_host", "127.0.0.1:27015")

    obs_array = obs.obs_data_array_create()
    for i in ["!A", "!B", "!C", "!D"]:
        item = obs.obs_data_create()
        obs.obs_data_set_string(item, "value", i)
        obs.obs_data_array_push_back(obs_array, item)
        # obs.obs_data_release(item)
    obs.obs_data_set_default_array(settings, "vote_keywords", obs_array)
    # obs.obs_data_array_release(obs_array)


def script_update(settings):
    # i feel like i'm doing something wrong.
    global voteKeywords, TARGET_CHANNEL, SOURCE_NAME, RCON_HOST, RCON_PORT, RCON_PASSWORD
    TARGET_CHANNEL = obs.obs_data_get_string(settings, "target_channel")
    if twitch:
        twitch.channel_update(TARGET_CHANNEL)
    SOURCE_NAME = obs.obs_data_get_string(settings, "source")
    # TODO: Verify port here. We may have an error if we don't
    RCON_HOST, RCON_PORT = obs.obs_data_get_string(settings, "rcon_host").split(":", 1)
    RCON_PASSWORD = obs.obs_data_get_string(settings, "rcon_password")

    voteKeywords = []
    obs_votes_keywords = obs.obs_data_get_array(settings, "vote_keywords")
    for i in range(obs.obs_data_array_count(obs_votes_keywords)):
        item = obs.obs_data_array_item(obs_votes_keywords, i)
        value = obs.obs_data_get_string(item, "value")
        voteKeywords.append(value)
        obs.obs_data_release(item)
    obs.obs_data_array_release(obs_votes_keywords)
    print("data updated")


# https://gist.github.com/serializingme/5c1a6fd6c7ea58af77c7b80579737c5a

def script_load(settings):
    global _LOOP, _THREAD

    # let's be nice, and only call the obs's methods from its own thread.
    # TODO: call this every frame because why not?
    obs.timer_add(update_source, 1000)

    _LOOP = asyncio.new_event_loop()

    def async_thread():
        global _LOOP
        asyncio.set_event_loop(_LOOP)
        asyncio.ensure_future(startup())
        _LOOP.run_forever()
        _LOOP.close()
        _LOOP = None

    _THREAD = threading.Thread(None, async_thread, daemon=True)
    _THREAD.start()


def script_unload():
    obs.timer_remove(update_source)

    global _LOOP, _THREAD
    if _LOOP is not None:
        async def destroy():
            await shutdown()
            _LOOP.stop()

        # let the destructor run
        _LOOP.call_soon_threadsafe(lambda l: asyncio.ensure_future(destroy()), _LOOP)

    if _THREAD is not None:
        # Wait for 5 seconds, if it doesn't exit just move on not to block
        # OBS main thread. Logging something about the failure to properly exit
        # is advised.
        _THREAD.join()
        _THREAD = None


def script_description():
    return """Twitch chat voting plugin for HL2Chaos mod
    
Made by acuifex, modified by holy-jesus
Released under AGPLv3 license"""
