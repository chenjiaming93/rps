#!/usr/bin/env python3

import argparse
import asyncio
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
from random import SystemRandom
# Use random.SystemRandom as generator to make bot moves unguessable
random = SystemRandom()

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

ev = asyncio.get_event_loop()

matchmaker_queue = asyncio.Queue()
matchmaker_livecheck_queue = asyncio.Queue()
judge_queue = asyncio.Queue()

HERE = os.path.dirname(os.path.realpath(__file__))
CONFIGFILE = os.path.join(HERE, 'conf.ini')
CONFIG = configparser.ConfigParser()
CONFIG.read(CONFIGFILE)
ENABLE_SSL = CONFIG.getboolean('ssl', 'enable_ssl', fallback=False)
CERTFILE = CONFIG.get('ssl', 'certfile', fallback='')
KEYFILE = CONFIG.get('ssl', 'keyfile', fallback=None)
PORT = CONFIG.getint('server', 'port', fallback=8443 if ENABLE_SSL else 8080)


class User(object):
    # affiliation is an optional User object used for bots, indicating
    # which human user the bot is spawned for.
    def __init__(self, uid, name, affiliation=None):
        self.uid = uid
        self.name = name
        self.affiliation = affiliation

        self.queue = asyncio.Queue()
        self.opponent = None
        self.game = None

        self.dropped = False  # Set to True at the end of user session

    def __eq__(self, other):
        if self.__class__ is other.__class__:
            return self.uid == other.uid
        else:
            return NotImplemented

    def __str__(self):
        return f'{self.uid} "{self.name}"'


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
            logger.info(f'{msg_prefix}connection closed')
            return None
        except asyncio.TimeoutError:
            logger.debug(f'{msg_prefix}expected action "{expected_action}" timed out')
            return {}

        try:
            resp = json.loads(resp)
        except json.JSONDecodeError:
            logger.warning(f'{msg_prefix}cannot decode as JSON, '
                           f'ignored: {resp}')
            continue

        if 'action' in resp and resp['action'] in interrupters:
            logger.debug(f'{msg_prefix}expecting action "{expected_action}", '
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
        logger.info(f'{msg_prefix}connection closed')
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
            logger.debug(f'{msg_prefix}expected command "{expected_action}" timed out')
            return {}

        action = cmd['action']

        if action in interrupters:
            logger.debug(f'{msg_prefix}expecting command "{expected_action}", '
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


# Returns True if successfully paired;
# Otherwise (connection dropped at some point), returns False.
async def user_session_wait_for_opponent(ws, me):
    # Wait for client's standby message
    resp = await wait_for_message(
        ws, 'standby',
        msg_prefix=me,
    )
    if resp is None:
        return False

    async def listen_for_bot_request():
        resp = await wait_for_message(
            ws, 'bot_request',
            msg_prefix=me,
        )
        if resp is None:
            return False
        # Request a bot
        await matchmaker_queue.put((me, True))

    await matchmaker_queue.put((me, False))  # Request a human
    bot_request_listener = asyncio.ensure_future(listen_for_bot_request(), loop=ev)

    while True:
        cmd = await wait_for_command(
            me.queue, 'match',
            validity_test=lambda c: 'opponent' in c,
            interrupters=['livecheck'],
            msg_prefix=me,
        )
        if cmd['action'] == 'livecheck':
            try:
                await ws.ping()
                await matchmaker_livecheck_queue.put((me, True))
            except websockets.exceptions.ConnectionClosed:
                logger.info(f'{me}: connection closed')
                await matchmaker_livecheck_queue.put((me, False))
                return False
        else:
            break

    # Cancel the bot request listener task if it hasn't finished already
    bot_request_listener.cancel()

    return True


# Returns a bool indicating whether we should continue with another game.
async def user_session_play_game(ws, me):
    them = me.opponent
    game = me.game

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
            me.queue, 'endturn',
            interrupters=['endgame'],
            msg_prefix=me,
        )

        if cmd['action'] == 'endturn':
            last_turn = game.turns[-1]
            if last_turn['winner']:
                winner = 'me' if last_turn['winner'] == me else 'them'
            else:
                winner = ''
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
                'winner': 'me' if game.winner == me else 'them',
                'reason': game.special,
            }, msg_prefix=me)
            # Reset user's opponent and game
            me.opponent = None
            me.game = None
            break
        else:
            # Give clients 2 seconds to show this round's result
            await asyncio.sleep(2)

    return True


async def user_session(ws, path):
    me = await user_session_logon(ws)
    if me is None:
        return

    try:
        while True:
            # Wait for matchmaking
            paired = await user_session_wait_for_opponent(ws, me)
            if not paired:
                break

            # Play game
            keep_going = await user_session_play_game(ws, me)
            if not keep_going:
                break
    except websockets.exceptions.ConnectionClosed:
        pass
    except TimeoutError:
        # Sometimes there's an uncaught TimeoutError lower down in
        # asyncio/selector_events.py:724:
        #
        #     data = self._sock.recv(self.max_size)
        #
        # Kind of a leak in the websockets package.
        logger.warning(f'{me}: uncaught TimeoutError')
        # Send a leave message to the judge just to be safe.
        judge_queue.put((me, 'leave'))
    finally:
        await ws.close()
        me.dropped = True
        logger.info(f'dropped {me}')


# Spawn a bot to be matched against the given user
def spawn_bot(user):
    BOTNAMES = [
        'Abathur',
        'Alarak',
        'Aldaris',
        'Alexei Stukov',
        'Amon',
        'Arcturus Mengsk',
        'Ariel Hanson',
        'Artanis',
        'Daggoth',
        'Dehaka',
        'Edmund Duke',
        'Egon Stetmann',
        'Emil Narud',
        'Fenix',
        'Gabriel Tosh',
        'Gerard DuGalle',
        'Horace Warfield',
        'Jim Raynor',
        'Karax',
        'Matt Horner',
        'Nova Terra',
        'Raszagal',
        'Rohana',
        'Rory Swann',
        'Samir Duran',
        'Sarah Kerrigan',
        'Selendis',
        'Tassadar',
        'The Overmind',
        'Tychus Findlay',
        'Valerian Mengsk',
        'Zagara',
        'Zasz',
        'Zeratul',
        'Zurvan',
    ]
    return User(generate_uid(), random.choice(BOTNAMES), affiliation=user)


async def bot_session(bot):
    try:
        cmd = await wait_for_command(
            bot.queue, 'match',
            interrupters=['terminate'],
            msg_prefix=bot,
        )
        if cmd['action'] == 'terminate':
            return

        while True:
            move = Gesture(random.randrange(3))
            await judge_queue.put((bot, move))
            await wait_for_command(
                bot.queue, 'endturn',
                interrupters=['endgame'],
                msg_prefix=bot,
            )
            if bot.game.winner:
                break
    finally:
        bot.dropped = True
        logger.info(f'bot {bot}: mission complete')


async def matchmaker():
    waiting = None
    while True:
        # If a bot is waiting, kick it out
        if waiting is not None and waiting.affiliation is not None:
            await waiting.queue.put({'action': 'terminate'})
            waiting = None

        new_user, bot_request = await matchmaker_queue.get()

        on_hold = None
        if bot_request:
            # We put new_user into waiting mode (if not already) and
            # spawn a bot as the new user for the matchmaker to assign
            if waiting is None:
                waiting = new_user
            # If another user is currently waiting in line, put them on hold
            elif waiting != new_user:
                on_hold = waiting
                waiting = new_user

            bot = spawn_bot(new_user)
            asyncio.ensure_future(bot_session(bot), loop=ev)
            new_user = bot

        if waiting is not None:
            # Make sure waiting is still alive
            await waiting.queue.put({'action': 'livecheck'})
            try:
                while True:
                    user, live = await asyncio.wait_for(matchmaker_livecheck_queue.get(),
                                                        timeout=10)
                    if user != waiting:
                        logger.warning(f'matchmaker: livechecking {waiting} '
                                       f'but heard from {user} instead; ignored')
                        continue
                    else:
                        break
            except asyncio.TimeoutError:
                # Hasn't heard back from livecheck in ten seconds,
                # assume the connection has dropped
                live = False

            if not live:
                waiting = new_user
                continue

            u1 = waiting
            u2 = new_user
            u1.opponent = u2
            u2.opponent = u1
            game = Game(u1, u2)
            u1.game = game
            u2.game = game
            await u1.queue.put({'action': 'match', 'opponent': u2})
            await u2.queue.put({'action': 'match', 'opponent': u1})
            logger.info(f'match made: {u1} and {u2}')
            waiting = None
            continue
        else:
            waiting = new_user

        # Restore the on-hold user, if any
        if on_hold:
            waiting = on_hold


async def judge():
    outstanding = {}
    while True:
        user, move = await judge_queue.get()

        if user.dropped:
            # For some reason we received a move from a since-dropped
            # user. Not sure why this would happen, but it happened once
            # in private testing, so let's guard against it lest the
            # judge be brought down.
            logger.warning(f'judge: received move from dropped user {user}; ignored')
            continue

        opponent = user.opponent
        if not opponent:
            # This is also unexpected, but better be safe than sorry
            logger.warning(f'judge: received move from user {user} '
                           f'who isn\'t paired to anyone; ignored')
            continue

        # Similarly, we check if the opponent has been dropped
        if opponent.dropped:
            # Opponent has been dropped! And somehow they did not send a
            # farewell message or the message was somehow eaten. Damn.
            logger.warning(f'judge: the opponent {opponent} of {user}'
                           f'appears to have been dropped')
            user.game.winner = user
            user.game.special = 'leave'
            outstanding.pop(opponent.uid, None)
            await user.queue.put({'action': 'endgame'})
            continue

        if opponent.uid in outstanding:
            u1 = user
            move1 = move
            u2 = opponent
            move2 = outstanding[u2.uid]
            outstanding.pop(opponent.uid)

            game = u1.game
            if game.user1 != u1:
                u2, u1 = u1, u2
                move2, move1 = move1, move2

            if move1 in ['leave', 'surrender']:
                game.winner = u2
                game.special = move1
                await u2.queue.put({'action': 'endgame'})
            elif move2 in ['leave', 'surrender']:
                game.winner = u1
                game.special = move2
                await u1.queue.put({'action': 'endgame'})
            else:
                game.turn(move1, move2)
                await u1.queue.put({'action': 'endturn'})
                await u2.queue.put({'action': 'endturn'})
        else:
            outstanding[user.uid] = move


def sslcontext():
    if not ENABLE_SSL:
        return None

    context = ssl.SSLContext(ssl.PROTOCOL_TLS)
    context.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    context.load_cert_chain(CERTFILE, KEYFILE)
    return context


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--debug', action='store_true')
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    ev.run_until_complete(websockets.serve(user_session, '0.0.0.0', PORT, ssl=sslcontext()))
    asyncio.ensure_future(matchmaker(), loop=ev)
    asyncio.ensure_future(judge(), loop=ev)
    ev.run_forever()


if __name__ == '__main__':
    main()
