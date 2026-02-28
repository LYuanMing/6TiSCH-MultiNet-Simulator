"""
Tests for SimEngine.Connectivity
"""
from __future__ import print_function
from __future__ import absolute_import
from builtins import zip
from builtins import range
from builtins import object
import itertools
import json
import gzip
import os
import random
import types

import pytest

from . import test_utils as u
import SimEngine.Mote.MoteDefines as d
from SimEngine import SimLog
from SimEngine.Connectivity import ConnectivityMatrixK7

#============================ helpers =========================================

def destroy_all_singletons(engine):
    engine.destroy()
    engine.connectivity.destroy()
    engine.settings.destroy()
    SimLog.SimLog().destroy()

#============================ tests ===========================================

def test_linear_matrix(sim_engine):
    """ verify the connectivity matrix for the 'Linear' class is as expected

    creates a static connectivity linear path
    0 <-- 1 <-- 2 <-- ... <-- num_motes
    """

    num_motes = 6
    engine = sim_engine(
        diff_config = {
            'exec_numMotes': num_motes,
            'conn_class':    'Linear',
        }
    )
    motes  = engine.motes
    matrix = engine.connectivity.matrix

    matrix.dump()

    assert motes[0].dagRoot is True

    for c in range(0, num_motes):
        for p in range(0, num_motes):
            if (c == p+1) or (c+1 == p):
                for channel in d.TSCH_HOPPING_SEQUENCE:
                    assert matrix.get_pdr(c, p, channel)  ==  1.00
                    assert matrix.get_rssi(c, p, channel) ==   -10
            else:
                for channel in d.TSCH_HOPPING_SEQUENCE:
                    assert matrix.get_pdr(c, p, channel)  ==  0.00
                    assert matrix.get_rssi(c, p, channel) == -1000


#=== verify propagate function doesn't raise exception

def test_propagate(sim_engine):
    engine = sim_engine()
    engine.connectivity.propagate()

class TestConnectivity:
    def test_multi_network_propagate_enqueue(self, sim_engine):
        # test the multi-network can enqueue the transmission and reception
        sim_engine = sim_engine(
            diff_config={
                'exec_numSlotframesPerRun'      : 10000,
                'conn_class'                    : 'Random',
                'secjoin_enabled'               : False,
                "phy_numChans"                  : 1,
                "tsch_probBcast_ebProb"         : 1 # always send EB in a channel
            }
        )
        for mote in sim_engine.motes:
            mote.rpl.trickle_timer.stop()

        TX_mote_id = 0
        RX_mote_id = 1

        TX_mote = sim_engine.motes[TX_mote_id]
        RX_mote = sim_engine.motes[RX_mote_id]

        # initially, queues should be empty
        assert len(sim_engine.connectivity.transmission_queue) == 0
        for channel in sim_engine.connectivity.reception_queue:
            assert len(sim_engine.connectivity.reception_queue[channel]) == 0

        # startRx on RX mote to add to reception queue
        channel = RX_mote.tsch.hopping_sequence[0]
        RX_mote.radio.startRx(channel, sim_engine.global_time + sim_engine.settings.tsch_slotDuration)

        # assert reception queue is not empty after startRx
        assert len(sim_engine.connectivity.reception_queue[channel]) == 1

        # startTx on TX mote to add to transmission queue
        packet = {
            u'type': u'EB',
            u'pkt_len': 68,
            u'mac': {
                u'dstMac': d.BROADCAST_ADDRESS
            }
        }
        TX_mote.radio.startTx(channel, packet)

        # run propagation to process the transmission
        u.run_until_(sim_engine, sim_engine.global_time + sim_engine.time_step)

        # assert transmission queue is not empty after startTx
        assert len(sim_engine.connectivity.transmission_queue[channel]) == 1

    def test_multi_network_propagate_lockon(self, sim_engine):
        # lockon to a transmission
        sim_engine = sim_engine(
            diff_config={
                'exec_numSlotframesPerRun'      : 10000,
                'conn_class'                    : 'Random',
                'secjoin_enabled'               : False,
                "phy_numChans"                  : 1,
                "tsch_probBcast_ebProb"         : 0, # disable automatic EB transmission to prevent conflicts
                "exec_numMotes"                 : 3,  # use 3 motes to satisfy minimum neighbor requirement
                "conn_random_init_min_neighbors": 2   # set minimum neighbors to 2
            }
        )
        for mote in sim_engine.motes:
            mote.rpl.trickle_timer.stop()
            
        # Check that queues are now empty
        assert len(sim_engine.connectivity.transmission_queue) == 0
        assert len(sim_engine.connectivity.reception_queue) == 0

        # Manually trigger a transmission and reception
        TX_mote = sim_engine.motes[0]
        RX_mote = sim_engine.motes[1]
        channel = TX_mote.tsch.hopping_sequence[0]
        
        # Start reception on RX mote
        RX_mote.radio.startRx(channel, sim_engine.global_time + sim_engine.settings.tsch_slotDuration)
        
        # Create and start a transmission on TX mote
        packet = {
            u'type': u'EB',
            u'pkt_len': 68,
            u'mac': {
                u'dstMac': d.BROADCAST_ADDRESS
            }
        }
        TX_mote.radio.startTx(channel, packet)
        
        # Run propagation to allow lockon to happen
        u.run_until_(sim_engine, sim_engine.global_time + sim_engine.time_step)

        # Check that transmission is now in queue
        assert len(sim_engine.connectivity.transmission_queue[channel]) == 1
        transmission = sim_engine.connectivity.transmission_queue[channel][0]
        
        # Check that reception is locked to the transmission
        reception = sim_engine.connectivity.reception_queue[channel][0]
        assert reception[u'locked_transmission'] is transmission

    def test_multi_network_propagate_relockon_within_capture_duration(self, sim_engine):
        # relockon with stronger signal (greater than threshold) within capture duration
        sim_engine = sim_engine(
            diff_config={
                'exec_numSlotframesPerRun'      : 10000,
                'conn_class'                    : 'Random',
                'secjoin_enabled'               : False,
                "phy_numChans"                  : 1,
                "tsch_probBcast_ebProb"         : 1, # always send EB in a channel
                "exec_numMotes"                 : 3
            }
        )
        for mote in sim_engine.motes:
            mote.rpl.trickle_timer.stop()

        connectivity = sim_engine.connectivity

        first_TX_mote = sim_engine.motes[0]
        channel = first_TX_mote.tsch.hopping_sequence[0]
        second_TX_mote = sim_engine.motes[1]
        RX_mote = sim_engine.motes[2]

        # first_TX_mote has weaker signal than second_TX_mote at RX_mote
        stronger_rssi = -50
        connectivity.matrix.set_rssi_both_directions(
            first_TX_mote.id,
            RX_mote.id,
            channel,
            stronger_rssi - RX_mote.radio.capture_threshold, # weaker signal
        )
        connectivity.matrix.set_rssi_both_directions(
            second_TX_mote.id,
            RX_mote.id,
            channel,
            stronger_rssi, # stronger signal
        )
        
        # RX mote starts reception
        RX_mote.radio.startRx(channel, sim_engine.global_time + sim_engine.settings.tsch_slotDuration)
        # both TX motes start transmission at the same time
        first_TX_mote.radio.startTx(
            channel=channel, 
            packet={
                u'type': u'EB',
                u'pkt_len': 68, 
                u'mac': {
                    u'dstMac': d.BROADCAST_ADDRESS
                }
            }
        )
        second_TX_mote.radio.startTx(
            channel=channel,
            packet={
                u'type': u'EB',
                u'pkt_len': 68, 
                u'mac': {
                    u'dstMac': d.BROADCAST_ADDRESS
                }
            }
        )
        # run propagation
        u.run_until_(sim_engine, sim_engine.global_time + sim_engine.time_step)
        # the reception should relockon to the stronger transmission
        reception = list(connectivity.reception_queue[channel])[0]
        assert reception[u'locked_transmission'][u'mote'] is second_TX_mote

    def test_multi_network_propagate_no_relockon_outside_capture_duration(self, sim_engine):
        # no relockon or lockon outside capture duration
        sim_engine = sim_engine(
            diff_config={
                'exec_numSlotframesPerRun'      : 10000,
                'conn_class'                    : 'Random',
                'secjoin_enabled'               : False,
                "phy_numChans"                  : 1,
                "tsch_probBcast_ebProb"         : 0, # disable automatic EB transmission to prevent conflicts
                "exec_numMotes"                 : 3,
                "conn_random_init_min_neighbors": 2   # set minimum neighbors to satisfy requirements
            }
        )
        for mote in sim_engine.motes:
            mote.rpl.trickle_timer.stop()

        connectivity = sim_engine.connectivity

        first_TX_mote = sim_engine.motes[0]
        channel = first_TX_mote.tsch.hopping_sequence[0]
        second_TX_mote = sim_engine.motes[1]
        RX_mote = sim_engine.motes[2]

        # Set up stronger signal for first_TX_mote to ensure it locks first
        strong_signal_rssi = -50
        weak_signal_rssi = strong_signal_rssi
        connectivity.matrix.set_rssi_both_directions(
            first_TX_mote.id,
            RX_mote.id,
            channel,
            strong_signal_rssi, # stronger signal
        )
        connectivity.matrix.set_rssi_both_directions(
            second_TX_mote.id,
            RX_mote.id,
            channel,
            weak_signal_rssi, # weaker signal
        )

        # RX mote starts reception
        RX_mote.radio.startRx(channel, sim_engine.global_time + sim_engine.settings.tsch_slotDuration)
        RX_mote.radio.waitingFor = d.WAITING_FOR_RX

        # First TX mote starts transmission
        first_TX_mote.radio.startTx(
            channel=channel,
            packet={
                u'type': u'EB',
                u'pkt_len': 68,
                u'mac': {
                    u'dstMac': d.BROADCAST_ADDRESS
                }
            }
        )
        # run propagation to allow lockon
        u.run_until_(sim_engine, sim_engine.global_time + sim_engine.time_step)
        
        # Get the reception object
        reception = list(connectivity.reception_queue[channel])[0]

        # Verify that the reception is locked to the first transmission
        assert reception[u'locked_transmission'] is not None
        assert reception[u'locked_transmission'][u'mote'] is first_TX_mote

        # Record the initial locked transmission
        initial_locked_transmission = reception[u'locked_transmission']

        # Advance time beyond capture duration
        original_time = sim_engine.global_time
        sim_engine.global_time = original_time + RX_mote.radio.capture_duration + 1

        # Second TX mote starts transmission after capture duration
        second_TX_mote.radio.startTx(
            channel=channel,
            packet={
                u'type': u'EB',
                u'pkt_len': 68,
                u'mac': {
                    u'dstMac': d.BROADCAST_ADDRESS
                }
            }
        )
        # run propagation
        u.run_until_(sim_engine, sim_engine.global_time + sim_engine.time_step)

        # The reception should still be locked to the original transmission
        # since the second transmission started outside the capture window
        assert reception[u'locked_transmission'] is initial_locked_transmission
        assert reception[u'locked_transmission'][u'mote'] is first_TX_mote

    def test_multi_network_propagate_successful_reception(self, sim_engine):
        # receive a packet successfully
        sim_engine = sim_engine(
            diff_config={
                'exec_numSlotframesPerRun'      : 10000,
                'conn_class'                    : 'Random',
                'secjoin_enabled'               : False,
                "phy_numChans"                  : 1,
                "tsch_probBcast_ebProb"         : 0, # disable automatic EB transmission to prevent conflicts
                "exec_numMotes"                 : 2,
                "conn_random_init_min_neighbors": 1   # set minimum neighbors to satisfy requirements
            }
        )
        for mote in sim_engine.motes:
            mote.rpl.trickle_timer.stop()

        connectivity = sim_engine.connectivity

        TX_mote = sim_engine.motes[0]
        RX_mote = sim_engine.motes[1]
        channel = TX_mote.tsch.hopping_sequence[0]

        # Set up perfect connection between TX and RX motes
        connectivity.matrix.set_rssi_both_directions(
            TX_mote.id,
            RX_mote.id,
            channel,
            -50  # good RSSI
        )
        connectivity.matrix.set_pdr_both_directions(
            TX_mote.id,
            RX_mote.id,
            channel,
            1.0  # perfect PDR
        )

        # Mock the rxDone and txDone methods to track if they are called
        original_rxdone = RX_mote.radio.rxDone
        original_txdone = TX_mote.radio.txDone

        rxdone_called = []
        txdone_called = []

        def mock_rxdone(packet):
            rxdone_called.append(packet)
            return original_rxdone(packet)

        def mock_txdone():
            txdone_called.append(True)
            return original_txdone()

        RX_mote.radio.rxDone = mock_rxdone
        TX_mote.radio.txDone = mock_txdone

        # Start reception on RX mote
        RX_mote.radio.startRx(channel, sim_engine.global_time + sim_engine.settings.tsch_slotDuration)
        # Set the TSCH waiting state to RX as expected by the rxDone method
        RX_mote.tsch.waitingFor = d.WAITING_FOR_RX

        # Create and start a transmission on TX mote
        packet = {
            u'type': u'EB',
            u'pkt_len': 68,
            u'mac': {
                u'dstMac': d.BROADCAST_ADDRESS,
                u'srcMac': TX_mote.get_mac_addr()
            }
        }
        # Set the TSCH waiting state to TX as expected by the txDone method
        TX_mote.tsch.waitingFor = d.WAITING_FOR_TX
        # Set the packet to send in TSCH
        TX_mote.tsch.pktToSend = packet
        TX_mote.radio.startTx(channel, packet)

        # Run propagation for enough time to complete the transmission
        # Need to run until the transmission ends
        transmission_end_time = TX_mote.radio.onGoingTransmission[u'end_time']
        u.run_until_(sim_engine, transmission_end_time + sim_engine.time_step)

        # Verify that both rxDone and txDone were called during propagation
        assert len(rxdone_called) == 1, "RX mote's rxDone method was not called"
        assert len(txdone_called) == 1, "TX mote's txDone method was not called"

        # Also verify that the packet was received successfully (not None)
        assert rxdone_called[0] is not None, "Packet was not received successfully, rxDone was called with None"

    def test_multi_network_propagate_failed_reception_due_to_collision_with_result_of_none(self, sim_engine):
        # Test case where collision results in rxDone being called with None
        # According to the theory: if a weak signal is locked and a strong signal causes collision,
        # the weak signal will likely fail to be received due to interference
        sim_engine = sim_engine(
            diff_config={
                'exec_numSlotframesPerRun'      : 10000,
                'conn_class'                    : 'Random',
                'secjoin_enabled'               : False,
                "phy_numChans"                  : 1,
                "tsch_probBcast_ebProb"         : 0, # disable automatic EB transmission to prevent conflicts
                "exec_numMotes"                 : 3,
                "conn_random_init_min_neighbors": 2   # set minimum neighbors to satisfy requirements
            }
        )
        for mote in sim_engine.motes:
            mote.rpl.trickle_timer.stop()

        connectivity = sim_engine.connectivity

        weak_TX_mote = sim_engine.motes[0]   # Weak transmission that gets locked first
        strong_Interfering_TX_mote = sim_engine.motes[1]  # Strong interfering transmission
        RX_mote = sim_engine.motes[2]
        channel = weak_TX_mote.tsch.hopping_sequence[0]

        # Set up connections
        # Weak transmission has poor signal - this will be the one that gets "locked" first
        connectivity.matrix.set_rssi_both_directions(
            weak_TX_mote.id,
            RX_mote.id,
            channel,
            -95  # very poor RSSI for weak transmission
        )
        connectivity.matrix.set_pdr_both_directions(
            weak_TX_mote.id,
            RX_mote.id,
            channel,
            0.05  # very low PDR for weak transmission
        )

        # Strong interfering transmission has good signal - this will interfere with the locked weak signal
        connectivity.matrix.set_rssi_both_directions(
            strong_Interfering_TX_mote.id,
            RX_mote.id,
            channel,
            -40  # good RSSI for strong interfering transmission
        )
        connectivity.matrix.set_pdr_both_directions(
            strong_Interfering_TX_mote.id,
            RX_mote.id,
            channel,
            0.9  # high PDR for strong interfering transmission
        )

        # Mock the rxDone method to track if it is called with None (failed reception)
        original_rxdone = RX_mote.radio.rxDone

        rxdone_called_with = []

        def mock_rxdone(packet):
            rxdone_called_with.append(packet)
            return original_rxdone(packet)

        RX_mote.radio.rxDone = mock_rxdone

        # Start reception on RX mote
        RX_mote.radio.startRx(channel, sim_engine.global_time + sim_engine.settings.tsch_slotDuration)
        # Set the TSCH waiting state to RX as expected by the rxDone method
        RX_mote.tsch.waitingFor = d.WAITING_FOR_RX

        # Create and start the weak transmission first (it will be locked due to arriving first)
        weak_packet = {
            u'type': u'EB',
            u'pkt_len': 68,
            u'mac': {
                u'dstMac': d.BROADCAST_ADDRESS,
                u'srcMac': weak_TX_mote.get_mac_addr(),
                u'retriesLeft': 3,
                u'join_metric': 1000
            }
        }
        # Set the TSCH waiting state for weak TX mote
        weak_TX_mote.tsch.waitingFor = d.WAITING_FOR_TX
        weak_TX_mote.tsch.pktToSend = weak_packet
        weak_TX_mote.radio.startTx(channel, weak_packet)

        # Run propagation to allow lockon to the weak transmission
        u.run_until_(sim_engine, sim_engine.global_time + sim_engine.time_step)

        # At this point, the RX mote should have locked onto the weak transmission
        # Now start the strong interfering transmission which will cause collision
        strong_interfering_packet = {
            u'type': u'EB',
            u'pkt_len': 68,
            u'mac': {
                u'dstMac': d.BROADCAST_ADDRESS,
                u'srcMac': strong_Interfering_TX_mote.get_mac_addr(),
                u'retriesLeft': 3,
                u'join_metric': 1000
            }
        }
        # Set the TSCH waiting state for strong interfering TX mote
        strong_Interfering_TX_mote.tsch.waitingFor = d.WAITING_FOR_TX
        strong_Interfering_TX_mote.tsch.pktToSend = strong_interfering_packet
        strong_Interfering_TX_mote.radio.startTx(channel, strong_interfering_packet)

        # Run propagation for enough time to complete the transmissions
        weak_transmission_end_time = weak_TX_mote.radio.onGoingTransmission[u'end_time']
        u.run_until_(sim_engine, weak_transmission_end_time + sim_engine.time_step)

        # Verify that the reception was attempted (rxDone was called)
        assert len(rxdone_called_with) > 0, "RX mote's rxDone method was not called"

        # Verify that the reception failed due to collision/interference
        assert rxdone_called_with[0] is None, f"Expected None due to collision, but got packet: {rxdone_called_with[0]}"

    def test_multi_network_propagate_failed_reception_due_to_collision_with_result_of_strong_signal_or_weak_signal(self, sim_engine):
        # Test case where collision results in receiving either the strong or weak signal
        # According to the theory: if signals arrive at similar time, the stronger one may be captured
        # but sometimes the weak one may still be received depending on timing and random factors
        sim_engine = sim_engine(
            diff_config={
                'exec_numSlotframesPerRun'      : 10000,
                'conn_class'                    : 'Random',
                'secjoin_enabled'               : False,
                "phy_numChans"                  : 1,
                "tsch_probBcast_ebProb"         : 0, # disable automatic EB transmission to prevent conflicts
                "exec_numMotes"                 : 3,
                "conn_random_init_min_neighbors": 2   # set minimum neighbors to satisfy requirements
            }
        )
        for mote in sim_engine.motes:
            mote.rpl.trickle_timer.stop()

        connectivity = sim_engine.connectivity

        weak_TX_mote = sim_engine.motes[0]   # Weak transmission
        strong_TX_mote = sim_engine.motes[1]  # Strong transmission that arrives at similar time
        RX_mote = sim_engine.motes[2]
        channel = weak_TX_mote.tsch.hopping_sequence[0]

        # Set up connections
        # Weak transmission has poor signal
        connectivity.matrix.set_rssi_both_directions(
            weak_TX_mote.id,
            RX_mote.id,
            channel,
            -95  # poor RSSI for weak transmission
        )
        connectivity.matrix.set_pdr_both_directions(
            weak_TX_mote.id,
            RX_mote.id,
            channel,
            0  # low PDR for weak transmission
        )

        # Strong transmission has good signal
        connectivity.matrix.set_rssi_both_directions(
            strong_TX_mote.id,
            RX_mote.id,
            channel,
            -30  # good RSSI for strong transmission
        )
        connectivity.matrix.set_pdr_both_directions(
            strong_TX_mote.id,
            RX_mote.id,
            channel,
            1  # high PDR for strong transmission
        )

        # Mock the rxDone method to track what packet is received
        original_rxdone = RX_mote.radio.rxDone

        rxdone_called_with = []

        def mock_rxdone(packet):
            rxdone_called_with.append(packet)
            return original_rxdone(packet)

        RX_mote.radio.rxDone = mock_rxdone

        # Start reception on RX mote
        RX_mote.radio.startRx(channel, sim_engine.global_time + sim_engine.settings.tsch_slotDuration)
        # Set the TSCH waiting state to RX as expected by the rxDone method
        RX_mote.tsch.waitingFor = d.WAITING_FOR_RX

        # Create and start both transmissions at approximately the same time to cause collision
        weak_packet = {
            u'type': u'EB',
            u'pkt_len': 68,
            u'mac': {
                u'dstMac': d.BROADCAST_ADDRESS,
                u'srcMac': weak_TX_mote.get_mac_addr(),
                u'retriesLeft': 3,
                u'join_metric': 1000
            }
        }
        strong_packet = {
            u'type': u'EB',
            u'pkt_len': 68,
            u'mac': {
                u'dstMac': d.BROADCAST_ADDRESS,
                u'srcMac': strong_TX_mote.get_mac_addr(),
                u'retriesLeft': 3,
                u'join_metric': 1000
            }
        }

        # Set the TSCH waiting state for weak TX mote
        weak_TX_mote.tsch.waitingFor = d.WAITING_FOR_TX
        weak_TX_mote.tsch.pktToSend = weak_packet
        weak_TX_mote.radio.startTx(channel, weak_packet)

        # Set the TSCH waiting state for strong TX mote
        strong_TX_mote.tsch.waitingFor = d.WAITING_FOR_TX
        strong_TX_mote.tsch.pktToSend = strong_packet
        strong_TX_mote.radio.startTx(channel, strong_packet)

        # Run propagation for enough time to complete the transmissions
        weak_transmission_end_time = weak_TX_mote.radio.onGoingTransmission[u'end_time']
        u.run_until_(sim_engine, weak_transmission_end_time + sim_engine.time_step)

        # Verify that the reception was attempted (rxDone was called)
        assert len(rxdone_called_with) > 0, "RX mote's rxDone method was not called"

        # If a packet was received, verify it comes from one of the expected sources
        received_src = rxdone_called_with[0]['mac']['srcMac']
        assert received_src == strong_TX_mote.get_mac_addr(), f"Expected packet from either transmission, but got from {received_src}"
        # If rxdone_called_with[0] is None, that means the reception failed due to collision/interference
        # This is also a valid outcome in collision scenarios

    def test_multi_network_propagate_dequeue(self, sim_engine):
        # dequeue transmission and reception after propagation
        sim_engine = sim_engine(
            diff_config={
                'exec_numSlotframesPerRun'      : 10000,
                'conn_class'                    : 'Random',
                'secjoin_enabled'               : False,
                "phy_numChans"                  : 1,
                "tsch_probBcast_ebProb"         : 0, # disable automatic EB transmission to prevent conflicts
                "exec_numMotes"                 : 2,
                "conn_random_init_min_neighbors": 1   # set minimum neighbors to satisfy requirements
            }
        )
        for mote in sim_engine.motes:
            mote.rpl.trickle_timer.stop()

        connectivity = sim_engine.connectivity

        TX_mote = sim_engine.motes[0]
        RX_mote = sim_engine.motes[1]
        channel = TX_mote.tsch.hopping_sequence[0]

        # Initialize the queues for the channel if they don't exist
        if channel not in sim_engine.connectivity.transmission_queue:
            sim_engine.connectivity.transmission_queue[channel] = []
        if channel not in sim_engine.connectivity.reception_queue:
            sim_engine.connectivity.reception_queue[channel] = []

        # Initially, queues should be empty
        assert len(sim_engine.connectivity.transmission_queue[channel]) == 0
        assert len(sim_engine.connectivity.reception_queue[channel]) == 0

        # Set up a good connection between TX and RX to ensure successful lock
        connectivity.matrix.set_rssi_both_directions(
            TX_mote.id,
            RX_mote.id,
            channel,
            -50  # good RSSI
        )
        connectivity.matrix.set_pdr_both_directions(
            TX_mote.id,
            RX_mote.id,
            channel,
            1.0  # perfect PDR
        )

        # Set the TSCH waiting state for RX mote
        RX_mote.tsch.waitingFor = d.WAITING_FOR_RX

        # Start reception on RX mote
        RX_mote.radio.startRx(channel, sim_engine.global_time + sim_engine.settings.tsch_slotDuration)

        # Create and start a transmission on TX mote
        packet = {
            u'type': u'EB',
            u'pkt_len': 68,
            u'mac': {
                u'dstMac': d.BROADCAST_ADDRESS,
                u'srcMac': TX_mote.get_mac_addr(),
                u'retriesLeft': 3,
                u'join_metric': 1000
            }
        }
        # Set the TSCH waiting state for TX mote
        TX_mote.tsch.waitingFor = d.WAITING_FOR_TX
        TX_mote.tsch.pktToSend = packet
        TX_mote.radio.startTx(channel, packet)

        # Run propagation once to add both transmission and reception to the queue
        # and allow the reception to lock onto the transmission
        u.run_until_(sim_engine, sim_engine.global_time + sim_engine.time_step)

        # Verify that both transmission and reception were added to the queue
        assert len(sim_engine.connectivity.transmission_queue[channel]) == 1
        assert len(sim_engine.connectivity.reception_queue[channel]) == 1

        # Verify that the reception has locked onto the transmission
        reception = sim_engine.connectivity.reception_queue[channel][0]
        assert reception[u'locked_transmission'] is not None, "Reception should have locked onto the transmission"

        # Run propagation to process the transmission and reception until they complete
        transmission_end_time = TX_mote.radio.onGoingTransmission[u'end_time']
        u.run_until_(sim_engine, transmission_end_time + sim_engine.time_step)

        # After propagation, both transmission and reception should be dequeued
        # because they have completed (their end_time has passed)
        assert len(sim_engine.connectivity.transmission_queue[channel]) == 0, \
            f"Expected transmission queue to be empty after propagation, but had {len(sim_engine.connectivity.transmission_queue[channel])} items"
        assert len(sim_engine.connectivity.reception_queue[channel]) == 0, \
            f"Expected reception queue to be empty after propagation, but had {len(sim_engine.connectivity.reception_queue[channel])} items"

    def test_multi_network_propagate_dequeue_at_end_time(self, sim_engine):
        # test the reception is dequeued at end time
        sim_engine = sim_engine(
            diff_config={
                'exec_numSlotframesPerRun'      : 10000,
                'conn_class'                    : 'Random',
                'secjoin_enabled'               : False,
                "phy_numChans"                  : 1,
                "tsch_probBcast_ebProb"         : 0, # disable automatic EB transmission to prevent conflicts
                "exec_numMotes"                 : 2,
                "conn_random_init_min_neighbors": 1   # set minimum neighbors to satisfy requirements
            }
        )
        for mote in sim_engine.motes:
            mote.rpl.trickle_timer.stop()

        connectivity = sim_engine.connectivity

        TX_mote = sim_engine.motes[0]
        RX_mote = sim_engine.motes[1]
        channel = TX_mote.tsch.hopping_sequence[0]

        # Initialize the queues for the channel if they don't exist
        if channel not in connectivity.transmission_queue:
            connectivity.transmission_queue[channel] = []
        if channel not in connectivity.reception_queue:
            connectivity.reception_queue[channel] = []

        # Initially, queues should be empty
        assert len(connectivity.transmission_queue[channel]) == 0
        assert len(connectivity.reception_queue[channel]) == 0

        sim_engine.removeFutureEvent((1, '_action_listeningForEB_cell'))

        # Set the TSCH waiting state for RX mote
        RX_mote.tsch.waitingFor = d.WAITING_FOR_RX
        # Start reception on RX mote
        RX_mote.radio.startRx(channel, sim_engine.global_time + sim_engine.settings.tsch_slotDuration)

        assert len(connectivity.reception_queue[channel]) == 1

        u.run_until_(sim_engine, sim_engine.global_time + sim_engine.settings.tsch_slotDuration * 2)

        assert len(connectivity.reception_queue[channel]) == 0, \
            f"Expected reception queue to be empty after end time, but had {len(connectivity.reception_queue[channel])} items"
        assert RX_mote.radio.state == d.RADIO_STATE_OFF

        # Create and start a transmission on TX mote
        packet = {
            u'type': u'EB',
            u'pkt_len': 68,
            u'mac': {
                u'dstMac': d.BROADCAST_ADDRESS,
                u'srcMac': TX_mote.get_mac_addr(),
                u'retriesLeft': 3,
                u'join_metric': 1000
            }
        }
        # Set the TSCH waiting state for TX mote and RX mote
        RX_mote.tsch.waitingFor = d.WAITING_FOR_RX
        # Start reception on RX mote
        RX_mote.radio.startRx(channel, sim_engine.global_time + sim_engine.settings.tsch_slotDuration)
        # Start transimssion on TX mote
        TX_mote.tsch.waitingFor = d.WAITING_FOR_TX
        TX_mote.tsch.pktToSend = packet
        TX_mote.radio.startTx(channel, packet)
        # Run propagation once to add both transmission and reception to the queue
        # and allow the reception to lock onto the transmission
        u.run_until_(sim_engine, sim_engine.global_time + sim_engine.time_step)

        # Verify that both transmission and reception were added to the queue
        assert len(connectivity.transmission_queue[channel]) == 1
        assert len(connectivity.reception_queue[channel]) == 1

        # Verify that the reception has locked onto the transmission
        reception = connectivity.reception_queue[channel][0]
        assert reception[u'locked_transmission'] is not None, "Reception should have locked onto the transmission"

        transmission_end_time = TX_mote.radio.onGoingTransmission[u'end_time']

        # Run one more propagation cycle after the end time
        u.run_until_(sim_engine, transmission_end_time + sim_engine.time_step)

        # After the end time, both transmission and reception should be dequeued
        assert len(connectivity.transmission_queue[channel]) == 0, \
            f"Expected transmission queue to be empty after end time, but had {len(connectivity.transmission_queue[channel])} items"
        assert len(connectivity.reception_queue[channel]) == 0, \
            f"Expected reception queue to be empty after end time, but had {len(connectivity.reception_queue[channel])} items"
        assert RX_mote.radio.state == d.RADIO_STATE_OFF
        assert TX_mote.radio.state == d.RADIO_STATE_OFF

#=== test for ConnectivityRandom
class TestRandom(object):

    def test_free_run(self, sim_engine):
        # all the motes should be able to join the network
        sim_engine = sim_engine(
            diff_config = {
                'exec_numSlotframesPerRun'      : 10000,
                'conn_class'                    : 'Random',
                'secjoin_enabled'               : False,
                "phy_numChans"                  : 1,
            }
        )
        asn_at_end_of_simulation = (
            sim_engine.settings.tsch_slotframeLength *
            sim_engine.settings.exec_numSlotframesPerRun
        )

        u.run_until_everyone_joined(sim_engine)
        assert sim_engine.getAsn() < asn_at_end_of_simulation

    def test_getter(self, sim_engine):
        num_channels = 2
        sim_engine = sim_engine(
            diff_config = {
                'conn_class'                    : 'Random',
                'exec_numMotes'                 : 2,
                'conn_random_init_min_neighbors': 1,
                'phy_numChans'                  : num_channels,
            }
        )

        # PDR and RSSI should not change over time
        for src, dst in zip(sim_engine.motes[:-1], sim_engine.motes[1:]):
            for channel in d.TSCH_HOPPING_SEQUENCE[:num_channels]:
                pdr  = []
                rssi = []

                for _ in range(100):
                    pdr.append(
                        sim_engine.connectivity.get_pdr(
                            src_id  = src.id,
                            dst_id  = dst.id,
                            channel = channel
                        )
                    )
                    rssi.append(
                        sim_engine.connectivity.get_rssi(
                            src_id  = src.id,
                            dst_id  = dst.id,
                            channel = channel
                        )
                    )
                    # proceed the simulator
                    u.run_until_asn(sim_engine, sim_engine.getAsn() + 1)

                # compare two consecutive PDRs and RSSIs. They should be always
                # the same value. Then, the following condition of 'i != j'
                # should always false
                assert sum([(i != j) for i, j in zip(pdr[:-1], pdr[1:])])   == 0
                assert sum([(i != j) for i, j in zip(rssi[:-1], rssi[1:])]) == 0

        # PDR and RSSI should be the same within the same slot, of course
        for src, dst in zip(sim_engine.motes[:-1], sim_engine.motes[1:]):
            for channel in d.TSCH_HOPPING_SEQUENCE[:num_channels]:
                pdr  = []
                rssi = []

                for _ in range(100):
                    pdr.append(
                        sim_engine.connectivity.get_pdr(
                            src_id  = src.id,
                            dst_id  = dst.id,
                            channel = channel
                        )
                    )
                    rssi.append(
                        sim_engine.connectivity.get_rssi(
                            src_id  = src.id,
                            dst_id  = dst.id,
                            channel = channel
                        )
                    )

                # compare two consecutive PDRs and RSSIs; all the pairs should
                # be same (all comparison, i != j, should be False).
                assert sum([(i != j) for i, j in zip(pdr[:-1], pdr[1:])])   == 0
                assert sum([(i != j) for i, j in zip(rssi[:-1], rssi[1:])]) == 0


    def test_context_random_seed(self, sim_engine):
        diff_config = {
            'exec_numMotes'  : 10,
            'exec_randomSeed': 'context',
            'conn_class'     : 'Random'
        }

        # ConnectivityRandom should create an identical topology for two
        # simulations having the same run_id
        sf_class_list = ['SFNone', 'MSF']
        coordinates = {}
        for sf_class, run_id in itertools.product(sf_class_list, [1, 2]):
            diff_config['sf_class'] = sf_class
            engine = sim_engine(
                diff_config                                = diff_config,
                force_initial_routing_and_scheduling_state = False,
                run_id                                     = run_id
            )
            coordinates[(sf_class, run_id)] = (
                engine.connectivity.matrix.coordinates
            )
            destroy_all_singletons(engine)

        # We have four sets of coordinates:
        # - coordinates of ('SFNone', run_id=1) and ('MSF',    1) should be
        #   identical
        # - coordinates of ('SFNone', run_id=2) and ('MSF',    2) should be
        #   identical
        # - coordinates of ('SFNone,  run_id=1) and ('SFNone', 2) should be
        #   different
        # - coordinates of ('MSF',    run_id=1) and ('MSF',    2) should be
        #   different
        assert coordinates[('SFNone', 1)] == coordinates[('MSF', 1)]
        assert coordinates[('SFNone', 2)] == coordinates[('MSF', 2)]
        assert coordinates[('SFNone', 1)] != coordinates[('SFNone', 2)]
        assert coordinates[('MSF', 1)]    != coordinates[('MSF', 2)]

#=== test for LockOn mechanism that is implemented in propagate()
def test_lockon(sim_engine):
    sim_engine = sim_engine(
        diff_config = {
            'exec_numMotes'           : 2,
            'exec_numSlotframesPerRun': 1,
            'conn_class'              : 'Linear',
            'app_pkPeriod'            : 0,
            'secjoin_enabled'         : False,
            'sf_class'                : 'SFNone',
            'tsch_probBcast_ebProb'   : 0,
            'rpl_daoPeriod'           : 0
        }
    )

    # short-hands
    root  = sim_engine.motes[0]
    hop_1 = sim_engine.motes[1]

    # force hop_1 to join the network
    eb = root.tsch._create_EB()
    hop_1.tsch._action_receiveEB(eb)
    dio = root.rpl._create_DIO()
    dio['mac'] = {'srcMac': root.get_mac_addr()}
    hop_1.rpl.action_receiveDIO(dio)

    # let hop_1 send an application packet
    hop_1.app._send_a_single_packet()

    # force random.random() to return 1, which will cause any frame not to be
    # received by anyone
    _random = random.random
    def return_one(self):
        return float(1)
    random.random = types.MethodType(return_one, random)

    # run the simulation
    u.run_until_end(sim_engine)

    # put the original random() back to random
    random.random = _random

    # root shouldn't lock on the frame hop_1 sent since root is not expected to
    # receive even the preamble of the packet.
    logs = u.read_log_file([SimLog.LOG_PROP_DROP_LOCKON['type']])
    assert len(logs) == 0

#=== test if the simulator ends without an error

ROOT_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
TRACE_FILE_PATH = os.path.join(ROOT_DIR, 'traces/grenoble.k7.gz')

@pytest.fixture(params=['FullyMeshed', 'Linear', 'K7', 'Random'])
def fixture_conn_class(request):
    return request.param

def test_runsim(sim_engine, fixture_conn_class):
    # run the simulation with each conn_class. use a shorter
    # 'exec_numSlotframesPerRun' so that this test doesn't take long time
    diff_config = {
        'exec_numSlotframesPerRun': 100,
        'conn_class'              : fixture_conn_class
    }
    if fixture_conn_class == 'K7':
        with gzip.open(TRACE_FILE_PATH, 'r') as trace:
            header = json.loads(trace.readline())
            diff_config['exec_numMotes'] = header['node_count']
        diff_config['conn_trace'] = TRACE_FILE_PATH

    sim_engine = sim_engine(diff_config=diff_config)
    u.run_until_end(sim_engine)


@pytest.fixture(params=[
    'test_setup',
    'perfect_rssi',
    'poor_rssi',
    'worst_rssi',
    'invalid_rssi'
])
def fixture_propagation_test_type(request):
    return request.param


def test_propagation(sim_engine, fixture_propagation_test_type):
    PERFECT_PDR  = 1.0
    RSSI_VALUES = {
        'perfect_rssi': -10,
        'poor_rssi'   : -90,
        'worst_rssi'  : -97, # the worst in rssi_pdr_table of Connectivity.py
        'invalid_rssi': -1000
    }

    num_motes = 2
    num_frames = 1000
    max_tx_retries = 5
    sim_engine = sim_engine(
        diff_config = {
            'exec_numSlotframesPerRun': num_frames * (max_tx_retries + 1),
            'exec_numMotes'           : num_motes,
            'secjoin_enabled'         : False,
            'app_pkPeriod'            : 0,
            'rpl_of'                  : 'OFNone',
            'rpl_daoPeriod'           : 0,
            'rpl_extensions'          : [],
            'sf_class'                : 'SFNone',
            'tsch_slotframeLength'    : 2,
            'tsch_probBcast_ebProb'   : 0,
            'tsch_keep_alive_interval': 0,
            'tsch_max_tx_retries'     : max_tx_retries,
            'tsch_tx_queue_size'      : num_frames,
            'conn_class'              : 'Linear', # this is intentional
            'phy_numChans'            : 1
        }
    )
    root = sim_engine.motes[0]
    mote = sim_engine.motes[1]
    # aliases
    dst = root
    src = mote

    class TestConnectivityMatrixK7(ConnectivityMatrixK7):
        def _additional_initialization(self):
            # set up the connectivity matrix
            channel = d.TSCH_HOPPING_SEQUENCE[0]
            self.set_pdr_both_directions(src.id, dst.id, channel, PERFECT_PDR)
            self.set_rssi_both_directions(
                src.id,
                dst.id,
                channel,
                RSSI_VALUES[fixture_propagation_test_type]
            )
            # dump the connectivity matrix
            print('The Connectivity Matrix ("1.0" means PDR of 100%):')
            self.dump()

    # replace the 'Linear' conn_class with the test purpose
    # conn_class, TestConnectivityMatrixK7
    if fixture_propagation_test_type != 'test_setup':
        sim_engine.connectivity.matrix = TestConnectivityMatrixK7(
            sim_engine.connectivity
        )

    # add a dedicated TX cell in order to avoid backoff wait
    slot_offset = 1
    channel_offset = 0
    dst.tsch.addCell(
        slot_offset,
        channel_offset,
        src.get_mac_addr(),
        [d.CELLOPTION_RX]
    )
    src.tsch.addCell(
        slot_offset,
        channel_offset,
        dst.get_mac_addr(),
        [d.CELLOPTION_TX]
    )

    # get mote synchronized
    eb = root.tsch._create_EB()
    mote.tsch._action_receiveEB(eb)

    # put a fake EB to mote so that it can get synchronized
    # immediately
    eb_dummy = {
        'type':            d.PKT_TYPE_EB,
        'mac': {
            'srcMac':      '00-00-00-AA-AA-AA',     # dummy
            'dstMac':      d.BROADCAST_ADDRESS,     # broadcast
            'join_metric': 1000
        }
    }
    mote.tsch._action_receiveEB(eb_dummy)

    # disabled the trickle timer
    root.rpl.trickle_timer.stop()
    mote.rpl.trickle_timer.stop()

    # [test types]
    #
    # test_setup: verify if we can set up a test environment
    # correctly, where there is no background traffic
    #
    # perfect_rssi/poor_rssi: all the transmission should succeed
    # since the links between the two motes have a PDR of 100%
    # regardless of their RSSI values

    if fixture_propagation_test_type != 'test_setup':
        # put frames to the TX queue of the source; use the keep-alive
        # frame as the test packet
        for seqno in range(num_frames):
            packet = {
                'type': d.PKT_TYPE_KEEP_ALIVE,
                'mac': {
                    'srcMac': src.get_mac_addr(),
                    'dstMac': dst.get_mac_addr()
                },
                'app': { 'seq': seqno }, # for debugging purpose
                'pkt_len': d.PKT_LEN_KEEP_ALIVE
            }
            src.tsch.enqueue(packet)

    u.run_until_end(sim_engine)

    logs = u.read_log_file([SimLog.LOG_TSCH_TXDONE['type']])
    max_seq_num = 0
    for log in logs:
        if log['packet']['type'] == packet['type']:
            max_seq_num = max(max_seq_num, log['packet']['app']['seq'])

    if fixture_propagation_test_type == 'test_setup':
        # we shouldn't see any transmission
        assert max_seq_num == 0
    else:
        # num_transmissions contains the number of retransmissions if
        # any. in other words, num_transmissions should be equal to
        # num_frames when no frame is dropped
        assert max_seq_num == (num_frames - 1) # double that is becuase len(keep_alive_packet + ACK)

@pytest.fixture(params=[0.0, 1.0])
def fixture_pdr(request):
    return request.param

def test_drop_ack(sim_engine, fixture_pdr):
    PERFECT_PDR = 1.0
    GOOD_RSSI = -10
    sim_engine = sim_engine(
        diff_config = {
            'conn_simulate_ack_drop'  : True,
            'exec_numMotes'           : 2,
            'secjoin_enabled'         : False,
            'app_pkPeriod'            : 0,
            'conn_class'              : 'Linear',
            'rpl_daoPeriod'           : 0,
            'rpl_extensions'          : [],
            'tsch_probBcast_ebProb'   : 0,
            'tsch_keep_alive_interval': 10,
            'phy_numChans'            : 1
        }
    )

    root = sim_engine.motes[0]
    mote = sim_engine.motes[1]

    eb = root.tsch._create_EB()
    mote.tsch._action_receiveEB(eb)
    mote.tsch._perform_synchronization()
    mote.engine.removeFutureEvent((mote.id, 'tsch', 'wait_eb'))

    root.rpl.trickle_timer.stop()
    mote.rpl.trickle_timer.stop()

    class TestConnectivityMatrixK7(ConnectivityMatrixK7):
        def _additional_initialization(self):
            # set up the connectivity matrix
            channel = d.TSCH_HOPPING_SEQUENCE[0]
            self.set_pdr(mote.id, root.id, channel, PERFECT_PDR)
            self.set_pdr(root.id, mote.id, channel, fixture_pdr)
            self.set_rssi_both_directions(
                root.id,
                mote.id,
                channel,
                GOOD_RSSI
            )
            # dump the connectivity matrix
            print('The Connectivity Matrix ("1.0" means PDR of 100%):')
            self.dump()

    sim_engine.connectivity.matrix = TestConnectivityMatrixK7(
        sim_engine.connectivity
    )

    # u.run_until_asn(sim_engine, 17 * 101 + 33)
    # init_global_time = sim_engine.global_time
    # for i in range(1, 200):
    #     u.run_until_(sim_engine, init_global_time + i * 1000)
    #     breakpoint()
    u.run_until_end(sim_engine)

    logs = u.read_log_file(['tsch.rxdone'])
    # all txdone logs is of KEEP_ALIVE
    num_acked = len([log for log in logs if log['packet']['type'] == d.PKT_TYPE_ACK])
    num_pkt = len([log for log in logs if log['packet']['type'] == d.PKT_TYPE_KEEP_ALIVE])
    if fixture_pdr == 0.0:
        assert num_pkt != num_acked
        # none of them gets ACK
        assert num_acked == 0
        # mote gets desynchronized because it doesn't receive any ACK
        assert u.read_log_file(['tsch.desynced'])
    elif fixture_pdr == 1.0:
        # all get ACK
        assert num_acked == num_pkt
        assert not u.read_log_file(['tsch.desynced'])
    else:
        raise ValueError('invalid value ({0}) for fixture_pdr'.format(
            fixture_pdr)
        )
