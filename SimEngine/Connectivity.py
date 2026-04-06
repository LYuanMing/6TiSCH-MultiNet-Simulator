#!/usr/bin/python
"""
Creates a connectivity matrix and provide methods to get the connectivity
between two motes.

The connectivity matrix is indexed by source id, destination id and channel.
Each cell of the matrix is a dict with the fields `pdr` and `rssi`

The connectivity matrix can be filled statically at startup or be updated along
time if a connectivity trace is given.

The propagate() method is called at every slot. It loops through the
transmissions occurring during that slot and checks if the transmission fails or
succeeds.
"""
from __future__ import print_function
from __future__ import absolute_import
from __future__ import division

# =========================== imports =========================================

from builtins import zip
from builtins import str
from builtins import object
import numpy as np
from past.utils import old_div
import copy
import sys
import random
import math
import gzip
import datetime as dt
import json
import itertools
from scipy.optimize import curve_fit


from . import SimSettings
from . import SimLog
from .Mote.Mote import Mote
from .Mote import MoteDefines as d

# =========================== defines =========================================

CONN_TYPE_TRACE = "trace"

# =========================== helpers =========================================

# =========================== classes =========================================


class Connectivity(object):
    # ===== start singleton
    _instance = None
    _init = False

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(Connectivity, cls).__new__(cls)
        return cls._instance

    # ===== end singleton

    def __init__(self, sim_engine=None):

        # ==== start singleton
        cls = type(self)
        if cls._init:
            return
        cls._init = True
        # ==== end singleton

        # store params

        # singletons (quicker access, instead of recreating every time)
        assert sim_engine
        self.settings = SimSettings.SimSettings()
        self.engine = sim_engine

        # instantiate a connectivity matrix (this may update settings.tsch_slotDuration)
        conn_class_name = self.settings.conn_class
        matrix_class_name = "ConnectivityMatrix{0}".format(conn_class_name)
        matrix_class = getattr(sys.modules[__name__], matrix_class_name)
        self.matrix = matrix_class(self)
        # get log handle after matrix initialization to ensure updated settings are logged
        self.log = SimLog.SimLog().log

        if hasattr(self.settings, 'phy_numChans') and self.settings.phy_numChans is not None:
            # short-hands and local variables
            self.num_channels = self.settings.phy_numChans

        # schedule propagation task
        self._schedule_propagate()

    def destroy(self):
        cls = type(self)
        cls._instance = None
        cls._init = False

    def get_pdr(self, src_id, dst_id, channel):
        assert isinstance(src_id, int)
        assert isinstance(dst_id, int)
        assert channel in d.TSCH_HOPPING_SEQUENCE

        return self.matrix.get_pdr(src_id, dst_id, channel)

    def get_rssi(self, src_id, dst_id, channel):
        assert isinstance(src_id, int)
        assert isinstance(dst_id, int)
        assert channel in d.TSCH_HOPPING_SEQUENCE

        return self.matrix.get_rssi(src_id, dst_id, channel)

    def propagate(self):
        """Simulate the propagation of frames in a slot."""

        # local shorthands
        asn = self.engine.getAsn()
        slotOffset = asn % self.settings.tsch_slotframeLength

        # get all motes TXing or RXing on this slot organized by channel
        transmissions_by_channel = {}
        receivers_by_channel = {}

        # organize all transmissions and receptions by channel
        for mote in self.engine.motes:
            # get all transmissions
            if mote.radio.state == d.RADIO_STATE_TX:
                assert mote.radio.onGoingTransmission
                thisTran = {
                    # channel
                    "channel": mote.radio.onGoingTransmission["channel"],
                    # packet
                    "tx_mote_id": mote.id,
                    "packet": mote.radio.onGoingTransmission["packet"],
                    # time at which the packet starts transmitting
                    "txTime": mote.tsch.clock.get_drift(),
                    # number of ACKs received by this packet
                    "numACKs": 0,
                }

                if thisTran["channel"] not in transmissions_by_channel:
                    transmissions_by_channel[thisTran["channel"]] = []

                transmissions_by_channel[thisTran["channel"]] += [thisTran]

            # get all receivers
            elif mote.radio.state == d.RADIO_STATE_RX:
                if mote.radio.channel not in receivers_by_channel:
                    receivers_by_channel[mote.radio.channel] = []

                receivers_by_channel[mote.radio.channel] += [mote.id]

            else:
                # mote is idle, do nothing
                pass

        # remove all motes that are listening to channels without any transmission
        for channel in set(receivers_by_channel.keys()) - set(
            transmissions_by_channel.keys()
        ):
            assert channel not in transmissions_by_channel
            assert channel in d.TSCH_HOPPING_SEQUENCE[: self.num_channels], (channel, d.TSCH_HOPPING_SEQUENCE[: self.num_channels])

            for listener_id in receivers_by_channel[channel]:
                sentAck = self.engine.motes[listener_id].radio.rxDone(
                    packet=None,
                )
                assert sentAck is False

        # remove all transmissions that are sent on channels without any listeners
        for channel in set(transmissions_by_channel.keys()) - set(
            receivers_by_channel.keys()
        ):
            assert channel not in receivers_by_channel
            assert channel in d.TSCH_HOPPING_SEQUENCE[: self.num_channels]

            for t in transmissions_by_channel[channel]:
                self.engine.motes[t["tx_mote_id"]].radio.txDone(False)

        # prosses packets sent on channels with listeners
        for channel in set(transmissions_by_channel.keys()) & set(
            receivers_by_channel.keys()
        ):
            assert channel in d.TSCH_HOPPING_SEQUENCE[: self.num_channels]

            for listener_id in receivers_by_channel[channel]:
                # list the transmissions that listener can hear and lock to the earliest one
                lockon_transmission = None
                lockon_random_value = None
                interfering_transmissions = []
                detected_transmissions = 0

                # deal with collisions
                if len(transmissions_by_channel[channel]) > 1:
                    for t in transmissions_by_channel[channel]:
                        # random_value will be used for comparison against PDR
                        random_value = random.random()

                        peamble_pdr = self.get_pdr(
                            src_id=t["tx_mote_id"],
                            dst_id=listener_id,
                            channel=channel,
                        )

                        # you can interpret the following line as decision for
                        # reception of the preamble of 't'
                        if random_value > peamble_pdr:
                            # reception failed, continue to the next transmission
                            continue

                        # update counter
                        detected_transmissions += 1

                        # begin locking to the first heard transmission
                        if lockon_transmission is None:
                            lockon_transmission = t
                            lockon_random_value = random_value
                            continue

                        # then update the locked transmission if it's earlier than the previous earliest
                        if t["txTime"] < lockon_transmission["txTime"]:
                            # add previous locked on tranmission to the interference list
                            interfering_transmissions += [t]
                            # and lock to the new earliest transmission
                            lockon_transmission = t
                            lockon_random_value = random_value
                        else:
                            interfering_transmissions += [t]

                    # check if it received anything
                    if lockon_transmission is None:
                        # nope, set the receiver to idle listen and cotinue to next one
                        sentAck = self.engine.motes[listener_id].radio.rxDone(
                            packet=None,
                        )
                        continue

                    # something was received, continue execution
                    self.log(
                        SimLog.LOG_PROP_INTERFERENCE,
                        {
                            "_mote_id": listener_id,
                            "channel": lockon_transmission["channel"],
                            "lockon_transmission": (lockon_transmission["packet"]),
                            "interfering_transmissions": [
                                t["packet"] for t in interfering_transmissions
                            ],
                        },
                    )

                    # calculate the resulting pdr when taking
                    # interferers into account
                    packet_pdr = self._compute_pdr_with_interference(
                        listener_id=listener_id,
                        lockon_transmission=lockon_transmission,
                        interfering_transmissions=interfering_transmissions,
                    )

                # no collision, easy peasy
                elif len(transmissions_by_channel[channel]) == 1:
                    # there's no point in testing the preamble here, so we'll skip it
                    detected_transmissions = 1

                    lockon_random_value = random.random()
                    lockon_transmission = transmissions_by_channel[channel][0]
                    packet_pdr = self.get_pdr(
                        src_id=lockon_transmission["tx_mote_id"],
                        dst_id=listener_id,
                        channel=channel,
                    )

                # this souldn't really happen
                else:
                    assert False

                # lockon transmission selected
                # all other transmissions are now intereferers
                assert detected_transmissions == (len(interfering_transmissions) + 1)

                # decide whether listener receives
                # lockon_transmission or not
                if lockon_random_value < packet_pdr:
                    # listener receives!

                    # lockon_transmission received correctly
                    receivedAck = self.engine.motes[listener_id].radio.rxDone(
                        packet=lockon_transmission["packet"],
                    )

                    if receivedAck and self.settings.conn_simulate_ack_drop:
                        pdr_of_return_link = self.get_pdr(
                            src_id=listener_id,
                            dst_id=lockon_transmission["tx_mote_id"],
                            channel=channel,
                        )
                        receivedAck = random.random() < pdr_of_return_link

                    if receivedAck:
                        # keep track of the number of ACKs received by
                        # that transmission
                        lockon_transmission["numACKs"] += 1
                    else:
                        # ACK is lost in the air
                        pass
                else:
                    # lockon_transmission NOT received correctly
                    # (interference)
                    receivedAck = self.engine.motes[listener_id].radio.rxDone(
                        packet=None,
                    )
                    self.log(
                        SimLog.LOG_PROP_DROP_LOCKON,
                        {
                            "_mote_id": listener_id,
                            "channel": lockon_transmission["channel"],
                            "lockon_transmission": (lockon_transmission["packet"]),
                        },
                    )
                    assert receivedAck is False

                # done processing this listener

            # after processing all listeners send back ACK to transmitter if possible
            for t in transmissions_by_channel[channel]:
                # decide whether transmitter received an ACK
                if t["numACKs"] == 0:
                    isACKed = False
                elif t["numACKs"] == 1:
                    isACKed = True
                else:
                    # we do not expect multiple ACKs (would indicate
                    # duplicate MAC addresses)
                    raise SystemError()

                # indicate to source packet was sent
                self.engine.motes[t["tx_mote_id"]].radio.txDone(isACKed)

        # verify all radios off
        for mote in self.engine.motes:
            assert mote.radio.state == d.RADIO_STATE_OFF
            assert mote.radio.channel is None

        # schedule next propagation
        self._schedule_propagate()

    def _schedule_propagate(self):
        """
        schedule a propagation task in the middle of the next slot.
        FIXME: only schedule for next active slot.
        """
        self.engine.scheduleAtAsn(
            asn=self.engine.getAsn() + 1,
            cb=self.propagate,
            uniqueTag=(None, "Connectivity.propagate"),
            intraSlotOrder=d.INTRASLOTORDER_PROPAGATE,
        )

    def _get_listener_id_list(self, channel):
        returnVal = []
        for mote in self.engine.motes:
            if (mote.radio.state == d.RADIO_STATE_RX) and (
                mote.radio.channel == channel
            ):
                returnVal.append(mote.id)
        return returnVal

    def _compute_pdr_with_interference(
        self, listener_id, lockon_transmission, interfering_transmissions
    ):

        # shorthand
        channel = lockon_transmission["channel"]
        for t in interfering_transmissions:
            assert t["channel"] == channel
        lockon_tx_mote_id = lockon_transmission["tx_mote_id"]

        # === compute the SINR

        noise_mW = self._dBm_to_mW(self.engine.motes[listener_id].radio.noisepower)

        # S = RSSI - N

        signal_mW = self._dBm_to_mW(
            self.get_rssi(lockon_tx_mote_id, listener_id, channel)
        )
        signal_mW -= noise_mW
        if signal_mW < 0.0:
            # RSSI has not to be below the noise level.
            # If this happens, return very low SINR (-10.0dB)
            return -10.0

        # I = RSSI - N

        totalInterference_mW = 0.0
        for interfering_tran in interfering_transmissions:
            interfering_tx_mote_id = interfering_tran["tx_mote_id"]
            interference_mW = self._dBm_to_mW(
                self.get_rssi(interfering_tx_mote_id, listener_id, channel)
            )
            interference_mW -= noise_mW
            if interference_mW < 0.0:
                # RSSI has not to be below noise level.
                # If this happens, set interference to 0.0
                interference_mW = 0.0
            totalInterference_mW += interference_mW

        sinr_dB = self._mW_to_dBm(old_div(signal_mW, (totalInterference_mW + noise_mW)))

        # === compute the interference PDR

        # shorthand
        noise_dBm = self.engine.motes[listener_id].radio.noisepower

        # RSSI of the interfering transmissions
        interference_rssi = self._mW_to_dBm(
            self._dBm_to_mW(sinr_dB + noise_dBm) + self._dBm_to_mW(noise_dBm)
        )

        # PDR of the interfering transmissions
        interference_pdr = self._rssi_to_pdr(interference_rssi)

        # === compute the resulting PDR

        lockon_pdr = self.get_pdr(
            src_id=lockon_tx_mote_id, dst_id=listener_id, channel=channel
        )
        returnVal = lockon_pdr * interference_pdr

        return returnVal

    # === helpers

    @staticmethod
    def _dBm_to_mW(dBm):
        return math.pow(10.0, dBm / 10.0)

    @staticmethod
    def _mW_to_dBm(mW):
        return 10 * math.log10(mW)

    @staticmethod
    def _rssi_to_pdr(rssi):
        """
        rssi and pdr relationship obtained by experiment below
        http://wsn.eecs.berkeley.edu/connectivity/?dataset=dust
        """

        rssi_pdr_table = {
            -97: 0.0000,  # this value is not from experiment
            -96: 0.1494,
            -95: 0.2340,
            -94: 0.4071,
            # <-- 50% PDR is here, at RSSI=-93.6
            -93: 0.6359,
            -92: 0.6866,
            -91: 0.7476,
            -90: 0.8603,
            -89: 0.8702,
            -88: 0.9324,
            -87: 0.9427,
            -86: 0.9562,
            -85: 0.9611,
            -84: 0.9739,
            -83: 0.9745,
            -82: 0.9844,
            -81: 0.9854,
            -80: 0.9903,
            -79: 1.0000,  # this value is not from experiment
        }

        minRssi = min(rssi_pdr_table.keys())
        maxRssi = max(rssi_pdr_table.keys())

        floorRssi = int(math.floor(rssi))
        if floorRssi < minRssi:
            pdr = 0.0
        elif floorRssi >= maxRssi:
            pdr = 1.0
        else:
            pdrLow = rssi_pdr_table[floorRssi]
            pdrHigh = rssi_pdr_table[floorRssi + 1]
            # linear interpolation
            pdr = (pdrHigh - pdrLow) * (rssi - float(floorRssi)) + pdrLow

        assert 0 <= pdr <= 1.0

        return pdr


class ConnectivityMatrixBase(object):
    LINK_PERFECT = {"pdr": 1.00, "rssi": -10}
    LINK_NONE = {"pdr": 0, "rssi": -1000}

    def __init__(self, connectivity):
        # local variables
        self.mote_id_list = [mote.id for mote in connectivity.engine.motes]
        self.engine = connectivity.engine
        self.settings = connectivity.settings
        self._matrix = {}
        self.connectivity = connectivity  # Store reference for access to sparklinkPHY
        self.num_channels = None

        if hasattr(self.settings, 'phy_numChans') and self.settings.phy_numChans is not None:
            # short-hands and local variables
            self.num_channels = self.settings.phy_numChans

        # at the beginning, connectivity matrix indicates no connectivity at all
        for src_id in self.mote_id_list:
            self._matrix[src_id] = {}
            for dst_id in self.mote_id_list:
                self._matrix[src_id][dst_id] = {}
                for channel in d.TSCH_HOPPING_SEQUENCE:
                    self._matrix[src_id][dst_id][channel] = copy.copy(self.LINK_NONE)

        self._additional_initialization()

    def _additional_initialization(self):
        # override this method if you want to do more in __init__(),
        # for instance, to fill the matrix with some values
        pass

    def set_pdr(self, src_id, dst_id, channel, pdr):
        self._matrix[src_id][dst_id][channel]["pdr"] = pdr

    def set_pdr_both_directions(self, mote_id_1, mote_id_2, channel, pdr):
        self._matrix[mote_id_1][mote_id_2][channel]["pdr"] = pdr
        self._matrix[mote_id_2][mote_id_1][channel]["pdr"] = pdr

    def get_pdr(self, src_id, dst_id, channel):
        return self._matrix[src_id][dst_id][channel]["pdr"]

    def set_rssi(self, src_id, dst_id, channel, rssi):
        self._matrix[src_id][dst_id][channel]["rssi"] = rssi

    def set_rssi_both_directions(self, mote_id_1, mote_id_2, channel, rssi):
        self._matrix[mote_id_1][mote_id_2][channel]["rssi"] = rssi
        self._matrix[mote_id_2][mote_id_1][channel]["rssi"] = rssi

    def get_rssi(self, src_id, dst_id, channel):
        return self._matrix[src_id][dst_id][channel]["rssi"]

    def dump(self):
        output = []
        output += ["\n"]

        # header
        line = []
        for src_id in self._matrix:
            line += [str(src_id)]
        line = "\t|".join(line)
        output += ["\t|" + line]

        # body
        channel = d.TSCH_HOPPING_SEQUENCE[0]
        for src_id in self._matrix:
            line = []
            line += [str(src_id)]
            for dst_id in self._matrix[src_id]:
                if src_id == dst_id:
                    line += ["N/A"]
                else:
                    line += [str(self._matrix[src_id][dst_id][channel]["pdr"])]
            line = "\t|".join(line)
            output += [line]

        output = "\n".join(output)
        print(output)


class ConnectivityMatrixFullyMeshed(ConnectivityMatrixBase):
    """
    All nodes can hear all nodes with PDR=100%.
    """

    def _additional_initialization(self):
        assert self.num_channels
        perfect_pdr = self.LINK_PERFECT["pdr"]
        perfect_rssi = self.LINK_PERFECT["rssi"]
        for src_id in self.mote_id_list:
            for dst_id in self.mote_id_list:
                for channel in d.TSCH_HOPPING_SEQUENCE[: self.num_channels]:
                    self.set_pdr(src_id, dst_id, channel, perfect_pdr)
                    self.set_rssi(src_id, dst_id, channel, perfect_rssi)


class ConnectivityMatrixLinear(ConnectivityMatrixBase):
    """
    Perfect linear topology.
           100%     100%     100%       100%
        0 <----> 1 <----> 2 <----> ... <----> num_motes-1
    """

    def _additional_initialization(self):
        assert self.num_channels
        perfect_pdr = self.LINK_PERFECT["pdr"]
        perfect_rssi = self.LINK_PERFECT["rssi"]
        parent_id = None
        for child_id in self.mote_id_list:
            if parent_id is not None:
                for channel in d.TSCH_HOPPING_SEQUENCE[: self.num_channels]:
                    self.set_pdr_both_directions(
                        child_id, parent_id, channel, perfect_pdr
                    )
                    self.set_rssi_both_directions(
                        child_id, parent_id, channel, perfect_rssi
                    )
            parent_id = child_id


class ConnectivityMatrixK7(ConnectivityMatrixBase):
    """
    Replay K7 connectivity trace.
    """

    def _additional_initialization(self):
        """Fill the matrix using the connectivity trace file.  The
        connectivity matrix is initialized with values representing
        the absence of a link.  The connectivity trace file is then
        loaded into memory (connectivity values and trace meta
        information).
        """
        assert self.num_channels
        # additional local variables
        self.trace = []
        self.start_date = None
        # the offset at which we stopped reading the trace
        self.trace_position = 0
        self.asn_of_next_update = 0

        # load trace into memory and save metas (headers)
        with gzip.open(self.settings.conn_trace, "r") as tracefile:
            self.trace_header = json.loads(tracefile.readline().decode("utf-8"))
            self.csv_header = tracefile.readline().decode("utf-8").strip().split(",")
            self.start_date = dt.datetime.strptime(
                self.trace_header["start_date"], "%Y-%m-%dT%H:%M:%S.%f"
            )
            stop_date = dt.datetime.strptime(
                self.trace_header["stop_date"], "%Y-%m-%dT%H:%M:%S.%f"
            )

            # check if the simulation settings match the trace file

            if self.settings.exec_numMotes != self.trace_header["node_count"]:
                print(
                    "Wrong configuration. exec_numMotes is {0}, should be {1}".format(
                        self.settings.exec_numMotes, self.trace_header["node_count"]
                    )
                )
                assert self.settings.exec_numMotes == self.trace_header["node_count"]

            # check if all the channels in the hopping sequence are
            # covered by ones listed in the header
            if set(d.TSCH_HOPPING_SEQUENCE).issubset(
                set(self.trace_header["channels"])
            ):
                # the channels listed in the trace file are valid
                pass
            else:
                raise ValueError(
                    "All the channels in TSCH_HOPPING_SEQUENCE "
                    + "must be covered by the trace file\n"
                    + "TSCH_HOPPING_SEQUENCE: {0}\n".format(
                        sorted(d.TSCH_HOPPING_SEQUENCE)
                    )
                    + "Channels in the trace: {0}\n".format(
                        sorted(self.trace_header["channels"])
                    )
                    + "Check SimEngine/Mote/MoteDefines.py"
                )

            numSlotframes = old_div(
                (stop_date - self.start_date).total_seconds(),
                self.settings.tsch_slotDuration,
            )
            if self.settings.exec_numSlotframesPerRun > numSlotframes:
                raise ValueError("exec_numSlotframesPerRun is too long")

            initialization_is_done = False
            initialized_links = set([])

            for line in tracefile:
                row = self._parse_line(line.decode("utf-8"))
                # make sure that PDR is a float
                row["pdr"] = float(row["pdr"])
                if not initialization_is_done:
                    link = (row["src_id"], row["dst_id"], row["channel"])
                    if link in initialized_links:
                        # we've already initlized this link
                        initialization_is_done = True
                        # we don't need to keep the links any more
                        initialized_links = None
                    else:
                        # this link has not been initialized. for this
                        # purpose, set ASN 0 to this row so that this
                        # row will be used to in the first _update()
                        # call
                        row["asn"] = 0
                        # add the link to the list
                        initialized_links.add(link)
                self.trace.append(row)

            # initialize the matrix with the first part of the trace
            # file
            self._update()

    # ======================= private =========================================

    def _update(self):
        assert self.asn_of_next_update >= self.engine.getAsn()
        # Read the connectivity trace and fill the connectivity
        # matrix
        assert self.trace_position < len(self.trace)
        start_trace_position = self.trace_position
        while True:
            row = self.trace[self.trace_position]

            # return next update ASN

            if row["asn"] > self.engine.asn:
                asn_of_next_update = row["asn"]
                break

            # update matrix value

            self._set_connectivity(row)

            # increment trace_position
            self.trace_position += 1

            if self.trace_position == len(self.trace):
                # we hit the bottom of the trace
                asn_of_next_update = None
                break

        # update 'asn_of_next_update' with a new ASN, which can be
        # None
        self.asn_of_next_update = asn_of_next_update
        self.log(
            SimLog.LOG_CONN_MATRIX_K7_UPDATE,
            {
                "start_trace_position": start_trace_position,
                "end_trace_position": self.trace_position,
                "asn_of_next_update": self.asn_of_next_update,
            },
        )
        if self.asn_of_next_update:
            assert self.engine.getAsn() < self.asn_of_next_update
            self.engine.scheduleAtAsn(
                asn=self.asn_of_next_update,
                cb=self._update,
                uniqueTag=("ConnectivityMatrixK7", "update matrix"),
                intraSlotOrder=d.INTRASLOTORDER_STARTSLOT,
            )

    def _set_connectivity(self, row):
        """Modify the connectivity matrix.  If no channel is given
        (i.e. channel is None), set all channels to the same value.
        """
        for channel in d.TSCH_HOPPING_SEQUENCE[: self.num_channels]:
            if (row["channel"] is None) or (row["channel"] == channel):
                self.set_pdr(row["src_id"], row["dst_id"], channel, row["pdr"])
                self.set_rssi(row["src_id"], row["dst_id"], channel, row["mean_rssi"])

    def _parse_line(self, line):

        # === read and parse line

        vals = line.strip().split(",")
        row = dict(list(zip(self.csv_header, vals)))

        # === change row format

        row["src_id"] = int(row["src"]) if row["src"] else None
        del row["src"]
        row["dst_id"] = int(row["dst"]) if row["dst"] else None
        del row["dst"]
        row["channel"] = int(row["channel"]) if row["channel"] else None
        row["datetime"] = dt.datetime.strptime(row["datetime"], "%Y-%m-%dT%H:%M:%S.%f")

        # rssi

        if row["mean_rssi"] == "" or (row["mean_rssi"] == "None"):
            row["mean_rssi"] = self.LINK_NONE["rssi"]
        else:
            row["mean_rssi"] = float(row["mean_rssi"])

        # === add ASN value to row

        time_delta = row["datetime"] - self.start_date
        row["asn"] = int(
            time_delta.total_seconds() / float(self.settings.tsch_slotDuration)
        )

        return row


class ConnectivityMatrixRandom(ConnectivityMatrixBase):
    """Random (topology) connectivity using the Pister-Hack model

    Note that it doesn't guarantee every motes has always at least as
    many neighbors as 'conn_random_init_min_neighbors', who have good
    PDR values with the mote.

    Computed PDR and RSSI are computed on the fly; they could vary at
    every transmission.
    """

    def _additional_initialization(self):
        assert self.num_channels
        # additional local variables
        self.coordinates = {}  # (x, y) indexed by mote_id
        self.pister_hack = PisterHackModel(self.engine)

        # ConnectivityRandom doesn't need the connectivity matrix. Instead, it
        # initializes coordinates of the motes. Its algorithm is:
        #
        # step.1 if moteid is 0
        #   step.1-1 set (0, 0) to its coordinate
        # step.2 otherwise
        #   step.2-1 set its (tentative) coordinate randomly
        #   step.2-2 count the number of neighbors with sufficient PDR (N)
        #   step.2-3 if the number of deployed motes are smaller than
        #          STABLE_NEIGHBORS
        #     step.2-3-1 if N is equal to the number of deployed motes, fix the
        #                coordinate of the mote
        #     step.2-3-2 otherwise, go back to step.2-1
        #   step.2-4 otherwise,
        #     step.2-4 if N is equal to or larger than STABLE_NEIGHBORS, fix
        #                the coordinate of the mote
        #     step.2-5 otherwise, go back to step.2-1

        # for quick access
        square_side = self.settings.conn_random_square_side
        init_min_pdr = self.settings.conn_random_init_min_pdr
        init_min_neighbors = self.settings.conn_random_init_min_neighbors

        assert init_min_neighbors <= self.settings.exec_numMotes

        # determine coordinates of the motes
        for target_mote_id in self.mote_id_list:
            mote_is_deployed = False
            while mote_is_deployed is False:

                # select a tentative coordinate
                if target_mote_id == 0:
                    self.coordinates[target_mote_id] = (0, 0)
                    mote_is_deployed = True
                    continue

                coordinate = (
                    square_side * random.random(),
                    square_side * random.random(),
                )

                # count deployed motes who have enough PDR values to this
                # mote
                good_pdr_count = 0
                base_channel = d.TSCH_HOPPING_SEQUENCE[0]
                for deployed_mote_id in self.coordinates:
                    rssi = self.pister_hack.compute_rssi(
                        {
                            "mote": self._get_mote(target_mote_id),
                            "coordinate": coordinate,
                        },
                        {
                            "mote": self._get_mote(deployed_mote_id),
                            "coordinate": self.coordinates[deployed_mote_id],
                        },
                    )
                    pdr = self.pister_hack.convert_rssi_to_pdr(rssi)
                    # memorize the rssi and pdr values at the base channel
                    self.set_pdr_both_directions(
                        target_mote_id, deployed_mote_id, base_channel, pdr
                    )
                    self.set_rssi_both_directions(
                        target_mote_id, deployed_mote_id, base_channel, rssi
                    )

                    if init_min_pdr <= pdr:
                        good_pdr_count += 1

                # determine whether we deploy this mote or not
                if (
                    (len(self.coordinates) <= init_min_neighbors)
                    and (len(self.coordinates) == good_pdr_count)
                ) or (
                    (init_min_neighbors < len(self.coordinates))
                    and (init_min_neighbors <= good_pdr_count)
                ):
                    # fix the coordinate of the mote
                    self.coordinates[target_mote_id] = coordinate
                    # copy the rssi and pdr values to other channels
                    for deployed_mote_id in list(self.coordinates.keys()):
                        rssi = self.get_rssi(
                            target_mote_id, deployed_mote_id, base_channel
                        )
                        pdr = self.get_pdr(
                            target_mote_id, deployed_mote_id, base_channel
                        )
                        for channel in d.TSCH_HOPPING_SEQUENCE[: self.num_channels]:
                            if channel == base_channel:
                                # do nothing
                                pass
                            else:
                                self.set_pdr_both_directions(
                                    target_mote_id, deployed_mote_id, channel, pdr
                                )
                                self.set_rssi_both_directions(
                                    target_mote_id, deployed_mote_id, channel, rssi
                                )

                    mote_is_deployed = True
                else:
                    # remove memorized values at channel 0
                    for deployed_mote_id in self.coordinates:
                        self._clear_rssi(target_mote_id, deployed_mote_id, base_channel)
                        self._clear_pdr(target_mote_id, deployed_mote_id, base_channel)
                    # try another random coordinate
                    continue

    def _get_mote(self, mote_id):
        # there must be a mote having mote_id. otherwise, the following line
        # raises an exception.
        return [mote for mote in self.engine.motes if mote.id == mote_id][0]

    def _clear_rssi(self, mote_id_1, mote_id_2, channel):
        self.set_rssi_both_directions(
            mote_id_1, mote_id_2, channel, self.LINK_NONE["rssi"]
        )

    def _clear_pdr(self, mote_id_1, mote_id_2, channel):
        self.set_rssi_both_directions(
            mote_id_1, mote_id_2, channel, self.LINK_NONE["pdr"]
        )


class PisterHackModel(object):

    PISTER_HACK_LOWER_SHIFT = 40  # dB
    TWO_DOT_FOUR_GHZ = 2400000000  # Hz
    SPEED_OF_LIGHT = 299792458  # m/s

    # RSSI and PDR relationship obtained by experiment; dataset was available
    # at the link shown below:
    # http://wsn.eecs.berkeley.edu/connectivity/?dataset=dust
    RSSI_PDR_TABLE = {
        -97: 0.0000,  # this value is not from experiment
        -96: 0.1494,
        -95: 0.2340,
        -94: 0.4071,
        # <-- 50% PDR is here, at RSSI=-93.6
        -93: 0.6359,
        -92: 0.6866,
        -91: 0.7476,
        -90: 0.8603,
        -89: 0.8702,
        -88: 0.9324,
        -87: 0.9427,
        -86: 0.9562,
        -85: 0.9611,
        -84: 0.9739,
        -83: 0.9745,
        -82: 0.9844,
        -81: 0.9854,
        -80: 0.9903,
        -79: 1.0000,  # this value is not from experiment
    }

    def __init__(self, sim_engine):

        # singleton
        self.engine = sim_engine

        # remember what RSSI value is computed for a mote at an ASN; the same
        # RSSI value will be returned for the same motes and the ASN.
        self.rssi_cache = {}  # indexed by (src_mote.id, dst_mote.id)

    def compute_mean_rssi(self, src, dst):
        # distance in meters
        distance = self._get_distance_in_meters(src["coordinate"], dst["coordinate"])

        # sqrt and inverse of the free space path loss (fspl)
        free_space_path_loss = old_div(
            self.SPEED_OF_LIGHT, (4 * math.pi * distance * self.TWO_DOT_FOUR_GHZ)
        )

        # simple friis equation in Pr = Pt + Gt + Gr + 20log10(fspl)
        pr = (
            src["mote"].radio.txPower
            + src["mote"].radio.antennaGain
            + dst["mote"].radio.antennaGain
            + (20 * math.log10(free_space_path_loss))
        )

        # according to the receiver power (RSSI) we can apply the Pister hack
        # model.
        # choosing the "mean" value
        return pr - old_div(self.PISTER_HACK_LOWER_SHIFT, 2)

    def compute_rssi(self, src, dst):
        """Compute RSSI between the points of a and b using Pister Hack"""

        assert sorted(src.keys()) == sorted(["mote", "coordinate"])
        assert sorted(dst.keys()) == sorted(["mote", "coordinate"])

        # compute the mean RSSI (== friis - 20)
        mu = self.compute_mean_rssi(src, dst)

        # the receiver will receive the packet with an rssi uniformly
        # distributed between friis and (friis - 40)
        rssi = mu + random.uniform(
            old_div(-self.PISTER_HACK_LOWER_SHIFT, 2),
            old_div(+self.PISTER_HACK_LOWER_SHIFT, 2),
        )

        return rssi

    def convert_rssi_to_pdr(self, rssi):
        minRssi = min(self.RSSI_PDR_TABLE.keys())
        maxRssi = max(self.RSSI_PDR_TABLE.keys())

        if rssi < minRssi:
            pdr = 0.0
        elif rssi > maxRssi:
            pdr = 1.0
        else:
            floor_rssi = int(math.floor(rssi))
            pdr_low = self.RSSI_PDR_TABLE[floor_rssi]
            pdr_high = self.RSSI_PDR_TABLE[floor_rssi + 1]
            # linear interpolation
            pdr = (pdr_high - pdr_low) * (rssi - float(floor_rssi)) + pdr_low

        assert pdr >= 0.0
        assert pdr <= 1.0
        return pdr

    @staticmethod
    def _get_distance_in_meters(a, b):
        """Compute distance in meters between two points of a and b

        a and b are tuples which are 2D coordinates expressed in
        kilometers.
        """
        return 1000 * math.sqrt(pow((b[0] - a[0]), 2) + pow((b[1] - a[1]), 2))


class ConnectivityMatrixMultiPHY(ConnectivityMatrixBase):
    """Random (topology) connectivity using different physical layer model

    inherit from ConnectivityMatrixRandom

    """

    def _additional_initialization(self):
        # additional local variables
        self.coordinates = {}  # (x, y) indexed by mote_id

        # Use the sparklinkPHY to compute slot duration based on the PHY configuration
        self.sparklinkPHY = SparklinkLowEnergyModel(self.engine, "1M_GFSK")
        self.num_channels = self.sparklinkPHY.get_numChans()
        # ConnectivityRandom doesn't need the connectivity matrix. Instead, it
        # initializes coordinates of the motes. Its algorithm is:
        #
        # step.1 if moteid is 0
        #   step.1-1 set (0, 0) to its coordinate
        # step.2 otherwise
        #   step.2-1 set its (tentative) coordinate randomly
        #   step.2-2 count the number of neighbors with sufficient PDR (N)
        #   step.2-3 if the number of deployed motes are smaller than
        #          STABLE_NEIGHBORS
        #     step.2-3-1 if N is equal to the number of deployed motes, fix the
        #                coordinate of the mote
        #     step.2-3-2 otherwise, go back to step.2-1
        #   step.2-4 otherwise,
        #     step.2-4 if N is equal to or larger than STABLE_NEIGHBORS, fix
        #                the coordinate of the mote
        #     step.2-5 otherwise, go back to step.2-1

        # for quick access
        square_side = self.settings.conn_random_square_side
        init_min_pdr = self.settings.conn_random_init_min_pdr
        init_min_neighbors = self.settings.conn_random_init_min_neighbors

        assert init_min_neighbors <= self.settings.exec_numMotes

        # determine coordinates of the motes
        for target_mote_id in self.mote_id_list:
            mote_is_deployed = False
            while mote_is_deployed is False:

                # select a tentative coordinate
                if target_mote_id == 0:
                    self.coordinates[target_mote_id] = (0, 0)
                    mote_is_deployed = True
                    continue

                coordinate = (
                    square_side * random.random(),
                    square_side * random.random(),
                )

                # count deployed motes who have enough PDR values to this
                # mote using sparklinkPHY model
                good_pdr_count = 0
                base_channel = d.TSCH_HOPPING_SEQUENCE[0]
                for deployed_mote_id in self.coordinates:
                    src, dst = {
                            "mote": self._get_mote(target_mote_id),
                            "coordinate": coordinate,
                        }, {
                            "mote": self._get_mote(deployed_mote_id),
                            "coordinate": self.coordinates[deployed_mote_id],
                        }

                    rssi = self.sparklinkPHY.compute_rssi(src, dst)
                    pdr = self.sparklinkPHY.compute_pdr(src, dst)
                    # memorize the rssi and pdr values at the base channel
                    
                    self.set_pdr_both_directions(
                        target_mote_id, deployed_mote_id, base_channel, pdr
                    )
                    self.set_rssi_both_directions(
                        target_mote_id, deployed_mote_id, base_channel, rssi
                    )

                    if init_min_pdr <= pdr:
                        good_pdr_count += 1

                # determine whether we deploy this mote or not
                if (
                    (len(self.coordinates) <= init_min_neighbors)
                    and (len(self.coordinates) == good_pdr_count)
                ) or (
                    (init_min_neighbors < len(self.coordinates))
                    and (init_min_neighbors <= good_pdr_count)
                ):
                    # fix the coordinate of the mote
                    self.coordinates[target_mote_id] = coordinate
                    # copy the rssi and pdr values to other channels
                    for deployed_mote_id in list(self.coordinates.keys()):
                        rssi = self.get_rssi(
                            target_mote_id, deployed_mote_id, base_channel
                        )
                        pdr = self.get_pdr(
                            target_mote_id, deployed_mote_id, base_channel
                        )
                        for channel in d.TSCH_HOPPING_SEQUENCE[: self.num_channels]:
                            if channel == base_channel:
                                # do nothing
                                pass
                            else:
                                self.set_pdr_both_directions(
                                    target_mote_id, deployed_mote_id, channel, pdr
                                )
                                self.set_rssi_both_directions(
                                    target_mote_id, deployed_mote_id, channel, rssi
                                )

                    mote_is_deployed = True
                else:
                    # remove memorized values at channel 0
                    for deployed_mote_id in self.coordinates:
                        self._clear_rssi(target_mote_id, deployed_mote_id, base_channel)
                        self._clear_pdr(target_mote_id, deployed_mote_id, base_channel)
                    # try another random coordinate
                    continue

    def _get_mote(self, mote_id):
        # there must be a mote having mote_id. otherwise, the following line
        # raises an exception.
        return [mote for mote in self.engine.motes if mote.id == mote_id][0]

    def _clear_rssi(self, mote_id_1, mote_id_2, channel):
        self.set_rssi_both_directions(
            mote_id_1, mote_id_2, channel, self.LINK_NONE["rssi"]
        )

    def _clear_pdr(self, mote_id_1, mote_id_2, channel):
        self.set_rssi_both_directions(
            mote_id_1, mote_id_2, channel, self.LINK_NONE["pdr"]
        )


class SparklinkLowEnergyModel(object):
    """
    SparkLink Low Energy PHY Model

    SparkLink operates in the 2.4 GHz band: 2400 MHz - 2483.5 MHz (83.5 MHz total)

    Supports the following PHY modes:
    +----------+----------+-------------+------------+
    | Mode     | Datarate | Bandwidth   | Channels   |
    +==========+==========+=============+============+
    | 1M GFSK  | 1 Mb/s   | 1 MHz       | 79 (0-78)  |
    | 2M GFSK  | 2 Mb/s   | 2 MHz       | 40 (even)  |
    | 4M GFSK  | 4 Mb/s   | 4 MHz       | 20 (x4)    |
    | 1M QPSK  | 2 Mb/s   | 1 MHz       | 79 (0-78)  |
    | 2M QPSK  | 4 Mb/s   | 2 MHz       | 40 (even)  |
    | 4M QPSK  | 8 Mb/s   | 4 MHz       | 20 (x4)    |
    | 1M 8PSK  | 3 Mb/s   | 1 MHz       | 79 (0-78)  |
    | 2M 8PSK  | 6 Mb/s   | 2 MHz       | 40 (even)  |
    | 4M 8PSK  | 12 Mb/s  | 4 MHz       | 20 (x4)    |
    +----------+----------+-------------+------------+

    Note: Channel allocation pattern:
    - 1 MHz: all 79 channels (0-78)
    - 2 MHz: every 2nd channel (0, 2, 4, ..., 78)
    - 4 MHz: every 4th channel (0, 4, 8, ..., 76)

    When the PHY mode is set, phy_numChans is automatically updated in SimSettings.
    """

    # PHY mode constants
    PHY_1M_GFSK = "1M_GFSK"
    PHY_2M_GFSK = "2M_GFSK"
    PHY_4M_GFSK = "4M_GFSK"
    PHY_1M_QPSK = "1M_QPSK"
    PHY_2M_QPSK = "2M_QPSK"
    PHY_4M_QPSK = "4M_QPSK"
    PHY_1M_8PSK = "1M_8PSK"
    PHY_2M_8PSK = "2M_8PSK"
    PHY_4M_8PSK = "4M_8PSK"

    # SparkLink frequency range: 2400 MHz - 2483.5 MHz (83.5 MHz total bandwidth)
    SPARKLINK_FREQ_MIN = 2400000000  # Hz (2400 MHz)
    SPARKLINK_FREQ_MAX = 2483500000  # Hz (2483.5 MHz)
    SPARKLINK_TOTAL_BW = 83.5  # MHz

    # PHY configuration: {mode: {'datarate': Mbps, 'bandwidth': MHz, 'numChans': int}}
    # Channel allocation:
    # - 1 MHz bandwidth: 79 channels (channel 0-78)
    # - 2 MHz bandwidth: 40 channels (channel 0, 2, 4, ..., 78)
    # - 4 MHz bandwidth: 20 channels (channel 0, 4, 8, ..., 76)
    
    MAX_PAYLOAD_SIZE = 128 # bytes
    
    TsRxOffset = 0.00112 # seconds (1.12 milliseconds)
    TsTxAckDelay = 0.001 # seconds (1 millisecond)
    
    TsTxOffset = 0.00212 # seconds (2.12 milliseconds)
    TsRxAckWait = 0.0004 # seconds (400 microseconds)
    
    GUARD_TIME = 0.0005 # seconds (500 microseconds)
    RADIO_RAMPUP_RAMPDOWN_TIME = 0.0002 # seconds (200 microseconds)

    PHY_CONFIGS = {
        PHY_1M_GFSK: {
            "datarate": 1,
            "bandwidth": "1M",
            "modulation": "GFSK",
            "numChans": 79,
        },
        PHY_2M_GFSK: {
            "datarate": 2,
            "bandwidth": "2M",
            "modulation": "GFSK",
            "numChans": 40,

        },
        PHY_4M_GFSK: {
            "datarate": 4,
            "bandwidth": "4M",
            "modulation": "GFSK",
            "numChans": 20,
        },
        PHY_1M_QPSK: {
            "datarate": 2,
            "bandwidth": "1M",
            "modulation": "QPSK",
            "numChans": 79,
        },
        PHY_2M_QPSK: {
            "datarate": 4,
            "bandwidth": "2M",
            "modulation": "QPSK",
            "numChans": 40,
        },
        PHY_4M_QPSK: {
            "datarate": 8,
            "bandwidth": "4M",
            "modulation": "QPSK",
            "numChans": 20,
        },
        PHY_1M_8PSK: {
            "datarate": 3,
            "bandwidth": "1M",
            "modulation": "8PSK",
            "numChans": 79,
        },
        PHY_2M_8PSK: {
            "datarate": 6,
            "bandwidth": "2M",
            "modulation": "8PSK",
            "numChans": 40,
        },
        PHY_4M_8PSK: {
            "datarate": 12,
            "bandwidth": "4M",
            "modulation": "8PSK",
            "numChans": 20,
        },
    }

    # reception in 1000 packets
    PDR_data = {
        "1M": {
            "GFSK": {
                "distance": [4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0, 36.0, 40.0, 41.7],
                "PDR": [
                    921.6,
                    834.3,
                    876.8,
                    846.0,
                    868.9,
                    762.9,
                    731.8,
                    767.8,
                    508.2,
                    452.0,
                    196.7,
                ],
            },
            "QPSK": {
                "distance": [4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0, 36.0, 40.0, 43.0],
                "PDR": [
                    891.3,
                    892.2,
                    913.9,
                    882.6,
                    888.3,
                    838.5,
                    666.5,
                    576.7,
                    498.8,
                    452.0,
                    80.7,
                ],
            },
            "8PSK": {
                "distance": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 7.5, 8.0, 8.4],
                "PDR": [
                    972.4,
                    930.4,
                    916.4,
                    783.6,
                    797.8,
                    882.7,
                    846.1,
                    679.0,
                    322.0,
                    249.9,
                ],
            },
        },
        "2M": {
            "GFSK": {
                "distance": [4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0, 36.0, 40.0],
                "PDR": [
                    903.5,
                    932.1,
                    927.7,
                    884.9,
                    871.7,
                    873.6,
                    636.4,
                    275.2,
                    400.0,
                    103.8,
                ],
            },
            "QPSK": {
                "distance": [4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0],
                "PDR": [950.0, 917.9, 895.2, 872.8, 771.1, 614.5, 416.1, 128.0],
            },
            "8PSK": {
                "distance": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 6.2, 6.5, 7.0],
                "PDR": [973.0, 957.9, 934.7, 914.6, 753.5, 843.3, 504.8, 169.6, 132.2],
            },
        },
        "4M": {
            "GFSK": {
                "distance": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
                "PDR": [989.5, 979.0, 868.1, 824.6, 769.4, 463.6, 247.1],
            },
            "QPSK": {
                "distance": [1.0, 2.0, 3.0, 4.0, 4.1, 4.15, 4.2],
                "PDR": [914.3, 897.3, 910.6, 921.3, 874.0, 436.6, 196.6],
            },
            "8PSK": {
                "distance": [0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.1, 2.4, 2.58],
                "PDR": [914.6, 866.4, 913.7, 841.1, 682.7, 486.4, 444.3, 130.8, 140.5],
            },
        },
    }
    RSSI_data = {
        "1M": {
            "GFSK": {
                "distance": [4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0, 36.0, 40.0, 41.7],
                "RSSI": [-76, -84, -81, -87, -86, -89, -90, -90, -91, -91, -93],
            },
            "QPSK": {
                "distance": [4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0, 36.0, 40.0, 43.0],
                "RSSI": [-75, -84, -82, -84, -85, -87, -89, -90, -91, -91, -94],
            },
            "8PSK": {
                "distance": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 7.5, 8.0, 8.4],
                "RSSI": [-68, -71, -74, -82, -81, -81, -81, -86, -86, -87],
            },
        },
        "2M": {
            "GFSK": {
                "distance": [4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0, 36.0, 40.0],
                "RSSI": [-76, -84, -82, -81, -84, -87, -88, -89, -90, -91],
            },
            "QPSK": {
                "distance": [4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 28.0, 32.0],
                "RSSI": [-74, -80, -82, -82, -86, -87, -88, -89],
            },
            "8PSK": {
                "distance": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 6.2, 6.5, 7.0],
                "RSSI": [-67, -71, -73, -77, -80, -79, -81, -83, -83],
            },
        },
        "4M": {
            "GFSK": {
                "distance": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
                "RSSI": [-63, -70, -73, -76, -77, -79, -79],
            },
            "QPSK": {
                "distance": [1.0, 2.0, 3.0, 4.0, 4.1, 4.15, 4.2],
                "RSSI": [-72, -73, -76, -76, -77, -78, -78],
            },
            "8PSK": {
                "distance": [0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.1, 2.4, 2.58],
                "RSSI": [-57, -66, -65, -67, -68, -70, -68, -69, -70],
            },
        },
    }

    def __init__(self, sim_engine, phy_mode=None):
        """
        Initialize the SparkLink Low Energy PHY model.

        :param sim_engine: SimEngine instance
        :param phy_mode: PHY mode to use (default: read from settings)
        """
        # singleton
        self.engine = sim_engine
        self.settings = SimSettings.SimSettings()

        # Determine PHY mode
        phy_mode = getattr(self.settings, "conn_phy_mode", self.PHY_1M_GFSK)

        self.set_phy_mode(phy_mode)

        # Validate phy_numChans against current PHY mode's maximum
        self._validate_phy_numChans()

        self.polyfit_PDR()
        self.polyfit_RSSI()

        # compute and update slot duration based on the current PHY configuration
        slot_duration = self.compute_slot_duration()
        self.settings.tsch_slotDuration = slot_duration

    def _update_phy_numChans(self):
        """
        Update phy_numChans in settings based on the current PHY mode.

        The number of channels is inversely proportional to bandwidth.

        """
        numChans = self.phy_config["numChans"]
        current_numChans = getattr(self.settings, "phy_numChans", None)
        if current_numChans is None:
            self.settings.phy_numChans = numChans
        else:
            # 只有当 current_numChans 比 numChans 大时才更新
            self.settings.phy_numChans = min(current_numChans, numChans)

    def _validate_phy_numChans(self):
        """
        Validate that settings.phy_numChans does not exceed the maximum
        allowed for the current PHY mode.

        :raises ValueError: if phy_numChans exceeds the maximum for this PHY mode
        """
        if hasattr(self.settings, 'phy_numChans') and self.settings.phy_numChans is None:
            return

        max_numChans = self.phy_config["numChans"]
        if self.settings.phy_numChans > max_numChans:
            raise ValueError(
                "phy_numChans ({}) exceeds maximum allowed ({}) for PHY mode {}".format(
                    self.settings.phy_numChans, max_numChans, self.phy_mode
                )
            )

    def set_phy_mode(self, phy_mode):
        """
        Change the PHY mode.

        :param phy_mode: new PHY mode to use
        """
        if phy_mode not in self.PHY_CONFIGS:
            raise ValueError(
                "Invalid PHY mode: {}. Valid modes: {}".format(
                    phy_mode, list(self.PHY_CONFIGS.keys())
                )
            )
        self.phy_mode = phy_mode
        self.phy_config = self.PHY_CONFIGS[phy_mode]

        # Automatically update phy_numChans if enabled
        self._update_phy_numChans()

    def get_datarate(self):
        """Get current PHY datarate in Mbps."""
        return self.phy_config["datarate"]

    def get_bandwidth(self):
        """Get current PHY bandwidth in MHz."""
        return self.phy_config["bandwidth"]

    def get_modulation(self):
        """Get current modulation scheme."""
        return self.phy_config["modulation"]

    def get_numChans(self):
        """Get the number of channels for the current PHY mode."""
        return self.phy_config["numChans"]

    # 1. 定义 PDR 物理拟合函数 (Sigmoid)
    @staticmethod
    def pdr_func(x, k, x0):
        return 1 / (1 + np.exp(k * (x - x0)))

    # 2. 定义 RSSI 物理拟合函数 (Logarithmic)
    @staticmethod
    def rssi_func(x, n, A):
        # 使用 x+1 避免 0 距离点的对数问题，A 为参考距离的强度
        return A - 10 * n * np.log10(x + 1)


    def polyfit_PDR(self):
        bandwidth = self.get_bandwidth()
        modulation = self.get_modulation()
        if bandwidth in self.PDR_data and modulation in self.PDR_data[bandwidth]:
            distance_data = self.PDR_data[bandwidth][modulation]["distance"]
            pdr_data = self.PDR_data[bandwidth][modulation]["PDR"]
            x = np.array(distance_data, dtype=float)
            y = np.array(pdr_data, dtype=float)
            
            # 确保包含 (0, 1)
            if 0.0 not in x:
                x = np.insert(x, 0, 0.0); y = np.insert(y, 0, 1.0)
            
            self.PDR_model_parameters, _ = curve_fit(self.pdr_func, x, y, p0=[0.5, np.median(x)])
        assert self.PDR_model_parameters is not None, "PDR curve fitting failed for bandwidth {} and modulation {}".format(bandwidth, modulation)

    def polyfit_RSSI(self):
        bandwidth = self.get_bandwidth()
        modulation = self.get_modulation()
        if bandwidth in self.RSSI_data and modulation in self.RSSI_data[bandwidth]:
            distance_data = self.RSSI_data[bandwidth][modulation]["distance"]
            rssi_data = self.RSSI_data[bandwidth][modulation]["RSSI"]
            x = np.array(distance_data, dtype=float)
            y = np.array(rssi_data, dtype=float)

            # 确保包含 (0, A)
            if 0.0 not in x:
                x = np.insert(x, 0, 0.0); y = np.insert(y, 0, -40.0)

            self.RSSI_model_parameters, _ = curve_fit(self.rssi_func, x, y, p0=[2.0, -40.0])
        assert self.RSSI_model_parameters is not None, "RSSI curve fitting failed for bandwidth {} and modulation {}".format(bandwidth, modulation)

    def compute_rssi(self, src, dst):
        assert sorted(src.keys()) == sorted(["mote", "coordinate"])
        assert sorted(dst.keys()) == sorted(["mote", "coordinate"])

        # 计算距离
        distance = self._get_distance_in_meters(src["coordinate"], dst["coordinate"])

        # 使用拟合的 RSSI 模型计算 RSSI
        n, A = self.RSSI_model_parameters
        rssi = self.rssi_func(distance, n, A)

        return rssi

    def compute_pdr(self, src, dst):
        assert sorted(src.keys()) == sorted(["mote", "coordinate"])
        assert sorted(dst.keys()) == sorted(["mote", "coordinate"])

        # 计算距离
        distance = self._get_distance_in_meters(src["coordinate"], dst["coordinate"])

        # 使用拟合的 PDR 模型计算 PDR
        k, x0 = self.PDR_model_parameters
        pdr = self.pdr_func(distance, k, x0)

        return pdr

    def _get_distance_in_meters(self, a, b):
        """Compute distance in meters between two points of a and b

        a and b are tuples which are 2D coordinates expressed in
        kilometers.
        """
        return math.sqrt(pow((b[0] - a[0]), 2) + pow((b[1] - a[1]), 2))
    
    def calculate_packet_duration(self, payload_byte=None):
        """
        根据调制类型自动识别无线帧类型，并计算总时长 (us)
        """
        if payload_byte is None:
            payload_byte = self.MAX_PAYLOAD_SIZE
            
        modulation = self.get_modulation()  # 获取当前调制方式
        bandwidth = self.get_bandwidth() # 获取当前带宽
        
        if bandwidth == "1M":
            symbol_rate_mhz = 1
        elif bandwidth == "2M":
            symbol_rate_mhz = 2
        elif bandwidth == "4M":
            symbol_rate_mhz = 4
        
        # --- 共有基础开销 ---
        t_preamble = 10  # 前导信号固定 10us
        
        # --- 分模式计算 ---
        if modulation == "GFSK":
            # 【无线帧类型 1 逻辑】
            # 同步序列: 32位 -> GFSK 调制产生 32 符号
            t_sync = 32 / symbol_rate_mhz
            # PHI (控制信息): 不进行信道编码，假设固定 24 bits
            t_phi = 24 / symbol_rate_mhz
            # 数据段: GFSK (1 bit/symbol), 不进行信道编码
            total_bits = payload_byte * 8 + 24  # Payload + 24bit CRC
            data_symbols = total_bits
            
        elif modulation in ["QPSK", "8PSK"]:
            # 【无线帧类型 2 逻辑】
            # 同步序列: 64位 -> QPSK 调制产生 32 符号
            t_sync = 32 / symbol_rate_mhz
            # PHI: 极化码产生 64bit -> QPSK 产生 32 符号 + 1 导频
            t_phi = 33 / symbol_rate_mhz
            
            # 数据段: 需要考虑极化码编码率 R (假设 R=0.5) 和 符号映射
            r_coding = 0.5 
            total_bits = payload_byte * 8 + 24  # Payload + 24bit CRC
            encoded_bits = math.ceil(total_bits / r_coding)
            
            # 映射到符号
            bits_per_symbol = 2 if modulation == "QPSK" else 3
            data_symbols = math.ceil(encoded_bits / bits_per_symbol)
            
        else:
            raise ValueError(f"Unknown modulation: {modulation}")
        t_frame_us = t_preamble + t_sync + t_phi + (data_symbols / symbol_rate_mhz)

        return t_frame_us / 1000000  # 转换为秒

    def compute_slot_duration(self):
        max_packet_duration = self.calculate_packet_duration()
        max_ack_duration = self.calculate_packet_duration(payload_byte=30)
        tx_slotDuration = (
            self.TsTxOffset + max_packet_duration + self.TsRxAckWait + max_ack_duration + self.RADIO_RAMPUP_RAMPDOWN_TIME * 2 + self.GUARD_TIME
        )
        rx_slotDuration = (
            self.TsRxOffset + max_packet_duration + self.TsTxAckDelay + max_ack_duration + self.RADIO_RAMPUP_RAMPDOWN_TIME * 2 + self.GUARD_TIME
        )
        slotDuration = max(tx_slotDuration, rx_slotDuration)
        print("raw_slotDuration: {:.6f} seconds".format(slotDuration))
        
        # 向上取整到最近的 0.5 ms
        step = 0.0005  # 0.5 ms
        slotDuration = math.ceil(slotDuration / step) * step
        slotDuration = round(slotDuration, 6) # 使用 round 处理浮点数精度残留，确保输出干净
        print("adjusted_slotDuration: {:.6f} seconds".format(slotDuration))
        return slotDuration
        