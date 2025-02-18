"""
Stream V2
For historic reasons stream2.py contains the old api version.
Don't get confused
"""
import asyncio
import logging
import json
from typing import List, Optional
import msgpack
import re
import websockets
import queue

from .common import get_base_url, get_data_stream_url, get_credentials, URL
from .entity import Entity
from .entity_v2 import quote_mapping_v2, trade_mapping_v2, bar_mapping_v2, \
    status_mapping_v2, luld_mapping_v2, Trade, Quote, Bar, StatusV2, LULDV2

log = logging.getLogger(__name__)


def _ensure_coroutine(handler):
    if not asyncio.iscoroutinefunction(handler):
        raise ValueError('handler must be a coroutine function')


class _DataStream():
    def __init__(self,
                 endpoint: str,
                 key_id: str,
                 secret_key: str,
                 raw_data: bool = False) -> None:
        self._endpoint = endpoint
        self._key_id = key_id
        self._secret_key = secret_key
        self._ws = None
        self._running = False
        self._raw_data = raw_data
        self._stop_stream_queue = queue.Queue()
        self._handlers = {
            'trades':    {},
            'quotes':    {},
            'bars':      {},
            'dailyBars': {},
        }
        self._name = 'data'
        self._should_run = True

    async def _connect(self):
        self._ws = await websockets.connect(
            self._endpoint,
            extra_headers={'Content-Type': 'application/msgpack'})
        r = await self._ws.recv()
        msg = msgpack.unpackb(r)
        if msg[0]['T'] != 'success' or msg[0]['msg'] != 'connected':
            raise ValueError('connected message not received')

    async def _auth(self):
        await self._ws.send(
            msgpack.packb({
                'action': 'auth',
                'key':    self._key_id,
                'secret': self._secret_key,
            }))
        r = await self._ws.recv()
        msg = msgpack.unpackb(r)
        if msg[0]['T'] == 'error':
            raise ValueError(msg[0].get('msg', 'auth failed'))
        if msg[0]['T'] != 'success' or msg[0]['msg'] != 'authenticated':
            raise ValueError('failed to authenticate')

    async def _start_ws(self):
        await self._connect()
        await self._auth()
        log.info(f'connected to: {self._endpoint}')

    async def close(self):
        if self._ws:
            await self._ws.close()
            self._ws = None
            self._running = False

    async def stop_ws(self):
        self._should_run = False
        if self._stop_stream_queue.empty():
            self._stop_stream_queue.put_nowait({"should_stop": True})

    async def _consume(self):
        while True:
            if not self._stop_stream_queue.empty():
                self._stop_stream_queue.get(timeout=1)
                await self.close()
                break
            else:
                try:
                    r = await asyncio.wait_for(self._ws.recv(), 5)
                    msgs = msgpack.unpackb(r)
                    for msg in msgs:
                        await self._dispatch(msg)
                except asyncio.TimeoutError:
                    # ws.recv is hanging when no data is received. by using
                    # wait_for we break when no data is received, allowing us
                    # to break the loop when needed
                    pass

    def _cast(self, msg_type, msg):
        result = msg
        if not self._raw_data:
            # convert msgpack timestamp to nanoseconds
            if 't' in msg:
                msg['t'] = msg['t'].seconds * int(1e9) + msg['t'].nanoseconds

            if msg_type == 't':
                result = Trade({
                    trade_mapping_v2[k]: v
                    for k, v in msg.items() if k in trade_mapping_v2
                })
            elif msg_type == 'q':
                result = Quote({
                    quote_mapping_v2[k]: v
                    for k, v in msg.items() if k in quote_mapping_v2
                })
            elif msg_type in ('b', 'd'):
                result = Bar({
                    bar_mapping_v2[k]: v
                    for k, v in msg.items() if k in bar_mapping_v2
                })
            else:
                result = Entity(msg)
        return result

    async def _dispatch(self, msg):
        msg_type = msg.get('T')
        symbol = msg.get('S')
        if msg_type == 't':
            handler = self._handlers['trades'].get(
                symbol, self._handlers['trades'].get('*', None))
            if handler:
                await handler(self._cast(msg_type, msg))
        elif msg_type == 'q':
            handler = self._handlers['quotes'].get(
                symbol, self._handlers['quotes'].get('*', None))
            if handler:
                await handler(self._cast(msg_type, msg))
        elif msg_type == 'b':
            handler = self._handlers['bars'].get(
                symbol, self._handlers['bars'].get('*', None))
            if handler:
                await handler(self._cast(msg_type, msg))
        elif msg_type == 'd':
            handler = self._handlers['dailyBars'].get(
                symbol, self._handlers['dailyBars'].get('*', None))
            if handler:
                await handler(self._cast(msg_type, msg))
        elif msg_type == 'subscription':
            sub = [f'{k}: {msg.get(k, [])}' for k in self._handlers]
            log.info(f'subscribed to {", ".join(sub)}')
        elif msg_type == 'error':
            log.error(f'error: {msg.get("msg")} ({msg.get("code")})')

    def _subscribe(self, handler, symbols, handlers):
        _ensure_coroutine(handler)
        for symbol in symbols:
            handlers[symbol] = handler
        if self._running:
            asyncio.get_event_loop().run_until_complete(self._subscribe_all())

    async def _subscribe_all(self):
        if any(self._handlers.values()):
            msg = {
                k: tuple(v.keys())
                for k, v in self._handlers.items()
                if v
            }
            msg['action'] = 'subscribe'
            await self._ws.send(msgpack.packb(msg))

    async def _unsubscribe(self,
                           trades=(),
                           quotes=(),
                           bars=(),
                           daily_bars=()):
        if trades or quotes or bars or daily_bars:
            await self._ws.send(
                msgpack.packb({
                    'action':    'unsubscribe',
                    'trades':    trades,
                    'quotes':    quotes,
                    'bars':      bars,
                    'dailyBars': daily_bars,
                }))

    async def _run_forever(self):
        # do not start the websocket connection until we subscribe to something
        while not any(self._handlers.values()):
            if not self._stop_stream_queue.empty():
                # the ws was signaled to stop before starting the loop so
                # we break
                self._stop_stream_queue.get(timeout=1)
                return
            await asyncio.sleep(0.1)
        log.info(f'started {self._name} stream')
        self._should_run = True
        self._running = False
        while True:
            try:
                if not self._should_run:
                    # when signaling to stop, this is how we break run_forever
                    log.info("{} stream stopped".format(self._name))
                    return
                if not self._running:
                    log.info("starting {} websocket connection".format(
                        self._name))
                    await self._start_ws()
                    await self._subscribe_all()
                    self._running = True
                await self._consume()
            except websockets.WebSocketException as wse:
                await self.close()
                self._running = False
                log.warn('data websocket error, restarting connection: ' +
                         str(wse))
            except Exception as e:
                log.exception('error during websocket '
                              'communication: {}'.format(str(e)))
            finally:
                await asyncio.sleep(0.01)

    def subscribe_trades(self, handler, *symbols):
        self._subscribe(handler, symbols, self._handlers['trades'])

    def subscribe_quotes(self, handler, *symbols):
        self._subscribe(handler, symbols, self._handlers['quotes'])

    def subscribe_bars(self, handler, *symbols):
        self._subscribe(handler, symbols, self._handlers['bars'])

    def subscribe_daily_bars(self, handler, *symbols):
        self._subscribe(handler, symbols, self._handlers['dailyBars'])

    def unsubscribe_trades(self, *symbols):
        if self._running:
            asyncio.get_event_loop().run_until_complete(
                self._unsubscribe(trades=symbols))
        for symbol in symbols:
            del self._handlers['trades'][symbol]

    def unsubscribe_quotes(self, *symbols):
        if self._running:
            asyncio.get_event_loop().run_until_complete(
                self._unsubscribe(quotes=symbols))
        for symbol in symbols:
            del self._handlers['quotes'][symbol]

    def unsubscribe_bars(self, *symbols):
        if self._running:
            asyncio.get_event_loop().run_until_complete(
                self._unsubscribe(bars=symbols))
        for symbol in symbols:
            del self._handlers['bars'][symbol]

    def unsubscribe_daily_bars(self, *symbols):
        if self._running:
            asyncio.get_event_loop().run_until_complete(
                self._unsubscribe(daily_bars=symbols))
        for symbol in symbols:
            del self._handlers['dailyBars'][symbol]


class DataStream(_DataStream):
    def __init__(self,
                 key_id: str,
                 secret_key: str,
                 base_url: URL,
                 raw_data: bool,
                 feed: str = 'iex'):
        base_url = re.sub(r'^http', 'ws', base_url)
        super().__init__(endpoint=base_url + '/v2/' + feed,
                         key_id=key_id,
                         secret_key=secret_key,
                         raw_data=raw_data,
                         )
        self._handlers['statuses'] = {}
        self._handlers['lulds'] = {}
        self._name = 'stock data'

    def _cast(self, msg_type, msg):
        result = super()._cast(msg_type, msg)
        if not self._raw_data:
            if msg_type == 's':
                result = StatusV2({
                    status_mapping_v2[k]: v
                    for k, v in msg.items() if k in status_mapping_v2
                })
            elif msg_type == 'l':
                result = LULDV2({
                    luld_mapping_v2[k]: v
                    for k, v in msg.items() if k in luld_mapping_v2
                })
        return result

    async def _dispatch(self, msg):
        msg_type = msg.get('T')
        symbol = msg.get('S')
        if msg_type == 's':
            handler = self._handlers['statuses'].get(
                symbol, self._handlers['statuses'].get('*', None))
            if handler:
                await handler(self._cast(msg_type, msg))
        elif msg_type == 'l':
            handler = self._handlers['lulds'].get(
                symbol, self._handlers['lulds'].get('*', None))
            if handler:
                await handler(self._cast(msg_type, msg))
        else:
            await super()._dispatch(msg)

    async def _unsubscribe(self,
                           trades=(),
                           quotes=(),
                           bars=(),
                           daily_bars=(),
                           statuses=(),
                           lulds=()):
        if trades or quotes or bars or daily_bars or statuses or lulds:
            await self._ws.send(
                msgpack.packb({
                    'action':    'unsubscribe',
                    'trades':    trades,
                    'quotes':    quotes,
                    'bars':      bars,
                    'dailyBars': daily_bars,
                    'statuses':  statuses,
                    'lulds':     lulds,
                }))

    def subscribe_statuses(self, handler, *symbols):
        self._subscribe(handler, symbols, self._handlers['statuses'])

    def subscribe_lulds(self, handler, *symbols):
        self._subscribe(handler, symbols, self._handlers['lulds'])

    def unsubscribe_statuses(self, *symbols):
        if self._running:
            asyncio.get_event_loop().run_until_complete(
                self._unsubscribe(statuses=symbols))
        for symbol in symbols:
            del self._handlers['statuses'][symbol]

    def unsubscribe_lulds(self, *symbols):
        if self._running:
            asyncio.get_event_loop().run_until_complete(
                self._unsubscribe(lulds=symbols))
        for symbol in symbols:
            del self._handlers['lulds'][symbol]


class CryptoDataStream(_DataStream):
    def __init__(self,
                 key_id: str,
                 secret_key: str,
                 base_url: URL,
                 raw_data: bool,
                 exchanges: Optional[List[str]] = None):
        self._key_id = key_id
        self._secret_key = secret_key
        base_url = re.sub(r'^http', 'ws', base_url)
        endpoint = base_url + '/v1beta1/crypto'
        if exchanges:
            if isinstance(exchanges, str):
                endpoint += '?exchanges=' + exchanges
            else:
                endpoint += '?exchanges=' + ','.join(exchanges)
        super().__init__(endpoint=endpoint,
                         key_id=key_id,
                         secret_key=secret_key,
                         raw_data=raw_data,
                         )
        self._name = 'crypto data'


class TradingStream:
    def __init__(self,
                 key_id: str,
                 secret_key: str,
                 base_url: URL):
        self._key_id = key_id
        self._secret_key = secret_key
        base_url = re.sub(r'^http', 'ws', base_url)
        self._endpoint = base_url + '/stream/'
        self._trade_updates_handler = None
        self._ws = None
        self._running = False
        self._stop_stream_queue = queue.Queue()
        self._should_run = True

    async def _connect(self):
        self._ws = await websockets.connect(self._endpoint)

    async def _auth(self):
        await self._ws.send(
            json.dumps({
                'action': 'authenticate',
                'data':   {
                    'key_id':     self._key_id,
                    'secret_key': self._secret_key,
                }
            }))
        r = await self._ws.recv()
        msg = json.loads(r)
        if msg.get('data').get('status') != 'authorized':
            raise ValueError('failed to authenticate')

    async def _dispatch(self, msg):
        stream = msg.get('stream')
        if stream == 'trade_updates':
            if self._trade_updates_handler:
                await self._trade_updates_handler(Entity(msg.get('data')))

    async def _subscribe_trade_updates(self):
        if self._trade_updates_handler:
            await self._ws.send(
                json.dumps({
                    'action': 'listen',
                    'data':   {
                        'streams': ['trade_updates']
                    }
                }))

    def subscribe_trade_updates(self, handler):
        _ensure_coroutine(handler)
        self._trade_updates_handler = handler
        if self._running:
            asyncio.get_event_loop().run_until_complete(
                self._subscribe_trade_updates())

    async def _start_ws(self):
        await self._connect()
        await self._auth()
        log.info(f'connected to: {self._endpoint}')
        await self._subscribe_trade_updates()

    async def _consume(self):
        while True:
            if not self._stop_stream_queue.empty():
                self._stop_stream_queue.get(timeout=1)
                await self.close()
                break
            else:
                try:
                    r = await asyncio.wait_for(self._ws.recv(), 5)
                    msg = json.loads(r)
                    await self._dispatch(msg)
                except asyncio.TimeoutError:
                    # ws.recv is hanging when no data is received. by using
                    # wait_for we break when no data is received, allowing us
                    # to break the loop when needed
                    pass

    async def _run_forever(self):
        # do not start the websocket connection until we subscribe to something
        while not self._trade_updates_handler:
            if not self._stop_stream_queue.empty():
                self._stop_stream_queue.get(timeout=1)
                return
            await asyncio.sleep(0.1)
        log.info('started trading stream')
        self._should_run = True
        self._running = False
        while True:
            try:
                if not self._should_run:
                    log.info("Trading stream stopped")
                    break
                if not self._running:
                    log.info("starting trading websocket connection")
                    await self._start_ws()
                    self._running = True
                    await self._consume()
            except websockets.WebSocketException as wse:
                await self.close()
                self._running = False
                log.warn('trading stream websocket error, restarting ' +
                         ' connection: ' + str(wse))
            except Exception as e:
                log.exception('error during websocket '
                              'communication: {}'.format(str(e)))
            finally:
                await asyncio.sleep(0.01)

    async def close(self):
        if self._ws:
            await self._ws.close()
            self._ws = None
            self._running = False

    async def stop_ws(self):
        self._should_run = False
        if self._stop_stream_queue.empty():
            self._stop_stream_queue.put_nowait({"should_stop": True})


class Stream:
    def __init__(self,
                 key_id: str = None,
                 secret_key: str = None,
                 base_url: URL = None,
                 data_stream_url: URL = None,
                 data_feed: str = 'iex',
                 raw_data: bool = False,
                 crypto_exchanges: Optional[List[str]] = None):
        self._key_id, self._secret_key, _ = get_credentials(key_id, secret_key)
        self._base_url = base_url or get_base_url()
        self._data_steam_url = data_stream_url or get_data_stream_url()

        self._trading_ws = TradingStream(self._key_id,
                                         self._secret_key,
                                         self._base_url)
        self._data_ws = DataStream(self._key_id,
                                   self._secret_key,
                                   self._data_steam_url,
                                   raw_data,
                                   data_feed.lower())
        self._crypto_ws = CryptoDataStream(self._key_id,
                                           self._secret_key,
                                           self._data_steam_url,
                                           raw_data,
                                           crypto_exchanges)

    def subscribe_trade_updates(self, handler):
        self._trading_ws.subscribe_trade_updates(handler)

    def subscribe_trades(self, handler, *symbols):
        self._data_ws.subscribe_trades(handler, *symbols)

    def subscribe_quotes(self, handler, *symbols):
        self._data_ws.subscribe_quotes(handler, *symbols)

    def subscribe_bars(self, handler, *symbols):
        self._data_ws.subscribe_bars(handler, *symbols)

    def subscribe_daily_bars(self, handler, *symbols):
        self._data_ws.subscribe_daily_bars(handler, *symbols)

    def subscribe_statuses(self, handler, *symbols):
        self._data_ws.subscribe_statuses(handler, *symbols)

    def subscribe_lulds(self, handler, *symbols):
        self._data_ws.subscribe_lulds(handler, *symbols)

    def subscribe_crypto_trades(self, handler, *symbols):
        self._crypto_ws.subscribe_trades(handler, *symbols)

    def subscribe_crypto_quotes(self, handler, *symbols):
        self._crypto_ws.subscribe_quotes(handler, *symbols)

    def subscribe_crypto_bars(self, handler, *symbols):
        self._crypto_ws.subscribe_bars(handler, *symbols)

    def subscribe_crypto_daily_bars(self, handler, *symbols):
        self._crypto_ws.subscribe_daily_bars(handler, *symbols)

    def on_trade_update(self, func):
        self.subscribe_trade_updates(func)
        return func

    def on_trade(self, *symbols):
        def decorator(func):
            self.subscribe_trades(func, *symbols)
            return func

        return decorator

    def on_quote(self, *symbols):
        def decorator(func):
            self.subscribe_quotes(func, *symbols)
            return func

        return decorator

    def on_bar(self, *symbols):
        def decorator(func):
            self.subscribe_bars(func, *symbols)
            return func

        return decorator

    def on_daily_bar(self, *symbols):
        def decorator(func):
            self.subscribe_daily_bars(func, *symbols)
            return func

        return decorator

    def on_status(self, *symbols):
        def decorator(func):
            self.subscribe_statuses(func, *symbols)
            return func

        return decorator

    def on_luld(self, *symbols):
        def decorator(func):
            self.subscribe_lulds(func, *symbols)
            return func

        return decorator

    def on_crypto_trade(self, *symbols):
        def decorator(func):
            self.subscribe_crypto_trades(func, *symbols)
            return func

        return decorator

    def on_crypto_quote(self, *symbols):
        def decorator(func):
            self.subscribe_crypto_quotes(func, *symbols)
            return func

        return decorator

    def on_crypto_bar(self, *symbols):
        def decorator(func):
            self.subscribe_crypto_bars(func, *symbols)
            return func

        return decorator

    def on_crypto_daily_bar(self, *symbols):
        def decorator(func):
            self.subscribe_crypto_daily_bars(func, *symbols)
            return func

        return decorator

    def unsubscribe_trades(self, *symbols):
        self._data_ws.unsubscribe_trades(*symbols)

    def unsubscribe_quotes(self, *symbols):
        self._data_ws.unsubscribe_quotes(*symbols)

    def unsubscribe_bars(self, *symbols):
        self._data_ws.unsubscribe_bars(*symbols)

    def unsubscribe_daily_bars(self, *symbols):
        self._data_ws.unsubscribe_daily_bars(*symbols)

    def unsubscribe_statuses(self, *symbols):
        self._data_ws.unsubscribe_statuses(*symbols)

    def unsubscribe_lulds(self, *symbols):
        self._data_ws.unsubscribe_lulds(*symbols)

    def unsubscribe_crypto_trades(self, *symbols):
        self._crypto_ws.unsubscribe_trades(*symbols)

    def unsubscribe_crypto_quotes(self, *symbols):
        self._crypto_ws.unsubscribe_quotes(*symbols)

    def unsubscribe_crypto_bars(self, *symbols):
        self._crypto_ws.unsubscribe_bars(*symbols)

    def unsubscribe_crypto_daily_bars(self, *symbols):
        self._crypto_ws.unsubscribe_daily_bars(*symbols)

    async def _run_forever(self):
        await asyncio.gather(self._trading_ws._run_forever(),
                             self._data_ws._run_forever(),
                             self._crypto_ws._run_forever())

    def run(self):
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(self._run_forever())
        except KeyboardInterrupt:
            print('keyboard interrupt, bye')
            pass

    async def stop_ws(self):
        """
        Signal the ws connections to stop listenning to api stream.
        """
        if self._trading_ws:
            await self._trading_ws.stop_ws()

        if self._data_ws:
            await self._data_ws.stop_ws()

        if self._crypto_ws:
            await self._crypto_ws.stop_ws()

    def is_open(self):
        """
        Checks if either of the websockets is open
        :return:
        """
        open_ws = self._trading_ws._ws or self._data_ws._ws or self._crypto_ws._ws  # noqa
        if open_ws:
            return True
        return False
