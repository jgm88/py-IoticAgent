# Copyright (c) 2016 Iotic Labs Ltd. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/Iotic-Labs/py-IoticAgent/blob/master/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint: disable=too-many-lines

from __future__ import unicode_literals

from hashlib import sha256 as hashfunc
from re import compile as re_compile
from hmac import new as hmacNew
from binascii import a2b_hex
from collections import OrderedDict
import string
import random
from threading import Thread
from time import sleep
from struct import Struct
import logging

from ubjson import dumpb as ubjdumpb, loadb as ubjloadb

from .AmqpLink import AmqpLink
from .Exceptions import LinkException, LinkShutdownException
from .RequestEvent import RequestEvent
from .MessageDecoder import decode_sent_msg, decode_rcvd_msg
from .ThreadSafeDict import ThreadSafeDict
from .Validation import Validation, VALIDATION_MAX_ENCODED_LENGTH
from .Compressors import COMPRESSORS, OversizeException
from .PreparedMessage import PreparedMessage
from .compat import (PY3, py_version_check, ssl_version_check, monotonic, Queue, Empty, u, int_types, unicode_type,
                     raise_from, Lock, Event)
from .ThreadPool import ThreadPool
from .Mime import valid_mimetype, expand_idx_mimetype
from .utils import version_string_to_tuple
from .Const import (C_CREATE, C_UPDATE, C_DELETE, C_LIST, E_COMPLETE, E_FAILED, E_PROGRESS, E_FAILED_CODE_LOWSEQNUM,
                    E_CREATED, E_DUPLICATED, E_DELETED, E_FEEDDATA, E_CONTROLREQ, E_SUBSCRIBED, E_RENAMED, E_REASSIGNED,
                    R_PING, R_ENTITY, R_FEED, R_CONTROL, R_SUB, R_ENTITY_META, R_FEED_META, R_CONTROL_META,
                    R_VALUE_META, R_ENTITY_TAG_META, R_FEED_TAG_META, R_CONTROL_TAG_META, R_SEARCH, R_DESCRIBE, W_SEQ,
                    W_HASH, W_COMPRESSION, W_MESSAGE, COMP_NONE, M_RESOURCE, M_TYPE, M_CLIENTREF, M_ACTION, M_PAYLOAD,
                    M_RANGE, P_CODE, P_RESOURCE, P_MESSAGE, P_LID, P_ENTITY_LID, P_FEED_ID, P_POINT_ID, P_DATA, P_MIME,
                    P_POINT_TYPE, COMP_DEFAULT, COMP_SIZE, COMP_LZ4F)

py_version_check()
ssl_version_check()

logger = logging.getLogger(__name__)
DEBUG_ENABLED = (logger.getEffectiveLevel() == logging.DEBUG)

_SEQ_WRAP_SIZE = 2**63 - 1  # sequence numbers wrap when larger than this
_SEQ_MAX_AHEAD = 1024  # how far head to allow sequence numbers (form container) before warning

# Specified separately since there isn't a direct mapping to Const.E_*
_CB_DEBUG_KA = 0        # amqp keepalive
_CB_DEBUG_SEND = 1      # every published message
_CB_DEBUG_BAD = 2       # any badly signed messages
_CB_DEBUG_RCVD = 3      # any receIved messages (not ka, not bad)
_CB_CREATED = 4         # a resource has been created
_CB_DUPLICATE = 5       # a resource already exists
_CB_RENAMED = 6         # a resource has been renamed (lid/nickname)
_CB_DELETED = 7         # a resource has been deleted
_CB_FEED = 8            # feedid -> function (1:1)
_CB_FEEDDATA = 9        # Catch All FEEDDATA Messages!
_CB_CONTROL = 10        # lid -> {pid -> func} (1:1)
_CB_CONTROLREQ = 11     # Catch All CONTROLREQ Messages!
_CB_REASSIGNED = 12     # Unsolicited REASSIGNED message
_CB_SUBSCRIPTION = 13   # Unsolicited SUBSCRIPTION count changed message

# callback CRUD types which should be serialised
_CB_CRUD_TYPES = frozenset((_CB_CREATED, _CB_DUPLICATE, _CB_RENAMED, _CB_DELETED, _CB_REASSIGNED))

# expected content type for incoming messages
_CONTENT_TYPE_PATTERN = re_compile(r'^(?%si)application/ubjson$' % ('a' if PY3 else ''))

# unsolicited responses which never have a reference
_RSP_NO_REF = frozenset((E_FEEDDATA, E_SUBSCRIBED))
# unsolicited responses for which reference is set by the container
_RSP_CONTAINER_REF = frozenset((E_CONTROLREQ,))
# (un)solicited responses for which a reference is optional
_RSP_OPTIONAL_OR_NO_REF = frozenset((E_CREATED, E_DELETED, E_RENAMED, E_REASSIGNED, E_SUBSCRIBED))
# responses which signify request completion
_RSP_TYPE_FINISH = frozenset((E_COMPLETE, E_FAILED, E_DUPLICATED))
# responses which signify successful request completion
_RSP_TYPE_SUCCESS = _RSP_TYPE_FINISH - {E_FAILED}
# resonses which signify a resource now (or already) exist
_RSP_TYPE_CREATION = frozenset((E_CREATED, E_DUPLICATED))
# responses which result in callbacks for which the whole payload is passed along
_RSP_PAYLOAD_CB_MAPPING = {E_CREATED: _CB_CREATED,
                           E_DUPLICATED: _CB_DUPLICATE,
                           E_RENAMED: _CB_RENAMED,
                           E_DELETED: _CB_DELETED,
                           E_REASSIGNED: _CB_REASSIGNED}


class Client(object):  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Iotic Labs QAPI Client
    """

    # QAPI version targeted by Core client
    __qapi_version = '0.6.0'

    def __init__(self, host, vhost, epId, passwd, token, prefix='', seqnum=1, lang=None,
                 sslca=None, lowseq_resend=True, network_retry_timeout=300, auto_encode_decode=True):
        """
        `host` amqp broker "host:port"
        `vhost` amqp broker virtual host
        `epId` amqp username
        `passwd` amqp broker password
        `token` ascii hex token secret
        `prefix` (default '') epId prefix depends on container settings
        `seqnum` (default 1) sequence number if known
        `lang` (default None) language code to use for relevant (meta data) requests. If not set, the container
               container default will be used. See also default_lang.
        `sslca` (default None) path to file of broker SSL Certificate (IF NOT using Public)
        `lowseq_resend` (default True) If True & FAILED,LOWSEQNUM is received the last request will be resent
        `network_retry_timeout` (default 300s) If 0 no retry/timeout else seconds.
        `auto_encode_decode` (default True) Automatically encode/decode text (utf8) and dictionaries (ubjson) when
                             sending/receiving point data. When sending, only applies if mime type not specified.
        """
        logger.debug("__init__ config host='%s', vhost='%s', epId='%s', passwd='%s', token='%s', prefix='%s'"
                     " , seqnum='%s', sslca='%s' network_retry_timeout=%s",
                     host, vhost, epId, passwd, token, prefix, seqnum, sslca, network_retry_timeout)
        #
        self.__epId = Validation.guid_check_convert(epId)
        self.__default_lang = Validation.lang_check_convert(lang, allow_none=True)
        #
        try:
            self.__token = a2b_hex(token.encode('ascii'))
        except Exception as ex:  # pylint: disable=broad-except
            raise_from(ValueError('token invalid'), ex)
        #
        try:
            self.__seqnum = int(seqnum)
            if self.__seqnum < 0:
                raise ValueError('seqnum negative')
        except Exception as ex:  # pylint: disable=broad-except
            raise_from(ValueError('seqnum invalid'), ex)
        #
        for param in ('host', 'vhost', 'passwd'):
            Validation.check_convert_string(locals().get(param))
        # Only applies until ping response (see start() method)
        self.set_compression(comp=COMP_NONE)
        #
        self.__seqnum_lock = Lock()
        self.__reqpre = self.__rnd_string(6)
        self.__auto_encode_decode = bool(auto_encode_decode)
        #
        self.__amqplink = AmqpLink(host, vhost, prefix, self.__epId, passwd, self.__dispatch_msg, self.__dispatch_ka, sslca=sslca)
        # seq (from container - initial value used to surpress warning on first message from container)
        self.__conn_seqnum = -1
        # __auto_resend
        self.__lowseq_resend = bool(lowseq_resend)
        self.__lowseq_resend_stash = ThreadSafeDict()
        # (Core.Client has not been .start or is .stop)
        self.__end = Event()
        self.__end.set()
        # network_retry
        self.__network_retry_thread = None
        try:
            self.__network_retry_timeout = int(network_retry_timeout)
            if self.__network_retry_timeout < 0:
                raise ValueError('network_retry_timeout negative')
        except Exception as ex:  # pylint: disable=broad-except
            raise_from(ValueError('network_retry_timeout invalid'), ex)
        self.__network_retry_queue = None
        # __requests stores all incoming messages {'requestId': event}
        self.__requests = ThreadSafeDict()
        #
        # Remember pending subscriptions & control callbacks.  __dispatch_msg will bind them when CREATED.
        self.__pending_subs = ThreadSafeDict()
        self.__pending_controls = ThreadSafeDict()
        #
        # __callbacks stores (un)solicited message registered callbacks
        self.__callbacks = ThreadSafeDict((type_, []) for type_ in
                                          (_CB_DEBUG_KA, _CB_DEBUG_SEND, _CB_DEBUG_BAD, _CB_DEBUG_RCVD, _CB_CREATED,
                                           _CB_DUPLICATE, _CB_RENAMED, _CB_DELETED, _CB_FEEDDATA, _CB_CONTROLREQ,
                                           _CB_REASSIGNED, _CB_SUBSCRIPTION))
        # mappings of pointId -> callback list
        self.__callbacks.update({type_: {} for type_ in (_CB_FEED, _CB_CONTROL)})
        #
        # Background thread for forwarding resource CRUD related events, including completion of CRUD requests
        # All such callbacks happen in a single thread so ordering of potentially related events is consistent.
        self.__crud_threadpool = ThreadPool(daemonic=True)
        #
        # Callback threadpool for any callbacks not covered by CRUD thread
        self.__threadpool = ThreadPool(num_workers=2, daemonic=True)
        #
        # Store container params from request_ping response
        self.__container_params = None

    @property
    def epId(self):
        """EP/Agent id in use for this client"""
        return self.__epId

    @property
    def default_lang(self):
        """Language in use when not explicitly specified (in meta related requests). Will be set to container default
        if was not set in constructor. Before calling start() this might be None."""
        return self.__default_lang

    @property
    def container_params(self):
        """Returns container configuration parameters as a mapping. Will be empty before start() has been called"""
        if self.__container_params is None:
            return {}
        else:
            return self.__container_params.copy()

    def restore_event(self, requestId):
        """restore an event based on the requestId.

        For example if the user app had to shutdown with pending requests.
        The user can rebuild the Events they were waiting for based on the requestId(s).

        :param requestId
        :return RequestEvent
        """
        ret = False
        with self.__requests:
            if requestId not in self.__requests:
                ret = True
                self.__requests[requestId] = RequestEvent(requestId)
        return ret

    def register_callback_created(self, func):
        """Register a callback function to receive QAPI Unsolicited (resource) CREATED. The
        callback receives a single argument - the inner message.
        """
        Validation.callable_check(func)
        with self.__callbacks:
            self.__callbacks[_CB_CREATED].append(func)

    def register_callback_duplicate(self, func):
        """Register a callback function to receive QAPI Unsolicited (resource) DUPLICATE. The
        callback receives a single argument - the inner message.
        """
        Validation.callable_check(func)
        with self.__callbacks:
            self.__callbacks[_CB_DUPLICATE].append(func)

    def register_callback_renamed(self, func):
        """Register a callback function to receive QAPI Unsolicited (resource) RENAMED. The
        callback receives a single argument - the inner message.
        """
        Validation.callable_check(func)
        with self.__callbacks:
            self.__callbacks[_CB_RENAMED].append(func)

    def register_callback_deleted(self, func):
        """Register a callback function to receive QAPI Unsolicited (resource) DELETED. The
        callback receives a single argument - the inner message.
        """
        Validation.callable_check(func)
        with self.__callbacks:
            self.__callbacks[_CB_DELETED].append(func)

    def register_callback_reassigned(self, func):
        """Register a callback function to receive QAPI Unsolicited (entity) REASSIGNED. The
        callback receives a single argument - the inner message.
        """
        Validation.callable_check(func)
        with self.__callbacks:
            self.__callbacks[_CB_REASSIGNED].append(func)

    def register_callback_subscription(self, func):
        """Register a callback function to receive subscription count changes. The callback receives
        a single argument - a dict with r, the resource type (R_FEED or R_CONTROL), entityLid (the
        nickname of the thing to which the point belongs), lid (the nickname of the point) and
        subCount (the current subscription count). Note: Subscription count changes can occur for
        each new (or deleted) subscription.
        """
        Validation.callable_check(func)
        with self.__callbacks:
            self.__callbacks[_CB_SUBSCRIPTION].append(func)

    def register_callback_debug_send(self, func):
        """Register a callback function for every sent message, including on retries. The callback
           receives a single argument - the sent message in raw byte form.
        """
        Validation.callable_check(func)
        with self.__callbacks:
            self.__callbacks[_CB_DEBUG_SEND].append(func)

    def register_callback_debug_rcvd(self, func):
        """Register a callback function to every GOOD recieved message. The callback receives a single
        argument - the unwrapped message as a dict.
        """
        Validation.callable_check(func)
        with self.__callbacks:
            self.__callbacks[_CB_DEBUG_RCVD].append(func)

    def register_callback_debug_bad(self, func):
        """Register a callback function to every BAD received message (EG bad sign). The callback
        receives two arguments - the received message in raw byte form and the content type
        """
        Validation.callable_check(func, arg_count=2)
        with self.__callbacks:
            self.__callbacks[_CB_DEBUG_BAD].append(func)

    def register_callback_feeddata(self, func):
        """Register a callback function to every FEEDDATA message! The callback receives a single
        dict argument, with keys of 'data' (decoded or raw bytes), 'mime' (None, unless payload
        was not decoded and has a mime type) and 'pid' (the global id of the feed from which
        the data originates).
        """
        Validation.callable_check(func)
        with self.__callbacks:
            self.__callbacks[_CB_FEEDDATA].append(func)

    def register_callback_controlreq(self, func):
        """Register a callback function to every CONTROLREQ message! The callback receives a single
        dict argument, with keys of 'data' (decoded or raw bytes), 'mime' (None, unless payload
        was not decoded and has a mime type), 'subId' (the global id of the associated subscripion),
        'entityLid' (local id of the thing to which the control belongs), 'lid' (local id of
        control), 'confirm' (whether a confirmation is expected) and 'requestId' (required for
        sending confirmation).
        """
        Validation.callable_check(func)
        with self.__callbacks:
            self.__callbacks[_CB_CONTROLREQ].append(func)

    def simulate_feeddata(self, feedid, data, mime=None):
        """Send feed data"""
        # Separate public method since internal one does not require parameter checks
        feedid = Validation.guid_check_convert(feedid)
        mime = Validation.mime_check_convert(mime, allow_none=True)
        self.__simulate_feeddata(feedid, data, mime)

    # Used by both simulate_feeddata() and internally to propagate feed data
    def __simulate_feeddata(self, feedid, data, mime=None):
        arg = {'data': data,
               'pid': feedid,
               'mime': mime}

        # general catch-all
        have_general = self.__fire_callback(_CB_FEEDDATA, arg)
        # just for this feed
        try:
            callback = self.__callbacks[_CB_FEED][feedid]
        except KeyError:
            if not have_general:
                logger.warning("Received Feed Data for Point GUID '%s' but no callback registered.", feedid)
        else:
            self.__threadpool.submit(callback, arg)

    # Unlike simulate_feeddata this attempts to decode!
    def __handle_controlreq(self, payload, requestId):
        data, mime = self.__bytes_to_share_data(payload)
        arg = payload.copy()
        arg.update({'requestId': requestId,
                    'data': data,
                    'mime': mime})

        # general catch-all
        self.__fire_callback(_CB_CONTROLREQ, arg)
        # just for this control
        try:
            callback = self.__callbacks[_CB_CONTROL][payload[P_ENTITY_LID]][payload[P_LID]]
        except KeyError:
            logger.warning("Recevied Control Request for %s,%s but no callback registered.",
                           payload[P_ENTITY_LID], payload[P_LID])
        else:
            self.__threadpool.submit(callback, arg)

    def is_alive(self):
        return not self.__end.is_set()

    def start(self):  # noqa (complexity)
        """Start the send & recv Threads.
        Start can be delayed to EG restore requestIds before attaching to the QAPI
        Note: This function waits for/blocks until amqplink connect(s) and the current
              sequence number has been obtained from the container (within 5 seconds)
        """
        if not self.__end.is_set():
            return

        self.__end.clear()
        try:
            self.__threadpool.start()
            self.__crud_threadpool.start()
            self.__network_retry_queue = Queue()
            self.__network_retry_thread = Thread(target=self.__network_retry, name='network')
            self.__network_retry_thread.start()
            try:
                self.__amqplink.start()
            except Exception as exc:  # pylint: disable=broad-except
                if not self.__amqplink.is_alive():
                    raise_from(Exception("Core.AmqpLink: Failed to connect"), exc)
                logger.exception("Unhandled startup error")
                raise

            req = self.request_ping()
            if not req.wait(5):
                raise Exception("No container response to ping within 5s")
            # (for req.payload) pylint: disable=unsubscriptable-object
            if req.success:
                payload = req.payload
                self.__qapi_version_check(payload)
                if self.__default_lang is None:
                    self.__default_lang = payload['lang']
                self.__container_params = payload
                try:
                    self.set_compression(payload['compression'])
                except ValueError as ex:
                    raise_from(Exception('Container compression method (%d) unsupported' % payload['compression']), ex)
            else:
                try:
                    info = ': %s' % req.payload[P_MESSAGE]
                except (KeyError, TypeError):
                    info = ''
                raise Exception("Unexpected ping failure: %s" % info)
        except:
            self.stop()
            raise

    @classmethod
    def __qapi_version_check(cls, payload):
        try:
            qapi_version = payload['version']
        except (KeyError, TypeError):
            raise RuntimeError('Unable to perform version check - version not included')

        qapi_str = '.'.join(str(part) for part in qapi_version)
        expected = version_string_to_tuple(cls.__qapi_version)

        if qapi_version[0] != expected[0]:
            raise RuntimeError('QAPI major version difference: %s (%s expected)' %
                               (qapi_str, cls.__qapi_version))
        elif qapi_version[1] < expected[1]:
            raise RuntimeError('QAPI minor version older: %s (%s known)' % (qapi_str, cls.__qapi_version))
        elif qapi_version[1] > expected[1]:
            logger.warning('QAPI minor version difference: %s (%s known)', qapi_str, cls.__qapi_version)
        elif qapi_version[2] > expected[2]:
            logger.info('QAPI patch level change: %s (%s known)', qapi_str, cls.__qapi_version)
        else:
            logger.debug('QAPI version: %s', qapi_str)

    def stop(self):
        """Stop the Client, disconnect from queue
        """
        if self.__end.is_set():
            return
        self.__end.set()
        self.__threadpool.stop()
        self.__crud_threadpool.stop()
        self.__amqplink.stop()
        self.__network_retry_thread.join()
        # Clear out remaining pending requests
        with self.__requests:
            shutdown = LinkShutdownException('Client stopped')
            for req in self.__requests.values():
                req.exception = shutdown
                req._set()
                self.__clear_references(req, remove_request=False)
            logger.info('%d unfinished requests discarded')
            self.__requests.clear()
        #
        self.__network_retry_thread = None
        self.__network_retry_queue = None
        self.__container_params = None

    def set_compression(self, comp=COMP_DEFAULT, size=COMP_SIZE):
        """Override compression method (defined by container) and threshold"""
        if comp not in COMPRESSORS:
            if comp == COMP_LZ4F:
                raise ValueError('lz4f compression not available, required lz4framed')
            else:
                raise ValueError('Invalid compression method')
        if not isinstance(size, int_types) or size < 1:
            raise ValueError('size must be non-negative integer')
        self.__comp_default = comp
        self.__comp_size = size
        return self.__comp_default, self.__comp_size

    def get_seqnum(self):
        return self.__seqnum

    @staticmethod
    def __valid_seqnum(seqnum, prev_seqnum):
        return 0 <= seqnum < _SEQ_WRAP_SIZE and 0 < (seqnum - prev_seqnum) % _SEQ_WRAP_SIZE <= _SEQ_MAX_AHEAD

    #
    # Client request functions
    #
    # Note about naming of function args
    #  requestId = string unique
    #  lid = local name to user
    #  pid = local name to user
    #  foc = R_FEED or R_CONTROL used on point functions which accept both
    #  slid = subscribing lid (local only)
    #  gpid = guid of point EG to subscribe to
    #  sub_id = subscription ID used for ask/tell etc
    #  data = STRING of the data to share,ask,tell etc
    #  limit = int EG 50 (num rows)
    #  offset = int EG 0 (starting position)
    #
    def _request(self, resource, rtype, action=None, payload=None, offset=None, limit=None, requestId=None, is_crud=False):
        """_request amqp queue publish helper

        return: RequestEvent object or None for failed to publish
        """
        if self.__end.is_set():
            raise RuntimeError('Request made whilst client not running')

        rng = None
        if offset is not None and limit is not None:
            Validation.limit_offset_check(limit, offset)
            rng = "%d/%d" % (offset, limit)
        with self.__requests:
            if requestId is None:
                requestId = self.__new_request_id()
            elif requestId in self.__requests:
                raise ValueError('requestId %s already in use' % requestId)
            self.__requests[requestId] = ret = RequestEvent(requestId, is_crud=is_crud)
        #
        inner_msg = self.__make_innermsg(resource, rtype, requestId, action, payload, rng)
        self.__network_retry_queue.put(PreparedMessage(inner_msg, requestId))
        return ret

    def request_ping(self):
        logger.debug("request_ping")
        return self._request(R_PING, C_LIST)

    def request_entity_create(self, lid, epId=None):
        """request entity create: lid = local name to user
        If epId=None (default), the current agent/EP is chosen
        If epId=False, no agent is assigned
        If epId=guid, said agent is chosen
        """
        lid = Validation.lid_check_convert(lid)
        if epId is None:
            epId = self.__epId
        elif epId is False:
            epId = None
        else:
            epId = Validation.guid_check_convert(epId)
        logger.debug("request_entity_create lid='%s'", lid)
        return self._request(R_ENTITY, C_CREATE, None, {'epId': epId, 'lid': lid}, is_crud=True)

    def request_entity_rename(self, lid, newlid):
        lid = Validation.lid_check_convert(lid)
        newlid = Validation.lid_check_convert(newlid)
        logger.debug("request_entity_rename lid='%s' -> newlid='%s'", lid, newlid)
        return self._request(R_ENTITY, C_UPDATE, (lid, 'rename'), {'lid': newlid}, is_crud=True)

    def request_entity_reassign(self, lid, nepId=None):
        """request entity to be reassigned to given ep/agent
        If nepId=None (default), the current agent/EP is chosen
        If nepId=False, no agent is assigned
        If nepId=guid, said agent is chosen
        """
        lid = Validation.lid_check_convert(lid)
        if nepId is None:
            nepId = self.__epId
        elif nepId is False:
            nepId = None
        else:
            nepId = Validation.guid_check_convert(nepId)
        logger.debug("request_entity_reassign lid='%s' -> nepId='%s'", lid, nepId)
        return self._request(R_ENTITY, C_UPDATE, (lid, 'reassign'), {'epId': nepId}, is_crud=True)

    def request_entity_delete(self, lid):
        lid = Validation.lid_check_convert(lid)
        logger.debug("request_entity_delete lid='%s'", lid)
        return self._request(R_ENTITY, C_DELETE, (lid,), is_crud=True)

    def request_entity_list(self, limit=500, offset=0):
        logger.debug("request_entity_list")
        return self._request(R_ENTITY, C_LIST, (self.__epId,), offset=offset, limit=limit)

    def request_entity_list_all(self, limit=500, offset=0):
        logger.debug("request_entity_list_all")
        return self._request(R_ENTITY, C_LIST, offset=offset, limit=limit)

    def request_entity_meta_get(self, lid, fmt="n3"):
        lid = Validation.lid_check_convert(lid)
        fmt = Validation.metafmt_check_convert(fmt)
        logger.debug("request_entity_meta_get lid='%s' fmt='%s'", lid, fmt)
        return self._request(R_ENTITY_META, C_LIST, (lid, fmt))

    def request_entity_meta_set(self, lid, meta, fmt="n3"):
        lid = Validation.lid_check_convert(lid)
        fmt = Validation.metafmt_check_convert(fmt)
        logger.debug("request_entity_meta_set lid='%s' fmt='%s'", lid, fmt)
        return self._request(R_ENTITY_META, C_UPDATE, (lid,), {'meta': meta, 'format': fmt})

    def request_entity_meta_setpublic(self, lid, public=True):
        lid = Validation.lid_check_convert(lid)
        logger.debug("request_entity_meta_setpublic lid='%s' public='%s'", lid, public)
        return self._request(R_ENTITY_META, C_UPDATE, (lid, 'setPublic'), {'public': public})

    def request_entity_tag_create(self, lid, tags, lang=None, delete=False):
        lid = Validation.lid_check_convert(lid)
        tags = Validation.tags_check_convert(tags)
        lang = Validation.lang_check_convert(lang, default=self.__default_lang)
        delete = Validation.bool_check_convert('delete', delete)
        logger.debug("request_entity_tag_create lid='%s' tags=%s lang=%s delete=%s", lid, tags, lang, delete)
        return self._request(R_ENTITY_TAG_META, C_UPDATE, (lid,), {'tags': tags, 'lang': lang, 'delete': delete})

    def request_entity_tag_delete(self, lid, tags, lang=None):
        return self.request_entity_tag_create(lid, tags, lang, True)

    def request_entity_tag_list(self, lid, limit=100, offset=0):
        lid = Validation.lid_check_convert(lid)
        logger.debug("request_entity_tag_list lid='%s'", lid)
        return self._request(R_ENTITY_TAG_META, C_LIST, (lid,), None, offset=offset, limit=limit)

    def request_point_create(self, foc, lid, pid, control_cb=None):
        """request point create: feed or control, lid and pid point lid
        """
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        logger.debug("request_point_create foc=%i lid='%s' pid='%s'", foc, lid, pid)

        if foc == R_CONTROL:
            Validation.callable_check(control_cb)
            evt = self._request(foc, C_CREATE, (lid,), {'lid': pid}, is_crud=True)
            with self.__pending_controls:
                self.__pending_controls[evt.id_] = control_cb
            return evt
        elif control_cb:
            raise ValueError('callback specified for Feed')
        else:
            return self._request(foc, C_CREATE, (lid,), {'lid': pid}, is_crud=True)

    def request_point_rename(self, foc, lid, pid, newpid):
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        newpid = Validation.pid_check_convert(newpid)
        logger.debug("request_point_rename foc=%i lid='%s' pid='%s' -> newpid='%s'", foc, lid, pid, newpid)
        return self._request(foc, C_UPDATE, (lid, pid, 'rename'), {'lid': newpid}, is_crud=True)

    def request_point_confirm_tell(self, foc, lid, pid, success=True, requestId=None):
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        success = Validation.bool_check_convert('success', success)
        logger.debug("request_point_confirm_tell foc=%i lid='%s' pid='%s' success=%s requestId=%s", foc, lid, pid,
                     success, requestId)
        return self._request(foc, C_UPDATE, (lid, pid, 'confirm'), {'success': success}, requestId=requestId)

    def request_point_delete(self, foc, lid, pid):
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        logger.debug("request_point_delete foc=%i lid='%s' pid='%s'", foc, lid, pid)
        return self._request(foc, C_DELETE, (lid, pid), is_crud=True)

    def request_point_list(self, foc, lid, limit=500, offset=0):
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        logger.debug("request_point_list foc=%i lid='%s'", foc, lid)
        return self._request(foc, C_LIST, (lid,), None, offset=offset, limit=limit)

    def request_point_list_detailed(self, foc, lid, pid):
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        logger.debug("request_point_list_detailed foc=%i lid='%s' pid='%s'", foc, lid, pid)
        return self._request(foc, C_LIST, (lid, pid))

    @classmethod
    def __res_to_meta(cls, res):
        if res == R_FEED:
            res = R_FEED_META
        elif res == R_CONTROL:
            res = R_CONTROL_META
        return res

    def request_point_meta_get(self, foc, lid, pid, fmt="n3"):
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        fmt = Validation.metafmt_check_convert(fmt)
        foc = self.__res_to_meta(foc)
        logger.debug("request_point_meta_get foc=%i lid='%s' pid='%s' fmt=%s", foc, lid, pid, fmt)
        return self._request(foc, C_LIST, (lid, pid, fmt))

    def request_point_meta_set(self, foc, lid, pid, meta, fmt="n3"):
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        fmt = Validation.metafmt_check_convert(fmt)
        foc = self.__res_to_meta(foc)
        logger.debug("request_point_meta_set foc=%i lid='%s' pid='%s' fmt=%s", foc, lid, pid, fmt)
        return self._request(foc, C_UPDATE, (lid, pid), {'meta': meta, 'format': fmt})

    def request_point_value_create(self, lid, pid, foc, label, vtype, lang=None, comment=None, unit=None):
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        vtype = Validation.value_type_check_convert(vtype)
        label = Validation.label_check_convert(label)
        lang = Validation.lang_check_convert(lang, default=self.__default_lang)
        comment = Validation.comment_check_convert(comment, allow_none=True)
        logger.debug("request_point_value_create foc=%i lid='%s' pid='%s' label=%s vtype=%s lang=%s", foc, lid, pid,
                     label, vtype, lang)
        return self._request(R_VALUE_META, C_CREATE, (lid, pid, foc),
                             {'label': label, 'type': vtype, 'lang': lang, 'comment': comment, 'unit': unit})

    def request_point_value_delete(self, lid, pid, foc, label, lang=None):
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        label = Validation.label_check_convert(label)
        lang = Validation.lang_check_convert(lang, default=self.__default_lang)
        logger.debug("request_point_value_delete foc=%i lid='%s' pid='%s' label=%s lang=%s", foc, lid, pid, label, lang)
        return self._request(R_VALUE_META, C_DELETE, (lid, pid, foc, label, lang))

    def request_point_value_list(self, lid, pid, foc, limit=500, offset=0):
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        logger.debug("request_point_value_list foc=%i lid='%s' pid='%s'", foc, lid, pid)
        return self._request(R_VALUE_META, C_LIST, (lid, pid, foc), offset=offset, limit=limit)

    @staticmethod
    def __point_type_to_tag_type(foc):
        if foc == R_FEED:
            return R_FEED_TAG_META
        elif foc == R_CONTROL:
            return R_CONTROL_TAG_META
        else:
            raise ValueError('Unknown point type %s' % foc)

    def request_point_tag_create(self, foc, lid, pid, tags, lang=None, delete=False):
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        tags = Validation.tags_check_convert(tags)
        lang = Validation.lang_check_convert(lang, default=self.__default_lang)
        delete = Validation.bool_check_convert('delete', delete)
        logger.debug("request_point_tag_create foc=%i lid='%s' pid='%s' tags=%s lang=%s delete=%s", foc, lid, pid, tags,
                     lang, delete)
        return self._request(self.__point_type_to_tag_type(foc),
                             C_UPDATE,
                             (lid, pid),
                             {'tags': tags, 'lang': lang, 'delete': delete})

    def request_point_tag_delete(self, foc, lid, pid, tags, lang=None):
        return self.request_point_tag_create(foc, lid, pid, tags, lang, True)

    def request_point_tag_list(self, foc, lid, pid, limit=500, offset=0):
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        logger.debug("request_point_tag_list foc=%i lid='%s' pid='%s'", foc, lid, pid)
        return self._request(self.__point_type_to_tag_type(foc),
                             C_LIST,
                             (lid, pid),
                             None,
                             offset=offset, limit=limit)

    def request_sub_create(self, lid, foc, gpid, callback=None):
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        Validation.guid_check_convert(gpid)
        if foc == R_FEED:
            Validation.callable_check(callback, allow_none=True)
        elif callback is not None:
            raise ValueError('Subscription for control cannot have callback')
        logger.debug("request_sub_create foc=%i lid='%s' gpid=%s", foc, lid, gpid)
        evt = self._request(R_SUB, C_CREATE, (lid, gpid), is_crud=True)
        if callback:
            with self.__pending_subs:
                self.__pending_subs[evt.id_] = callback
        return evt

    def request_sub_create_local(self, slid, foc, lid, pid, callback=None):
        slid = Validation.lid_check_convert(slid)
        Validation.foc_check(foc)
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        if foc == R_FEED:
            Validation.callable_check(callback, allow_none=True)
        elif callback is not None:
            raise ValueError('Subscription for control cannot have callback')
        logger.debug("request_sub_create_local slid=%s foc=%i lid='%s' pid='%s'", slid, foc, lid, pid)
        evt = self._request(R_SUB, C_CREATE, (slid, lid, pid, foc), is_crud=True)
        if callback:
            with self.__pending_subs:
                self.__pending_subs[evt.id_] = callback
        return evt

    def __point_data_to_bytes(self, data, mime=None):  # pylint: disable=too-many-branches
        """Returns tuple of mime type & data. Auto encodes unicode strings (to utf8) and
           dictionaries (to ubjson) depending on client setting."""
        if mime is None:
            if self.__auto_encode_decode:
                if isinstance(data, bytes):
                    return None, data
                elif isinstance(data, dict):
                    # check top level dictionary keys
                    if all(isinstance(key, unicode_type) for key in data):
                        return 'idx/1', ubjdumpb(data)  # application/ubjson
                    else:
                        raise ValueError('At least one key in dict not real (unicode) string')
                elif isinstance(data, unicode_type):
                    return 'idx/2', data.encode('utf8')  # text/plain; charset=utf8
                else:
                    raise ValueError('cannot auto-encode data of type %s' % type(data))
            elif isinstance(data, bytes):
                return None, data
            else:
                raise ValueError('No mime type specified and not bytes object (auto-encode disabled)')
        elif valid_mimetype(mime):
            if isinstance(data, bytes):
                return mime, data
            else:
                raise ValueError('mime specified but data not bytes object')
        else:
            raise ValueError('invalid mime type %s' % mime)

    def __bytes_to_share_data(self, payload):
        """Attempt to auto-decode data"""
        rbytes = payload[P_DATA]
        mime = payload[P_MIME]

        if mime is None or not self.__auto_encode_decode:
            return rbytes, mime
        mime = expand_idx_mimetype(mime).lower()
        try:
            if mime == 'application/ubjson':
                return ubjloadb(rbytes), None
            elif mime == 'text/plain; charset=utf8':
                return rbytes.decode('utf-8'), None
            else:
                return rbytes, mime
        except:
            logger.warning('auto-decode failed, returning bytes', exc_info=DEBUG_ENABLED)
            return rbytes, mime

    def request_point_share(self, lid, pid, data, mime=None):
        logger.debug("request_point_share lid='%s' pid='%s'", lid, pid)
        lid = Validation.lid_check_convert(lid)
        pid = Validation.pid_check_convert(pid)
        mime = Validation.mime_check_convert(mime, allow_none=True)
        mime, data = self.__point_data_to_bytes(data, mime)
        return self._request(R_FEED, C_UPDATE, (lid, pid, 'share'), {'mime': mime, 'data': data})

    def request_sub_ask(self, sub_id, data, mime=None):
        logger.debug("request_sub_ask sub_id=%s", sub_id)
        Validation.guid_check_convert(sub_id)
        mime = Validation.mime_check_convert(mime, allow_none=True)
        mime, data = self.__point_data_to_bytes(data, mime)
        return self._request(R_SUB, C_UPDATE, (sub_id, 'ask'), {'mime': mime, 'data': data})

    def request_sub_tell(self, sub_id, data, timeout, mime=None):
        logger.debug("request_sub_tell sub_id=%s timeout=%s", sub_id, timeout)
        Validation.guid_check_convert(sub_id)
        mime = Validation.mime_check_convert(mime, allow_none=True)
        mime, data = self.__point_data_to_bytes(data, mime)
        return self._request(R_SUB, C_UPDATE, (sub_id, 'tell'), {'mime': mime, 'data': data, 'timeout': timeout})

    def request_sub_delete(self, sub_id):
        logger.debug("request_sub_delete sub_id=%s", sub_id)
        return self._request(R_SUB, C_DELETE, (Validation.guid_check_convert(sub_id),), is_crud=True)

    def request_sub_list(self, lid, limit=500, offset=0):
        logger.debug("request_sub_list lid=%s", lid)
        lid = Validation.lid_check_convert(lid)
        return self._request(R_SUB, C_LIST, (lid,), offset=offset, limit=limit)

    def request_search(self, text=None, lang=None, location=None, unit=None, limit=100, offset=0, reduced=False):
        logger.debug("request_search text=%s lang=%s location=%s unit=%s limit=%s offset=%s reduced=%s",
                     text, lang, location, unit, limit, offset, reduced)
        action = ("reduced",) if Validation.bool_check_convert('reduced', reduced) else None
        return self._request(R_SEARCH, C_UPDATE, action,
                             Validation.search_check_convert(text, lang, location, unit,
                                                             default_lang=self.__default_lang),
                             offset=offset, limit=limit)

    def request_describe(self, guid):
        logger.debug("request_describe guid=%s", guid)
        guid = Validation.guid_check_convert(guid)
        return self._request(R_DESCRIBE, C_LIST, (guid,))

    @classmethod
    def __rnd_string(cls, l):  # pylint: disable=invalid-name
        return ''.join(random.choice(string.ascii_uppercase + string.digits + string.ascii_lowercase) for _ in range(l))

    def __new_request_id(self):
        """requestId follows form "pre num" where pre is some random ascii prefix EG 6 chars long
        and num is self.__seqnum. MUST be called within self.__requests lock
        """
        while True:
            # Since seqnum wraps on 2^64 at most, this should always fit into 32 chars (QAPI request id limit)
            with self.__seqnum_lock:
                requestId = "%s%d" % (self.__reqpre, self.__seqnum)
            if requestId not in self.__requests:
                break
            # in the unlikely event of a collision update prefix
            self.__reqpre = self.__rnd_string(6)
        return requestId

    # used by __make_hash
    __byte_packer = Struct('>Q').pack

    @classmethod
    def __make_hash(cls, innermsg, token, seqnum):
        """return the hash for this innermsg, token, seqnum
        return digest bytes
        """
        hobj = hmacNew(token, digestmod=hashfunc)
        hobj.update(innermsg)
        hobj.update(cls.__byte_packer(seqnum))
        return hobj.digest()

    def __check_hash(self, message):
        """return true/false if hash is good
        message = dict
        """
        return message[W_HASH] == self.__make_hash(message[W_MESSAGE], self.__token, message[W_SEQ])

    def __make_innermsg(self, resource, rtype, ref, action=None, payload=None, limit=None):
        """return innermsg chunk (dict)
        """
        if action is not None and not isinstance(action, (tuple, list)):
            raise TypeError('action must be None/tuple/list')
        p = {M_RESOURCE: resource,
             M_TYPE: int(rtype),
             M_CLIENTREF: ref,
             # Ensure action path consists only of strings
             M_ACTION: tuple(u(element) for element in action) if action else None,
             M_PAYLOAD: payload}
        if limit is not None:  # Note: fmtted like "0/15" where 0 = offset, 15 = limit
            p[M_RANGE] = limit
        with self.__lowseq_resend_stash:
            if self.__lowseq_resend and ref not in self.__lowseq_resend_stash:
                self.__lowseq_resend_stash[ref] = p
        return p

    def __request_except(self, requestId, exc, set_and_forget=True):
        """Set exception (if not None) for the given request and (optionally) remove from internal cache & setting its
           event"""
        try:
            with self.__requests:
                if set_and_forget:
                    req = self.__requests.pop(requestId)
                else:
                    req = self.__requests[requestId]
        except KeyError:
            logger.error('Unknown request %s - cannot set exception', requestId)
        else:
            if exc is not None:
                req.exception = exc
            if set_and_forget:
                req._set()

    def __publish(self, qmsg):
        """Returns True unless sending failed (at which point an exception will have been set in the request)"""
        with self.__seqnum_lock:
            seqnum = self.__seqnum
            self.__seqnum = (self.__seqnum + 1) % _SEQ_WRAP_SIZE
        #
        innermsg = ubjdumpb(qmsg.inner_msg)
        clevel = COMP_NONE
        if len(innermsg) >= self.__comp_size:
            logger.debug('Compressing payload')
            try:
                innermsg = COMPRESSORS[self.__comp_default].compress(innermsg)
            except KeyError:
                logger.warning('Unknown compression method %s, not compressing', self.__comp_default)
            else:
                clevel = self.__comp_default

        p = {W_SEQ: seqnum,
             W_MESSAGE: innermsg,
             W_HASH: self.__make_hash(innermsg, self.__token, seqnum),
             W_COMPRESSION: clevel}
        msg = ubjdumpb(p)

        # do not send messages exceeding size limit
        if len(msg) > VALIDATION_MAX_ENCODED_LENGTH:
            self.__request_except(qmsg.requestId, ValueError("Message Payload too large %d > %d"
                                                             % (len(msg), VALIDATION_MAX_ENCODED_LENGTH)))
            return False

        self.__amqplink.send(msg, content_type='application/ubjson')
        if DEBUG_ENABLED:
            p[W_MESSAGE] = qmsg.inner_msg
            logger.debug(decode_sent_msg('decode_sent_msg', p))
        # Callback any debuggers
        self.__fire_callback(_CB_DEBUG_SEND, msg)
        #
        return True

    def __network_retry(self):  # noqa (complexity)
        queue_get = self.__network_retry_queue.get
        queue_task_done = self.__network_retry_queue.task_done
        retry_timeout = self.__network_retry_timeout
        end_is_set = self.__end.is_set
        qmsg = None

        while not end_is_set():
            # retrieve next message
            if qmsg is None:
                try:
                    qmsg = queue_get(timeout=0.2)
                except Empty:
                    continue
            else:
                # wait before retrying previously failed request
                if retry_timeout:
                    sleep(0.5)

            if retry_timeout and qmsg.time < (monotonic() - retry_timeout):
                logger.warning("requestId '%s' timeout after %i", qmsg.requestId, retry_timeout)
                # note: previously set exception is preserved
                self.__request_except(qmsg.requestId, None)
            else:
                try:
                    published = self.__publish(qmsg)
                except LinkException as exc:
                    logger.debug("FAILED TO SEND requestId '%s'", qmsg.requestId)
                    if retry_timeout:
                        self.__request_except(qmsg.requestId, exc, set_and_forget=False)
                        # request will be retried (assuming timeout is not reached after delay)
                        continue
                    else:
                        self.__request_except(qmsg.requestId, exc)
                else:
                    # if not published, an exception will have been set on the request already
                    if published:
                        logger.debug("Sent requestId '%s'", qmsg.requestId)
                        with self.__requests:
                            self.__requests[qmsg.requestId].exception = None
            queue_task_done()
            qmsg = None

    def __fire_callback(self, type_, *args, **kwargs):
        """Returns True if at least one callback was called"""
        called = False
        with self.__callbacks:
            submit = self.__crud_threadpool.submit if type_ in _CB_CRUD_TYPES else self.__threadpool.submit
            for func in self.__callbacks[type_]:
                called = True
                submit(func, *args, **kwargs)
        return called

    def __dispatch_ka(self):
        logger.debug('Keep alive from container')
        self.__fire_callback(_CB_DEBUG_KA)

    __msg_wrapper_keys = frozenset((W_SEQ, W_COMPRESSION, W_MESSAGE, W_HASH))

    @classmethod
    def __valid_msg_wrapper(cls, wrapper):
        return (isinstance(wrapper, dict) and
                # Python v2 doesn't support views, so cast to set
                set(wrapper.keys()) == cls.__msg_wrapper_keys and
                isinstance(wrapper[W_SEQ], int_types) and
                isinstance(wrapper[W_MESSAGE], bytes) and
                isinstance(wrapper[W_COMPRESSION], int_types) and
                isinstance(wrapper[W_HASH], bytes))

    __msg_body_types = (OrderedDict, dict)
    __msg_body_keys = frozenset((M_CLIENTREF, M_TYPE, M_PAYLOAD))
    __msg_body_payload_types = (OrderedDict, dict, type(None))
    __msg_body_clientref_types = (unicode_type, type(None))

    @classmethod
    def __valid_msg_body(cls, body):
        return (isinstance(body, cls.__msg_body_types) and
                # Python v2 doesn't support views, so cast to set
                set(body.keys()) == cls.__msg_body_keys and
                isinstance(body[M_CLIENTREF], cls.__msg_body_clientref_types) and
                isinstance(body[M_TYPE], int_types) and
                isinstance(body[M_PAYLOAD], cls.__msg_body_payload_types))

    def __validate_decode_msg(self, message):  # noqa (complexity) pylint: disable=too-many-return-statements,too-many-branches
        """Decodes wrapper, check hash & seq, decodes body. Returns body or None, if validation / unpack failed"""
        try:
            if not _CONTENT_TYPE_PATTERN.match(message.content_type):
                logger.debug('Message with unexpected content type %s from container, ignoring', message.content_type)
                return
        except AttributeError:
            logger.debug('Message without content type from container, ignoring')
            return False

        # Decode & check message wrapper
        try:
            body = ubjloadb(message.body)
        except:
            logger.warning('Failed to decode message wrapper, ignoring', exc_info=DEBUG_ENABLED)
            return
        if not self.__valid_msg_wrapper(body):
            logger.warning('Invalid message wrapper, ignoring')
            return

        # currently only warn although maybe this should be an error
        if self.__conn_seqnum != -1 and not self.__valid_seqnum(body[W_SEQ], self.__conn_seqnum):
            logger.warning('Unexpected seqnum from container: %d (last seen: %d)', body[W_SEQ],
                           self.__conn_seqnum)
        self.__conn_seqnum = body[W_SEQ]

        # Check message hash
        if not self.__check_hash(body):
            logger.warning('Message has invalid hash, ignoring')
            return

        # Decompress inner message
        try:
            msg = COMPRESSORS[body[W_COMPRESSION]].decompress(body[W_MESSAGE])
        except KeyError:
            logger.warning('Received message with unknown compression: %s', body[W_COMPRESSION])
            return
        except OversizeException as ex:
            logger.warning('Uncompressed message exceeds %d bytes, ignoring', ex.size, exc_info=DEBUG_ENABLED)
            return
        except:
            logger.warning('Decompression failed, ignoring message', exc_info=DEBUG_ENABLED)
            return

        # Decode inner message
        try:
            msg = ubjloadb(msg, object_pairs_hook=OrderedDict)
        except:
            logger.warning('Failed to decode message, ignoring', exc_info=DEBUG_ENABLED)
            return

        if self.__valid_msg_body(msg):
            return (msg, body[W_SEQ])
        else:
            logger.warning('Message with invalid body, ignoring: %s', msg)
            return None

    def __dispatch_msg(self, message):
        """Verify the signature and update RequestEvents / perform callbacks

        Note messages with an invalid wrapper, invalid hash, invalid sequence number or unexpected clientRef
        will be sent to debug_bad callback.
        """
        msg = self.__validate_decode_msg(message)
        if msg:
            msg, seqnum = msg
        else:
            self.__fire_callback(_CB_DEBUG_BAD, message.body, message.content_type)
            return

        if DEBUG_ENABLED:
            logger.debug(decode_rcvd_msg('decode_rcvd_msg', msg, seqnum))
        self.__fire_callback(_CB_DEBUG_RCVD, msg)

        # no reference, or set by client (not container)
        if msg[M_TYPE] not in _RSP_CONTAINER_REF:
            # solicitied
            if msg[M_CLIENTREF]:
                if not self.__handle_known_solicited(msg):
                    logger.warning('Ignoring response for unknown request %s of type %s', msg[M_CLIENTREF], msg[M_TYPE])
            # unsolicitied
            else:
                self.__perform_unsolicited_callbacks(msg)

        # unsolicited but can have reference set by container
        elif msg[M_TYPE] == E_CONTROLREQ:
            self.__handle_controlreq(msg[M_PAYLOAD], msg[M_CLIENTREF])

        else:
            logger.error('Unhandled unsolicited message of type %s', msg[M_TYPE])

    def __handle_known_solicited(self, msg):
        """returns True if message has been handled as a solicited response"""
        with self.__requests:
            try:
                req = self.__requests[msg[M_CLIENTREF]]
            except KeyError:
                return False

            if self.__handle_low_seq_resend(msg):
                return True

            perform_cb = finish = False
            if msg[M_TYPE] not in _RSP_NO_REF:
                self.__update_existing(msg, req)
                # Finalise request if applicable (not marked as finished here so can perform callback first below)
                if msg[M_TYPE] in _RSP_TYPE_FINISH:
                    finish = True
                    # Exception - DUPLICATED also should produce callback
                    perform_cb = (msg[M_TYPE] == E_DUPLICATED)
                elif msg[M_TYPE] != E_PROGRESS:
                    perform_cb = True
            else:
                logger.warning('Reference unexpected for request %s of type %s', msg[M_CLIENTREF],
                               msg[M_TYPE])

        # outside lock to avoid deadlock if callbacks try to perform request-related functions
        if perform_cb:
            self.__perform_unsolicited_callbacks(msg)

        # mark request as finished
        if finish:
            req.success = msg[M_TYPE] in _RSP_TYPE_SUCCESS
            req.payload = msg[M_PAYLOAD]
            self.__clear_references(req)
            # Serialise completion of CRUD requests (together with CREATED, DELETED, etc. messages)
            if req.is_crud:
                self.__crud_threadpool.submit(req._set)
            else:
                req._set()

        return True

    def __clear_references(self, request, remove_request=True):
        """Remove any internal references to the given request"""
        # remove request itself
        if remove_request:
            with self.__requests:
                self.__requests.pop(request.id_)
        # remove request type specific references
        if not request.success:
            with self.__pending_subs:
                self.__pending_subs.pop(request.id_, None)
            with self.__pending_controls:
                self.__pending_controls.pop(request.id_, None)

    def __update_existing(self, msg, req):
        """Propagate changes based on type of message. MUST be called within self.__requests lock. Performs additional
           actions when solicited messages arrive."""
        req._messages.append(msg)

        payload = msg[M_PAYLOAD]
        if msg[M_TYPE] in _RSP_TYPE_CREATION:
            if payload[P_RESOURCE] == R_SUB:
                # Add callback for feeddata
                with self.__pending_subs:
                    if msg[M_CLIENTREF] in self.__pending_subs:
                        callback = self.__pending_subs.pop(msg[M_CLIENTREF])
                        if payload[P_POINT_TYPE] == R_FEED:
                            self.__callbacks[_CB_FEED][payload[P_POINT_ID]] = callback
                        else:
                            logger.warning('Subscription intended to feed is actually control: %s', payload[P_POINT_ID])

            elif payload[P_RESOURCE] == R_CONTROL:
                with self.__pending_controls:
                    if msg[M_CLIENTREF] in self.__pending_controls:
                        # callbacks by thing
                        entity_point_callbacks = self.__callbacks[_CB_CONTROL].setdefault(payload[P_ENTITY_LID], {})
                        # callback by thing and point
                        entity_point_callbacks[payload[P_LID]] = self.__pending_controls.pop(msg[M_CLIENTREF])

    def __handle_low_seq_resend(self, msg):
        """special error case - low sequence number (update sequence number & resend if applicable). Returns True if
           a resend was scheduled, False otherwise"""
        if msg[M_TYPE] == E_FAILED and msg[M_PAYLOAD][P_CODE] == E_FAILED_CODE_LOWSEQNUM:
            with self.__seqnum_lock:
                self.__seqnum = int(msg[M_PAYLOAD][P_MESSAGE])
            if self.__lowseq_resend:
                try:
                    inner_msg = self.__lowseq_resend_stash[msg[M_CLIENTREF]]
                except KeyError:
                    pass
                else:
                    self.__network_retry_queue.put(PreparedMessage(inner_msg, msg[M_CLIENTREF]))
                    return True
        elif self.__lowseq_resend:
            # since a request might receive multiple responses, don't assume to exist
            self.__lowseq_resend_stash.pop(msg[M_CLIENTREF], None)

        return False

    def __perform_unsolicited_callbacks(self, msg):
        """Callbacks for which a client reference is either optional or does not apply at all"""
        type_ = msg[M_TYPE]

        # callbacks for responses which might be unsolicited (e.g. created or deleted)
        if type_ in _RSP_PAYLOAD_CB_MAPPING:
            self.__fire_callback(_RSP_PAYLOAD_CB_MAPPING[type_], msg)

        # Perform callbacks for feed data
        elif type_ == E_FEEDDATA:
            # decoded first since simulate_feeddata expects decoded content
            data, mime = self.__bytes_to_share_data(msg[M_PAYLOAD])
            self.simulate_feeddata(msg[M_PAYLOAD][P_FEED_ID], data, mime)

        # Perform callbacks for unsolicited subscriber message
        elif type_ == E_SUBSCRIBED:
            self.__fire_callback(_CB_SUBSCRIPTION, msg[M_PAYLOAD])

        else:
            logger.error('Unexpected message type for unsolicited callback %s', type_)
