from __future__ import print_function
from collections import defaultdict
import time
import socket
import logging
import pprint
import struct
from threading import Thread, Condition
from llrp_proto import LLRPROSpec, LLRPError, Message_struct, \
         Message_Type2Name, Capability_Name2Type, \
         llrp_data2xml, LLRPMessageDict
import copy
from util import *
from twisted.internet import reactor, task
from twisted.internet.protocol import ClientFactory
from twisted.protocols.basic import LineReceiver
from twisted.internet.error import ReactorAlreadyRunning

LLRP_PORT = 5084

logger = logging.getLogger('sllurp')

class LLRPMessage:
    hdr_fmt = '!HI'
    hdr_len = struct.calcsize(hdr_fmt) # == 6 bytes
    full_hdr_fmt = hdr_fmt + 'I'
    full_hdr_len = struct.calcsize(full_hdr_fmt) # == 10 bytes
    msgdict = None
    msgbytes = None

    def __init__ (self, msgdict=None, msgbytes=None):
        if not (msgdict or msgbytes):
            raise LLRPError('Provide either a message dict or a sequence' \
                    ' of bytes.')
        if msgdict:
            self.msgdict = LLRPMessageDict(msgdict)
            if not msgbytes:
                self.serialize()
        if msgbytes:
            self.msgbytes = msgbytes
            if not msgdict:
                self.deserialize()
        self.peername = None

    def serialize (self):
        if self.msgdict is None:
            raise LLRPError('No message dict to serialize.')
        name = self.msgdict.keys()[0]
        logger.debug('serializing {} command'.format(name))
        ver = self.msgdict[name]['Ver'] & BITMASK(3)
        msgtype = self.msgdict[name]['Type'] & BITMASK(10)
        msgid = self.msgdict[name]['ID']
        try:
            encoder = Message_struct[name]['encode']
        except KeyError:
            raise LLRPError('Cannot find encoder for message type '
                    '{}'.format(name))
        data = encoder(self.msgdict[name])
        self.msgbytes = struct.pack(self.full_hdr_fmt,
                (ver << 10) | msgtype,
                len(data) + self.full_hdr_len,
                msgid) + data
        logger.debug('serialized bytes: {}'.format(hexlify(self.msgbytes)))
        logger.debug('done serializing {} command'.format(name))

    def deserialize (self):
        """Turns a sequence of bytes into a message dictionary."""
        if self.msgbytes is None:
            raise LLRPError('No message bytes to deserialize.')
        data = ''.join(self.msgbytes)
        msgtype, length, msgid = struct.unpack(self.full_hdr_fmt,
                data[:self.full_hdr_len])
        ver = (msgtype >> 10) & BITMASK(3)
        msgtype = msgtype & BITMASK(10)
        try:
            name = Message_Type2Name[msgtype]
            logger.debug('deserializing {} command'.format(name))
            decoder = Message_struct[name]['decode']
        except KeyError:
            raise LLRPError('Cannot find decoder for message type '
                    '{}'.format(msgtype))
        body = data[self.full_hdr_len:length]
        try:
            self.msgdict = {
               name: dict(decoder(body))
            }
            self.msgdict[name]['Ver'] = ver
            self.msgdict[name]['Type'] = msgtype
            self.msgdict[name]['ID'] = msgid
            logger.debug('done deserializing {} command'.format(name))
        except LLRPError as e:
            logger.warning('Problem with {} message format: {}'.format(name, e))
            return ''
        return ''

    def getName (self):
        if not self.msgdict:
            return None
        return self.msgdict.keys()[0]

    def __repr__ (self):
        try:
            ret = llrp_data2xml(self.msgdict)
        except TypeError as te:
            logger.exception(te)
            ret = ''
        return ret

class LLRPClient (LineReceiver):
    STATE_DISCONNECTED = 1
    STATE_CONNECTING = 2
    STATE_CONNECTED = 3
    STATE_SENT_ADD_ROSPEC = 4
    STATE_SENT_ENABLE_ROSPEC = 5
    STATE_INVENTORYING = 6
    STATE_SENT_DELETE_ROSPEC = 7
    STATE_SENT_GET_CAPABILITIES = 8

    def __init__ (self, duration=None, report_every_n_tags=None, antennas=(1,),
            tx_power=0, modulation='M4', tari=0, start_inventory=True,
            disconnect_when_done=True):
        self.setRawMode()
        self.state = LLRPClient.STATE_DISCONNECTED
        self.rospec = None
        self.report_every_n_tags = report_every_n_tags
        self.tx_power = tx_power
        self.modulation = modulation
        self.tari = tari
        self.antennas = antennas
        self.duration = duration
        self.start_inventory = start_inventory
        self.disconnect_when_done = disconnect_when_done
        self.peername = None
        self.tx_power_table = []

        # for partial data transfers
        self.expectingRemainingBytes = 0
        self.partialData = ''

        # state-change callbacks: STATE_* -> [list of callables]
        self.__state_callbacks = {}
        for _, st_num in LLRPClient.getStates():
            self.__state_callbacks[st_num] = []

        # message callbacks (including tag reports):
        # msg_name -> [list of callables]
        self.__message_callbacks = defaultdict(list)

    def addStateCallback (self, state, cb):
        self.__state_callbacks[state].append(cb)

    def addMessageCallback (self, msg_type, cb):
        self.__message_callbacks[msg_type].append(cb)

    def connectionMade(self):
        logger.debug('socket connected')
        self.transport.setTcpKeepAlive(True)
        self.peername = self.transport.getHandle().getpeername()

    @classmethod
    def getStates (_):
        state_names = [st for st in dir(LLRPClient) if st.startswith('STATE_')]
        for state_name in state_names:
            state_num = getattr(LLRPClient, state_name)
            yield state_name, state_num

    @classmethod
    def getStateName (_, state):
        try:
            return [st_name for st_name, st_num in LLRPClient.getStates() \
                    if st_num == state][0]
        except IndexError:
            raise LLRPError('unknown state {}'.format(state))

    def setState (self, newstate, triggering_msg=None, run_callbacks=True):
        logger.debug('state change: {} -> {}'.format(\
                    LLRPClient.getStateName(self.state),
                    LLRPClient.getStateName(newstate)))

        self.state = newstate
        if run_callbacks:
            for fn in self.__state_callbacks[newstate]:
                fn(triggering_msg)

    def connectionLost(self, reason):
        logger.debug('reader disconnected: {}'.format(reason))
        self.peername = None
        self.setState(LLRPClient.STATE_DISCONNECTED)
        try:
            reactor.callFromThread(reactor.stop)
        except ReactorNotRunning:
            pass

    def parseCapabilities (self, capdict):
        def find_p (p, arr):
            m = p(arr)
            for idx, val in enumerate(arr):
                if val == m: return idx
        # check requested antenna set
        gdc = capdict['GeneralDeviceCapabilities']
        if max(self.antennas) > gdc['MaxNumberOfAntennaSupported']:
            reqd  = ','.join(map(str, self.antennas))
            avail = ','.join(map(str,
                        range(1, gdc['MaxNumberOfAntennaSupported']+1)))
            raise LLRPError('Invalid antenna set specified: requested={},' \
                    ' available={}'.format(reqd, avail))

        # check requested Tx power
        logger.info('requested tx_power: {}'.format(self.tx_power))
        bandtbl = capdict['RegulatoryCapabilities']['UHFBandCapabilities']
        bandtbl = {k: v for k, v in bandtbl.items() \
            if k.startswith('TransmitPowerLevelTableEntry')}
        self.tx_power_table = [0,] * (len(bandtbl) + 1)
        for k, v in bandtbl.items():
            idx = v['Index']
            self.tx_power_table[idx] = v['TransmitPowerValue']
        if self.tx_power == 0:
            # tx_power = 0 means max power
            self.tx_power = find_p(max, self.tx_power_table)
        elif self.tx_power > len(self.tx_power_table):
            raise LLRPError('Invalid tx_power: requested={},' \
                    ' available={}'.format(self.tx_power,
                        find_p(max, self.tx_power_table)))
        logger.info('set tx_power: {} ({} dBm)'.format(self.tx_power,
                    self.tx_power_table[self.tx_power] / 100.0))

    def handleMessage (self, lmsg):
        """Implements the LLRP client state machine."""
        logger.debug('LLRPMessage received: {}'.format(lmsg))
        msgName = lmsg.getName()
        lmsg.peername = self.peername

        if msgName == 'RO_ACCESS_REPORT' and \
                    self.state != LLRPClient.STATE_INVENTORYING:
            logger.debug('ignoring RO_ACCESS_REPORT because not inventorying')
            return

        newstate = None
        run_callbacks = True
        bail = False

        #######
        # LLRP client state machine follows.  Beware: gets thorny.  Note the
        # order of the LLRPClient.STATE_* fields.
        #######

        # in DISCONNECTED, CONNECTING, and CONNECTED states, expect only
        # READER_EVENT_NOTIFICATION messages.
        if self.state in (LLRPClient.STATE_DISCONNECTED,
                LLRPClient.STATE_CONNECTING):
            if msgName == 'READER_EVENT_NOTIFICATION':
                d = lmsg.msgdict['READER_EVENT_NOTIFICATION']\
                        ['ReaderEventNotificationData']
                # figure out whether the connection was successful
                try:
                    status = d['ConnectionAttemptEvent']['Status']
                    if status == 'Success':
                        cn2t = Capability_Name2Type
                        reqd = cn2t['All']
                        self.sendLLRPMessage(LLRPMessage(msgdict={
                            'GET_READER_CAPABILITIES': {
                                'Ver':  1,
                                'Type': 1,
                                'ID':   0,
                                'RequestedData': reqd
                            }}))
                        newstate = LLRPClient.STATE_SENT_GET_CAPABILITIES
                    else:
                        logger.fatal('Could not start session on reader: ' \
                                '{}'.format(status))
                        bail = True
                        self.transport.loseConnection()
                except KeyError:
                    pass
            else:
                logger.error('unexpected message {} while' \
                        ' connecting'.format(msgName))
                bail = True
                run_callbacks = False

        # in state SENT_GET_CAPABILITIES, expect only GET_CAPABILITIES_RESPONSE;
        # respond to this message by starting inventory if we're directed to do
        # so and advancing to state CONNECTED.
        elif self.state == LLRPClient.STATE_SENT_GET_CAPABILITIES:
            if msgName == 'GET_READER_CAPABILITIES_RESPONSE':
                d = lmsg.msgdict['GET_READER_CAPABILITIES_RESPONSE']
                logger.debug('Capabilities: {}'.format(pprint.pformat(d)))
                try:
                    self.parseCapabilities(d)
                except LLRPError as err:
                    logger.fatal('Capabilities mismatch: {}'.format(err))
                    bail = True
                    run_callbacks = False
                newstate = LLRPClient.STATE_CONNECTED
                if self.start_inventory:
                    self.startInventory()
            else:
                logger.error('unexpected response {} ' \
                        ' when getting capabilities'.format(msgName))
                bail = True
                run_callbacks = False

        # in state SENT_ADD_ROSPEC, expect only ADD_ROSPEC_RESPONSE; respond to
        # favorable ADD_ROSPEC_RESPONSE by enabling the added ROSpec and
        # advancing to state SENT_ENABLE_ROSPEC.
        elif self.state == LLRPClient.STATE_SENT_ADD_ROSPEC:
            if msgName == 'ADD_ROSPEC_RESPONSE':
                d = lmsg.msgdict['ADD_ROSPEC_RESPONSE']
                if d['LLRPStatus']['StatusCode'] == 'Success':
                    self.sendLLRPMessage(LLRPMessage(msgdict={
                        'ENABLE_ROSPEC': {
                            'Ver':  1,
                            'Type': 24,
                            'ID':   0,
                            'ROSpecID': self.roSpecId
                        }}))
                    newstate = LLRPClient.STATE_SENT_ENABLE_ROSPEC
                else:
                    logger.warn('ADD_ROSPEC failed with status {}: {}' \
                            .format(d['LLRPStatus']['StatusCode'],
                                d['LLRPStatus']['ErrorDescription']))
                    run_callbacks = False
                    self.stopPolitely()
            else:
                logger.error('unexpected response {} ' \
                        ' when adding ROSpec'.format(msgName))
                bail = True
                run_callbacks = False

        # in state SENT_ENABLE_ROSPEC, expect only ENABLE_ROSPEC_RESPONSE;
        # respond to favorable ENABLE_ROSPEC_RESPONSE by starting the enabled
        # ROSpec and advancing to state INVENTORYING.
        elif self.state == LLRPClient.STATE_SENT_ENABLE_ROSPEC:
            if msgName == 'ENABLE_ROSPEC_RESPONSE':
                d = lmsg.msgdict['ENABLE_ROSPEC_RESPONSE']
                if d['LLRPStatus']['StatusCode'] == 'Success':
                    logger.info('starting inventory')
                    newstate = LLRPClient.STATE_INVENTORYING
                    if self.duration:
                        reactor.callFromThread(reactor.callLater, self.duration,
                                self.stopPolitely)
                else:
                    logger.warn('ENABLE_ROSPEC failed with status {}: {}' \
                            .format(d['LLRPStatus']['StatusCode'],
                                d['LLRPStatus']['ErrorDescription']))
                    run_callbacks = False
                    self.stopPolitely()
            else:
                logger.error('unexpected response {} ' \
                        ' when enabling ROSpec'.format(msgName))
                bail = True
                run_callbacks = False

        elif self.state == LLRPClient.STATE_INVENTORYING:
            if msgName not in ('RO_ACCESS_REPORT', 'READER_EVENT_NOTIFICATION'):
                logger.error('unexpected message {} while' \
                        ' inventorying'.format(msgName))
                bail = True
                run_callbacks = False

        elif self.state == LLRPClient.STATE_SENT_DELETE_ROSPEC:
            if msgName == 'DELETE_ROSPEC_RESPONSE':
                d = lmsg.msgdict['DELETE_ROSPEC_RESPONSE']
                if d['LLRPStatus']['StatusCode'] == 'Success':
                    logger.info('reader finished inventory')
                    newstate = LLRPClient.STATE_DISCONNECTED
                    if self.disconnect_when_done:
                        self.transport.loseConnection()
                else:
                    logger.warn('DELETE_ROSPEC failed with status {}: {}' \
                            .format(d['LLRPStatus']['StatusCode'],
                                d['LLRPStatus']['ErrorDescription']))
                    run_callbacks = False
                    logger.info('disconnecting')

                    # no use trying to stop politely if DELETE_ROSPEC has
                    # already failed...
                    self.transport.loseConnection()
            else:
                logger.error('unexpected response {} ' \
                        ' when deleting ROSpec'.format(msgName))
                bail = True
                run_callbacks = False

        # call state-change callbacks
        if newstate:
            self.setState(newstate, lmsg, run_callbacks=run_callbacks)

        # call other callbacks
        if run_callbacks:
            for fn in self.__message_callbacks[msgName]:
                fn(lmsg)

        if bail:
            reactor.stop()

    def rawDataReceived (self, data):
        logger.debug('got {} bytes from reader: {}'.format(len(data),
                    data.encode('hex')))

        if self.expectingRemainingBytes:
            if len(data) >= self.expectingRemainingBytes:
                data = self.partialData + data
                self.partialData = ''
                self.expectingRemainingBytes -= len(data)
            else:
                # still not enough; wait until next time
                self.partialData += data
                self.expectingRemainingBytes -= len(data)
                return

        while data:
            # parse the message header to grab its length
            msg_type, msg_len, message_id = \
                struct.unpack(LLRPMessage.full_hdr_fmt,
                              data[:LLRPMessage.full_hdr_len])
            logger.debug('expect {} bytes (have {})'.format(msg_len, len(data)))

            if len(data) < msg_len:
                # got too few bytes
                self.partialData = data
                self.expectingRemainingBytes = msg_len - len(data)
                break
            else:
                # got at least the right number of bytes
                self.expectingRemainingBytes = 0
                try:
                    lmsg = LLRPMessage(msgbytes=data[:msg_len])
                    self.handleMessage(lmsg)
                    data = data[msg_len:]
                except LLRPError as err:
                    logger.warn('Failed to decode LLRPMessage: {}.  ' \
                            'Will not decode {} remaining bytes'.format(err,
                                len(data)))
                    break

    def startInventory (self):
        if not self.rospec:
            self.create_rospec()
        r = self.rospec['ROSpec']
        self.roSpecId = r['ROSpecID']

        # add an ROspec
        self.sendLLRPMessage(LLRPMessage(msgdict={
            'ADD_ROSPEC': {
                'Ver':  1,
                'Type': 20,
                'ID':   0,
                'ROSpecID': self.roSpecId,
                'ROSpec': r,
            }}))
        self.setState(LLRPClient.STATE_SENT_ADD_ROSPEC)

    def create_rospec (self):
        if self.rospec:
            return
        # create an ROSpec, which defines the reader's inventorying
        # behavior, and start running it on the reader
        self.rospec = LLRPROSpec(1, duration_sec=self.duration,
                report_every_n_tags=self.report_every_n_tags,
                tx_power=self.tx_power, modulation=self.modulation,
                tari=self.tari, antennas=self.antennas)
        logger.debug('ROSpec: {}'.format(self.rospec))

    def stopPolitely (self, force=False):
        """Delete all active ROSpecs."""
        logger.info('stopping politely')
        if self.state != LLRPClient.STATE_CONNECTED:
            if force:
                logger.warn('stop impolitely from state ' \
                            '{}'.format(LLRPClient.getStateName(self.state)))
            else:
                logger.warn('cannot stop politely from state ' \
                            '{}; ignoring'.format(LLRPClient.getStateName(self.state)))
                return
        self.sendLLRPMessage(LLRPMessage(msgdict={
            'DELETE_ROSPEC': {
                'Ver':  1,
                'Type': 21,
                'ID':   0,
                'ROSpecID': 0
            }}))
        self.setState(LLRPClient.STATE_SENT_DELETE_ROSPEC)

    def pause (self, duration_seconds):
        """Pause an inventory operation for a set amount of time."""
        if self.state != LLRPClient.STATE_INVENTORYING:
            logger.debug('cannot pause() if not inventorying; ignoring')
            return
        logger.info('pausing for {} seconds'.format(duration_seconds))
        self.stopPolitely()
        d = task.deferLater(reactor, duration_seconds, reactor.callFromThread,
                self.startInventory)

    def sendLLRPMessage (self, llrp_msg):
        reactor.callFromThread(self.sendMessage, llrp_msg.msgbytes)

    def sendMessage (self, msg):
        self.transport.write(msg)

class LLRPClientFactory (ClientFactory):
    def __init__ (self, parent, reconnect=False, **kwargs):
        self.parent = parent
        self.client_args = kwargs
        self.reconnect = reconnect
        self.reconnect_delay = 1.0 # seconds

        # callbacks to pass to connected clients
        # (map of LLRPClient.STATE_* -> [list of callbacks])
        self.__state_callbacks = {}
        for _, st_num in LLRPClient.getStates():
            self.__state_callbacks[st_num] = []

        # message callbacks to pass to connected clients
        self.__message_callbacks = defaultdict(list)

    def startedConnecting(self, connector):
        logger.info('connecting...')

    def addStateCallback (self, state, cb):
        assert state in self.__state_callbacks
        self.__state_callbacks[state].append(cb)

    def addTagReportCallback (self, cb):
        self.__message_callbacks['RO_ACCESS_REPORT'].append(cb)

    def buildProtocol(self, addr):
        proto = LLRPClient(**self.client_args)

        # register state-change callbacks with new client
        for state, cbs in self.__state_callbacks.items():
            for cb in cbs:
                proto.addStateCallback(state, cb)

        # register message callbacks with new client
        for msg_type, cbs in self.__message_callbacks.items():
            for cb in cbs:
                proto.addMessageCallback(msg_type, cb)

        # register new client with parent
        try:
            self.parent.protocols.append(proto)
        except AttributeError:
            self.parent.protocol = proto

        return proto

    def clientConnectionLost(self, connector, reason):
        logger.info('lost connection: {}'.format(reason))
        ClientFactory.clientConnectionLost(self, connector, reason)
        if self.reconnect:
            reactor.callFromThread(time.sleep, self.reconnect_delay)
            connector.connect()

    def clientConnectionFailed(self, connector, reason):
        logger.info('connection failed: {}'.format(reason))
        ClientFactory.clientConnectionFailed(self, connector, reason)
        if self.reconnect:
            reactor.callFromThread(time.sleep, self.reconnect_delay)
            connector.connect()
        else:
            try:
                reactor.callFromThread(reactor.stop)
            except ReactorNotRunning:
                pass

class ProtocolWrapper (object):
    """Serves as parent object for LLRPClientFactory."""
    def __init__ (self):
        self.protocols = []
    def addProtocol (self, p):
        self.protocols.append(p)
    def getProtocols (self):
        for p in self.protocols:
            yield p
