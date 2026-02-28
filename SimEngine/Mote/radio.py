"""
Called by TSCH, links with propagation model.

Also accounts for charge consumed.
"""
from __future__ import absolute_import

# =========================== imports =========================================

import bisect
from builtins import object

# Mote sub-modules
from SimEngine.SimEngineDefines import MILLISECOND, SECOND
from . import MoteDefines as d

# Simulator-wide modules
import SimEngine

# =========================== defines =========================================



# =========================== helpers =========================================

# =========================== body ============================================

class Radio(object):

    def __init__(self, mote):

        # store params
        self.mote                           = mote

        # singletons (quicker access, instead of recreating every time)
        # self.engine                         = SimEngine.SimEngine.SimEngine()
        self.engine                         = SimEngine.MultiNetworkEngine.MultiNetworkSimEngineInstance()
        self.settings                       = SimEngine.SimSettings.SimSettings()
        self.log                            = SimEngine.SimLog.SimLog().log

        # local variables
        self.AckWait                        = MILLISECOND
        self.data_rate                      = 250000                        # 250kbps
        self.bit_duration                   = int(SECOND / self.data_rate)  # time unit
        self.byte_duration                  = self.bit_duration * 8
        self.capture_threshold              = 6                             # db
        self.capture_duration               = 40 * self.bit_duration        # Preamble(4 bytes) + SFD (1 bytes)
        self.onGoingTransmission            = None                          # ongoing transmission (used by propagate)
        self.txPower                        = 0                             # dBm
        self.antennaGain                    = 0                             # dBi
        self.noisepower                     = -105                          # dBm
        self.state                          = d.RADIO_STATE_OFF
        self.channel                        = None
        self.stats = {
            u'last_updated'  : 0,
            u'idle_listen'   : 0,
            u'tx_data_rx_ack': 0,
            u'tx_data'       : 0,
            u'rx_data_tx_ack': 0,
            u'rx_data'       : 0,
            u'sleep'         : 0,
        }
        self.log_stats_interval_asn = int(
            float(self.settings.radio_stats_log_period_s * SECOND) /
            self.settings.tsch_slotDuration
        )
        if self.log_stats_interval_asn > 0:
            self._schedule_log_stats()

    # ======================= public ==========================================

    # TX

    def startTx(self, channel, packet, start_time=None):

        assert self.onGoingTransmission is None
        assert u'type' in packet
        assert u'mac'  in packet

        if start_time is None:
            start_time = self.engine.global_time + self.mote.tsch.clock.get_drift()

        assert start_time >= self.engine.global_time

        # record the state of the radio
        self.state   = d.RADIO_STATE_TX
        self.channel = channel
        # record ongoing, for propagation model
        self.onGoingTransmission = {
            u'channel': channel,
            u'packet':  packet,
            u'start_time': start_time,
            u'end_time':   start_time + self.capture_duration + self.byte_duration * packet[u'pkt_len']
        }

    def txDone(self):
        """end of tx slot"""
        self.state = d.RADIO_STATE_OFF

        channel = self.channel
        self.channel = None

        assert self.onGoingTransmission

        onGoingBroadcast = (self.onGoingTransmission[u'packet'][u'mac'][u'dstMac']==d.BROADCAST_ADDRESS)

        # log charge consumed
        if self.mote.tsch.getIsSync():
            if onGoingBroadcast:
                # no ACK expected (link-layer bcast)
                self._update_stats(u'tx_data')
            else:
                # ACK expected; radio needs to be in RX mode
                self._update_stats(u'tx_data_rx_ack')

        # nothing ongoing anymore
        self.onGoingTransmission = None
        # inform upper layer (TSCH)
        self.mote.tsch.txDone(channel)


    def startRx(self, channel, end_time, start_time=None):
        assert channel in d.TSCH_HOPPING_SEQUENCE
        assert self.state != d.RADIO_STATE_LISTENING, (self.mote.id, self.state)

        self.channel = channel
        # start time should be earlier than transmission
        if start_time is None:
            start_time = self.engine.global_time

        assert end_time > start_time
        assert end_time > self.engine.global_time

        reception = {
            # listening channel
            u'channel': self.channel,
            # mote id
            u'mote': self.mote,
            # time at which the packet starts receving
            u'rx_time': start_time,
            u'end_time': end_time,
            # the transmission which it lock on
            u'locked_transmission': None,
            u'deleted': False
        }
        if self.mote.radio.channel not in self.engine.connectivity.reception_queue:
            self.engine.connectivity.reception_queue[self.mote.radio.channel] = []

        bisect.insort(self.engine.connectivity.reception_queue[self.mote.radio.channel], reception, key=lambda x:x[u'rx_time'])
        self.mote.radio.state = d.RADIO_STATE_LISTENING

    def rxDone(self, packet):
        """end of RX radio activity"""
        
        # switch radio state, and the code for state switching should be first because rxDone may send ACK, which would change its radio state
        self.state   = d.RADIO_STATE_OFF

        # restore channel first because tsch.rxDone may send ACK and reset the channel
        channel = self.channel
        # reset the channel
        self.channel = None

        # log charge consumed
        if not packet:
            # didn't receive any frame (idle listen)
            self._update_stats(u'idle_listen')
        elif (
                self.mote.tsch.getIsSync()
                and
                packet[u'mac'][u'dstMac'] == self.mote.get_mac_addr()
            ):
            # unicast frame for me, I sent an ACK only when I'm
            # synchronized with the network
            self._update_stats(u'rx_data_tx_ack')
        else:
            # either not for me, or broadcast. In any case, I didn't send an ACK
            self._update_stats(u'rx_data')

        # inform upper layer (TSCH)
        self.mote.tsch.rxDone(packet, channel)
        

    def _update_stats(self, stats_type):
        self.stats[u'sleep'] += (
            self.engine.getAsn() - self.stats[u'last_updated'] - 1
        )
        self.stats[stats_type] += 1
        self.stats[u'last_updated'] = self.engine.getAsn()

    def _schedule_log_stats(self):
        next_log_asn = self.engine.getAsn() + self.log_stats_interval_asn
        self.engine.scheduleAtAsn(
            asn = next_log_asn,
            cb = self._log_stats,
            uniqueTag = (self.mote.id, u'log_radio_stats'),
            intraSlotOrder = d.INTRASLOTORDER_ADMINTASKS,
        )

    def _log_stats(self):
        self.log(
            SimEngine.SimLog.LOG_RADIO_STATS,
            {
                u'_mote_id'      : self.mote.id,
                u'idle_listen'   : self.stats[u'idle_listen'],
                u'tx_data_rx_ack': self.stats[u'tx_data_rx_ack'],
                u'tx_data'       : self.stats[u'tx_data'],
                u'rx_data_tx_ack': self.stats[u'rx_data_tx_ack'],
                u'rx_data'       : self.stats[u'rx_data'],
                u'sleep'         : self.stats[u'sleep']
            }
        )

        # schedule next
        self._schedule_log_stats()
