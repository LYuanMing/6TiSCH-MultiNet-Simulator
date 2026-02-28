
from SimEngine.Mote.NetDefines import Packet
from SimEngine.Mote import MoteDefines

class TestRadio:
    def test_startTx(self, sim_engine):
        # test startTx in the radio
        # when start TX, it must be a transmission in self.onGoingTransmission
        # and the state should be changed
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
        
        mote = sim_engine.motes[0]
        channel = 16
        assert mote is not None

        packet = Packet.from_dict({
                u'type':            MoteDefines.PKT_TYPE_EB,
                u'mac': {
                    u'srcMac':      mote.get_mac_addr(),
                    u'dstMac':      MoteDefines.BROADCAST_ADDRESS,     # broadcast
                    u'join_metric': mote.rpl.getDagRank() - 1
                },
                u'pkt_len':        MoteDefines.PKT_LEN_EB,  # bytes
        })

        assert mote.radio.state == MoteDefines.RADIO_STATE_OFF
        # send the packet
        mote.radio.startTx(channel, packet, start_time=sim_engine.global_time)
        
        assert mote.radio.onGoingTransmission["channel"] == channel
        assert mote.radio.onGoingTransmission["packet"] is packet
        assert mote.radio.onGoingTransmission["start_time"] == sim_engine.global_time

    def test_txDone(self, sim_engine):
        # test txDone in the radio
        # when end TX, it must call the tsch.txDone
        # and then close the radio, clear self.onGoingTransmission and self.channel
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
        
        mote = sim_engine.motes[0]
        channel = 16
        assert mote is not None

        packet = Packet.from_dict({
                u'type':            MoteDefines.PKT_TYPE_EB,
                u'mac': {
                    u'srcMac':      mote.get_mac_addr(),
                    u'dstMac':      MoteDefines.BROADCAST_ADDRESS,     # broadcast
                    u'join_metric': mote.rpl.getDagRank() - 1
                },
                u'pkt_len':        MoteDefines.PKT_LEN_EB,  # bytes
        })
        mote.radio.startTx(channel, packet)

        txdone_channel = None
        def mock_tsch_txDone(channel):
            nonlocal txdone_channel
            txdone_channel = channel
        mote.tsch.txDone = mock_tsch_txDone
        mote.radio.txDone()

        assert mote.radio.state == MoteDefines.RADIO_STATE_OFF
        assert txdone_channel == channel
        assert mote.radio.onGoingTransmission == None

    def test_startRx(self, sim_engine):
        # test startRx in the radio 
        # when start RX, it must be a reception in connectivity.reception_queue
        # and the state should be changed
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
        
        mote = sim_engine.motes[0]
        channel = 16
        assert mote is not None

        mote.radio.startRx(channel, start_time=sim_engine.global_time, end_time=sim_engine.global_time + sim_engine.settings.tsch_slotDuration)

        assert mote.radio.state == MoteDefines.RADIO_STATE_LISTENING
        reception = sim_engine.connectivity.reception_queue[channel][0]

        assert reception["mote"] is mote
        assert reception["channel"] == channel

    def test_rxDone(self, sim_engine):
        # test rxDone in the radio
        # when end RX, it must be call the tsch.rxDone
        # and then close the radio, clear the channel
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
        
        mote = sim_engine.motes[0]
        channel = 16
        assert mote is not None
    
        packet = Packet.from_dict({
                u'type':            MoteDefines.PKT_TYPE_EB,
                u'mac': {
                    u'srcMac':      mote.get_mac_addr(),
                    u'dstMac':      MoteDefines.BROADCAST_ADDRESS,     # broadcast
                    u'join_metric': mote.rpl.getDagRank() - 1
                },
                u'pkt_len':        MoteDefines.PKT_LEN_EB,  # bytes
        })
        from SimEngine.utils import dataclass_to_dict
        dataclass_to_dict(packet)
        received_packet = None
        reception_channel = None
        def mock_tsch_rxDone(packet, channel):
            nonlocal received_packet, reception_channel
            received_packet = packet
            reception_channel = channel

        mote.tsch.rxDone = mock_tsch_rxDone
        mote.radio.startRx(channel, sim_engine.global_time + sim_engine.settings.tsch_slotDuration)
        mote.radio.rxDone(packet)

        assert reception_channel == channel
        assert received_packet is packet
