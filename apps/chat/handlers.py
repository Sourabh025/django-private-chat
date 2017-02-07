import asyncio
import json
import logging
import urllib
import uuid
import websockets
from django.contrib.auth import get_user_model

from django.template.defaultfilters import date as dj_date

from apps.chat import channels, models, router
from .utils import get_user_from_session, get_dialogs_with_user

logger = logging.getLogger('apps.chat')
ws_connections = {}


@asyncio.coroutine
def target_message(conn, payload):
    """
    Distibuted payload (message) to one connection
    :param conn: connection
    :param payload: payload(json dumpable)
    :return:
    """
    try:
        yield from conn.send(json.dumps(payload))
    except Exception as e:
        logger.debug('could not send', e)


@asyncio.coroutine
def fanout_message(connections, payload):
    """distributes payload (message) to all connected ws clients
    """
    for conn in connections:
        try:
            yield from conn.send(json.dumps(payload))
        except Exception as e:
            logger.debug('could not send', e)


@asyncio.coroutine
def gone_online(stream):
    """
    Distributes the users online status to everyone he has dialog with
    """
    while True:
        packet = yield from stream.get()
        session_id = packet.get('session_key')
        opponent_username = packet.get('username')
        if session_id:
            user_owner = get_user_from_session(session_id)
            if user_owner:
                sockets = []
                usernames = []

                logger.debug(f'User {user_owner.username} gone online')
                # find all connections including user_owner as opponent, send them a message that the user has gone online
                online_opponents = list(filter(lambda x: x[1] == user_owner.username, ws_connections))
                online_opponents_sockets = [ws_connections[i] for i in online_opponents]
                # result = yield from fanout_message(online_opponents_sockets,
                #                           {'type': 'gone-online', 'usernames': [user_owner.username]})
                sockets += online_opponents_sockets
                usernames += [user_owner.username]
                if opponent_username:
                    # Send user online statuses of his opponents
                    socket = ws_connections.get((user_owner.username, opponent_username))
                    if socket:
                        online_opponents_usernames = [i[0] for i in online_opponents]
                        sockets += [socket]
                        usernames += online_opponents_usernames


                        # yield from fanout_message(socket,
                        #                {'type': 'gone-online', 'usernames': online_opponents_usernames})
                    else:
                        pass  # socket for the pair user_owner.username, opponent_username not found

                else:
                    # no opponent username
                    pass

                yield from fanout_message(sockets, {'type': 'gone-online', 'usernames': usernames})
            else:
                pass  # invalid session id
        else:
            pass  # no session id


@asyncio.coroutine
def gone_offline(stream):
    """
    Distributes the users online status to everyone he has dialog with
    """
    while True:
        yield from stream.get()
        packet = {}
        logger.debug(packet)
        yield from fanout_message(ws_connections.keys(), packet)


@asyncio.coroutine
def new_messages_handler(stream):
    """Saves a new chat message to db and distributes msg to connected users
    """
    # TODO: handle no user found exception
    while True:
        packet = yield from stream.get()
        session_id = packet.get('session_key')
        msg = packet.get('message')
        username_opponent = packet.get('username')
        if session_id and msg and username_opponent:
            user_owner = get_user_from_session(session_id)
            if user_owner:
                user_opponent = get_user_model().objects.get(username=username_opponent)
                dialog = get_dialogs_with_user(user_owner, user_opponent)
                if len(dialog) > 0:
                    # Save the message
                    msg = models.Message.objects.create(
                        dialog=dialog[0],
                        sender=user_owner,
                        text=packet['message']
                    )

                    packet['created'] = dj_date(msg.created, "DATETIME_FORMAT")
                    packet['sender_name'] = msg.sender.username

                    # Send the message
                    connections = []
                    if (user_owner.username, user_opponent.username) in ws_connections:
                        connections.append(ws_connections[(user_owner.username, user_opponent.username)])
                    if (user_opponent.username, user_owner.username) in ws_connections:
                        connections.append(ws_connections[(user_opponent.username, user_owner.username)])
                    yield from fanout_message(connections, packet)
                else:
                    pass  # no dialog found
            else:
                pass  # no user_owner
        else:
            pass  # missing one of params


# TODO: use for online/offline status
@asyncio.coroutine
def users_changed_handler(stream):
    """Sends connected client list of currently active users in the chatroom
    """
    while True:
        yield from stream.get()

        # Get list list of current active users
        users = [
            {'username': username, 'uuid': uuid_str}
            for username, uuid_str in ws_connections.values()
            ]

        # Make packet with list of new users (sorted by username)
        packet = {
            'type': 'users-changed',
            'value': sorted(users, key=lambda i: i['username'])
        }
        logger.debug(packet)
        yield from fanout_message(ws_connections.keys(), packet)


@asyncio.coroutine
def main_handler(websocket, path):
    """An Asyncio Task is created for every new websocket client connection
    that is established. This coroutine listens to messages from the connected
    client and routes the message to the proper queue.

    This coroutine can be thought of as a producer.
    """

    # Get users name from the path
    path = path.split('/')
    username = path[2]
    session_id = path[1]
    user_owner = get_user_from_session(session_id)
    if user_owner:
        user_owner = user_owner.username
        # Persist users connection, associate user w/a unique ID
        ws_connections[(user_owner, username)] = websocket

        # While the websocket is open, listen for incoming messages/events
        # if unable to listening for messages/events, then disconnect the client
        try:
            while websocket.open:
                data = yield from websocket.recv()
                if not data: continue
                logger.debug(data)
                try:
                    yield from router.MessageRouter(data)()  # TODO: WTF
                except Exception as e:
                    logger.error('could not route msg', e)

        except websockets.exceptions.InvalidState:  # User disconnected
            # TODO: alert the other user that this user went offline
            pass
        finally:
            del ws_connections[(user_owner, username)]
    else:
        logger.info(f"Got invalid session_id attempt to connect {session_id}")
