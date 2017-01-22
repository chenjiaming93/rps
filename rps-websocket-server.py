#!/usr/bin/env python3

import asyncio
import collections
import configparser
import enum
import json
import logging
import os
import signal
import ssl
import sys
import time
import uuid

import websockets


def sigint_handler(signal, frame):
    print('Interrupted.', file=sys.stderr)
    sys.exit(0)


signal.signal(signal.SIGINT, sigint_handler)


logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S%z'
)
logger = logging.getLogger('rps')
logger.setLevel(logging.INFO)

matchmaker_queue = asyncio.Queue()
matchmaker_livecheck_queue = asyncio.Queue()
judge_queue = asyncio.Queue()
user_cmd_queues = {}  # keyed by uids
opponents = {}  # keyed by uids
games = {}  # keyed by uids; the same Game object is shared by the two competing users

HERE = os.path.dirname(os.path.realpath(__file__))
CONFIGFILE = os.path.join(HERE, 'conf.ini')
CONFIG = configparser.ConfigParser()
CONFIG.read(CONFIGFILE)
ENABLE_SSL = CONFIG.getboolean('ssl', 'enable_ssl', fallback=False)
CERTFILE = CONFIG.get('ssl', 'certfile', fallback='')
KEYFILE = CONFIG.get('ssl', 'keyfile', fallback=None)
PORT = CONFIG.getint('server', 'port', fallback=8443 if ENABLE_SSL else 8080)


class Gesture(enum.Enum):
    ROCK = 0
    PAPER = 1
    SCISSORS = 2
    PASS = -1

    @classmethod
    def _missing_(cls, value):
        return cls.PASS

    def __gt__(self, other):
        if self.__class__ is other.__class__:
            return (self._name_ == 'ROCK' and other._name_ == 'SCISSORS' or
                    self._name_ == 'PAPER' and other._name_ == 'ROCK' or
                    self._name_ == 'SCISSORS' and other._name_ == 'PAPER' or
                    self._name_ != 'PASS' and other._name_ == 'PASS')
        else:
            return NotImplemented

    def __lt__(self, other):
        if self.__class__ is other.__class__:
            return (self._name_ == 'ROCK' and other._name_ == 'PAPER' or
                    self._name_ == 'PAPER' and other._name_ == 'SCISSORS' or
                    self._name_ == 'SCISSORS' and other._name_ == 'ROCK' or
                    self._name_ == 'PASS' and other._name_ != 'PASS')
        else:
            return NotImplemented

    def __eq__(self, other):
        if self.__class__ is other.__class__:
            return self._name_ == other._name_
        else:
            return NotImplemented

    def __ge__(self, other):
        return self.__gt__(other) or self.__eq__(other)

    def __le__(self, other):
        return self.__lt__(other) or self.__eq__(other)

    def __str__(self):
        return self._name_


class User(collections.namedtuple('User', ['uid', 'name'])):
    def __str__(self):
        return f'{self.uid} "{self.name}"'


class Game(object):
    def __init__(self, user1, user2):
        self.user1 = user1
        self.user2 = user2
        self.score1 = self.score2 = 0
        self.winner = None
        self.special = None  # 'leave', 'surrender'
        self.turns = []

    def __str__(self):
        return f'{self.user1} {self.score1} - {self.score2} {self.user2}'

    def turn(self, move1: Gesture, move2: Gesture):
        for move in [move1, move2]:
            if move.__class__ is not Gesture:
                raise TypeError(f'expected Gesture, got {move.__class__}')
        if move1 > move2:
            winner = self.user1
            self.score1 += 1
            if self.score1 >= max(10, self.score2 + 2):
                self.winner = winner
        elif move2 > move1:
            winner = self.user2
            self.score2 += 1
            if self.score2 >= max(10, self.score1 + 2):
                self.winner = winner
        else:
            winner = None
        turn = {
            'winner': winner,
            self.user1.uid: move1,
            self.user2.uid: move2,
        }
        self.turns.append(turn)
        logger.debug(f'{self.user1}: {move1}, {self.user2}: {move2}')
        logger.debug(str(self))
        if self.winner is not None:
            logger.info(f'{self.winner} won')


def generate_uid():
    return uuid.uuid1().hex[:7].upper()


# Returns None is connection closes.
# Returns an empty object if timeout is specified and exceeded.
async def wait_for_message(ws, expected_action,
                           timeout=None, expected_keys=None, validity_test=None,
                           interrupters=None, msg_prefix=None):
    if expected_keys is None:
        expected_keys = []
    if validity_test is None:
        validity_test = lambda _: True
    if interrupters is None:
        interrupters = []
    msg_prefix = '' if msg_prefix is None else f'{msg_prefix}: '

    if timeout is not None:
        target_timestamp = time.time() + timeout
    while True:
        if timeout is not None:
            remaining_time = target_timestamp - time.time()
            if remaining_time < 0:
                return {}
        try:
            if timeout is not None:
                resp = await asyncio.wait_for(ws.recv(), timeout=remaining_time)
            else:
                resp = await ws.recv()
        except websockets.exceptions.ConnectionClosed:
            logger.warning(f'{msg_prefix}connection closed')
            return None
        except asyncio.TimeoutError:
            logger.warning(f'{msg_prefix}expected action "{expected_action}" timed out')
            return {}

        try:
            resp = json.loads(resp)
        except json.JSONDecodeError:
            logger.warning(f'{msg_prefix}cannot decode as JSON, '
                           f'ignored: {resp}')
            continue

        if 'action' in resp and resp['action'] in interrupters:
            logger.warning(f'{msg_prefix}expecting action "{expected_action}", '
                           f'but interrupted by "{resp["action"]}"')
            return resp

        if 'action' not in resp or resp['action'] != expected_action:
            logger.warning(f'{msg_prefix}expecting action "{expected_action}", '
                           f'ignored: {resp}')
            continue

        all_keys_present = False
        for key in expected_keys:
            if key not in resp:
                logger.warning(f'{msg_prefix}expecting key "{key}" '
                               f'for action "{expected_action}", '
                               f'ignored: {resp}')
                break
        else:
            all_keys_present = True
        if not all_keys_present:
            continue

        if not validity_test(resp):
            logger.warning(f'{msg_prefix}message does not pass validity test, '
                           f'ignored: {resp}')
            continue

        return resp


# Returns a bool to indicate whether the message went through.
# (False if timed out or connection dropped.)
#
# If raise_exceptions is True, websockets.exceptions.ConnectionClosed and
# asyncio.TimeoutError are raised as normal; in this case, when it returns the
# return value must be True.
async def send_message(ws, obj, raise_exceptions=True, timeout=None, msg_prefix=None):
    msg_prefix = '' if msg_prefix is None else f'{msg_prefix}: '

    try:
        if timeout is not None:
            await asyncio.wait_for(ws.send(), timeout=timeout)
        else:
            await ws.send(json.dumps(obj))
        return True
    except websockets.exceptions.ConnectionClosed:
        logger.warning(f'{msg_prefix}connection closed')
        if raise_exceptions:
            raise
        else:
            return False
    except asyncio.TimeoutError:
        logger.warning(f'{msg_prefix}sending message timed out: {obj}')
        if raise_exceptions:
            raise
        else:
            return False


# Returns an empty object if timeout is specified and exceeded.
async def wait_for_command(queue, expected_action,
                           timeout=None, validity_test=None,
                           interrupters=None, msg_prefix=None):
    if validity_test is None:
        validity_test = lambda _: True
    if interrupters is None:
        interrupters = []
    msg_prefix = '' if msg_prefix is None else f'{msg_prefix}: '

    if timeout is not None:
        target_timestamp = time.time() + timeout
    while True:
        if timeout is not None:
            remaining_time = target_timestamp - time.time()
            if remaining_time < 0:
                return {}
        try:
            if timeout is not None:
                cmd = await asyncio.wait_for(queue.get(), timeout=remaining_time)
            else:
                cmd = await queue.get()
        except asyncio.TimeoutError:
            logger.warning(f'{msg_prefix}expected command "{expected_action}" timed out')
            return {}

        action = cmd['action']

        if action in interrupters:
            logger.warning(f'{msg_prefix}expecting command "{expected_action}", '
                           f'but interrupted by "{action}"')
            return cmd

        if action != expected_action:
            logger.warning(f'{msg_prefix}expecting command "{expected_action}", '
                           f'ignored: {cmd}')
            continue

        if not validity_test(cmd):
            logger.warning(f'{msg_prefix}command does not pass validity test, '
                           f'ignored: {cmd}')
            continue

        return cmd


async def user_session_logon(ws):
    uid = generate_uid()
    resp = await wait_for_message(
        ws, 'logon',
        expected_keys=['name'],
        validity_test=lambda r: r['name'],  # name nonempty
        msg_prefix=uid,
    )

    if resp is None:
        return None

    name = resp['name']
    if len(name.encode('utf-8')) > 16:
        logger.warning(f'{uid}: name "{name}" too long, truncated to <= 16 bytes')
        name = name.encode('utf-8')[:16].decode('utf-8', 'ignore')

    me = User(uid, name)
    logger.info(f'user {me} logged on')
    return me


# Returns them, game if successfully paired;
# Otherwise (connection dropped at some point), returns None.
async def user_session_wait_for_opponent(ws, me):
    cmd_queue = user_cmd_queues[me.uid]

    # Wait for client's standby message
    resp = await wait_for_message(
        ws, 'standby',
        msg_prefix=me,
    )
    if resp is None:
        return None

    await matchmaker_queue.put(me)
    while True:
        cmd = await wait_for_command(
            cmd_queue, 'match',
            validity_test=lambda c: 'opponent' in c,
            interrupters=['livecheck'],
            msg_prefix=me,
        )
        if cmd['action'] == 'livecheck':
            try:
                await ws.ping()
                await matchmaker_livecheck_queue.put((me, True))
            except websockets.exceptions.ConnectionClosed:
                logger.warning(f'{me}: connection closed')
                await matchmaker_livecheck_queue.put((me, False))
                return None
        else:
            break
    them = cmd['opponent']
    game = games[me.uid]
    return them, game


# Returns a bool indicating whether we should continue with another game.
async def user_session_play_game(ws, me, them, game):
    cmd_queue = user_cmd_queues[me.uid]

    # Commence the game
    try:
        await send_message(ws, {
            'action': 'match',
            'opponent': them.name,
        }, msg_prefix=me)
    except websockets.exceptions.ConnectionClosed:
        await judge_queue.put((me, 'leave'))
        return False

    while True:
        # Get a move
        turn = len(game.turns)
        resp = await wait_for_message(
            ws, 'move',
            timeout=10.5,
            expected_keys=['move', 'turn'],
            validity_test=lambda r, expected_turn=turn: r['turn'] == expected_turn,
            interrupters=['surrender', 'quit'],
            msg_prefix=me,
        )
        if resp is None:
            # Connection dropped
            await judge_queue.put((me, 'leave'))
            return False
        elif resp == {}:
            # Timeout
            move = Gesture.PASS
        elif resp['action'] == 'quit':
            logger.info(f'{me} quit')
            await judge_queue.put((me, 'leave'))
            ws.close()
            return False
        elif resp['action'] == 'surrender':
            logger.info(f'{me} surrendered to {them}')
            await judge_queue.put((me, 'surrender'))
            return True
        else:
            move = Gesture(resp['move'])

        # Submit to judge
        await judge_queue.put((me, move))

        # Wait for instruction from judge
        cmd = await wait_for_command(
            cmd_queue, 'endturn',
            interrupters=['endgame'],
            msg_prefix=me,
        )

        if cmd['action'] == 'endturn':
            last_turn = game.turns[-1]
            winner = (('me' if last_turn['winner'].uid == me.uid else 'them')
                      if last_turn['winner'] else '')
            opponent_move = last_turn[them.uid]

            # Send endturn message to client
            try:
                await send_message(ws, {
                    'action': 'endturn',
                    'winner': winner,
                    'opponent_move': opponent_move.value,
                }, msg_prefix=me)
            except websockets.exceptions.ConnectionClosed:
                judge_queue.put((me, 'leave'))
                return False

        if game.winner:
            # If game is called
            await asyncio.sleep(0.5)
            await send_message(ws, {
                'action': 'endgame',
                'winner': 'me' if game.winner.uid == me.uid else 'them',
                'reason': game.special,
            }, msg_prefix=me)
            break
        else:
            # Give clients 2 seconds to show this round's result
            await asyncio.sleep(2)

    return True


def remove_user(user):
    uid = user.uid
    user_cmd_queues.pop(uid, None)
    opponents.pop(uid, None)
    games.pop(uid, None)


async def user_session(ws, path):
    me = await user_session_logon(ws)
    if me is None:
        return

    try:
        # Initialize user command queue
        cmd_queue = asyncio.Queue()
        user_cmd_queues[me.uid] = cmd_queue

        while True:
            # Wait for matchmaking
            result = await user_session_wait_for_opponent(ws, me)
            if result is None:
                break
            them, game = result

            # Play game
            keep_going = await user_session_play_game(ws, me, them, game)
            if not keep_going:
                break
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        await ws.close()
        logger.info(f'dropped {me}')

        # Wait for a while so that judge could pick up cleanup messages related
        # to this user.
        await asyncio.sleep(30)
        remove_user(me)


async def matchmaker():
    waiting = None
    while True:
        new_user = await matchmaker_queue.get()
        if waiting is not None:
            # Make sure waiting is still alive
            await user_cmd_queues[waiting.uid].put({'action': 'livecheck'})
            user, live = await matchmaker_livecheck_queue.get()
            assert user.uid == waiting.uid
            if not live:
                waiting = new_user
                continue

            u1 = waiting
            u2 = new_user
            opponents[u1.uid] = u2
            opponents[u2.uid] = u1
            game = Game(u1, u2)
            games[u1.uid] = game
            games[u2.uid] = game
            await user_cmd_queues[u1.uid].put({'action': 'match', 'opponent': u2})
            await user_cmd_queues[u2.uid].put({'action': 'match', 'opponent': u1})
            logger.info(f'match made: {u1} and {u2}')
            waiting = None
            continue
        else:
            waiting = new_user


async def judge():
    outstanding = {}
    while True:
        user, move = await judge_queue.get()
        if user.uid in opponents:
            opponent = opponents[user.uid]
        else:
            # For some reason we received a move from a since-dropped
            # user. Not sure why this would happen, but it happened once
            # in private testing, so let's guard against it lest the
            # judge be brought down.
            logger.warning(f'judge: received move from dropped uid {user.uid}')
            continue
        if opponent.uid in outstanding:
            u1 = user
            move1 = move
            u2 = opponent
            move2 = outstanding[u2.uid]
            outstanding.pop(opponent.uid)
            game = games[u1.uid]
            if game.user1.uid != u1.uid:
                u2, u1 = u1, u2
                move2, move1 = move1, move2

            if move1 in ['leave', 'surrender']:
                game.winner = u2
                game.special = move1
                await user_cmd_queues[u2.uid].put({'action': 'endgame'})
            elif move2 in ['leave', 'surrender']:
                game.winner = u1
                game.special = move2
                await user_cmd_queues[u1.uid].put({'action': 'endgame'})
            else:
                game.turn(move1, move2)
                await user_cmd_queues[u1.uid].put({'action': 'endturn'})
                await user_cmd_queues[u2.uid].put({'action': 'endturn'})

            if game.winner is not None:
                # Game finished
                games.pop(u1.uid)
                games.pop(u2.uid)
        else:
            outstanding[user.uid] = move


def sslcontext():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS)
    context.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    context.load_cert_chain(CERTFILE, KEYFILE)
    return context


def main():
    ev = asyncio.get_event_loop()
    ev.run_until_complete(websockets.serve(user_session, '0.0.0.0', PORT, ssl=sslcontext()))
    asyncio.ensure_future(matchmaker(), loop=ev)
    asyncio.ensure_future(judge(), loop=ev)
    ev.run_forever()


if __name__ == '__main__':
    main()
