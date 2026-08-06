"""
test multi network engine
"""
from __future__ import absolute_import
from builtins import range
from unittest.mock import MagicMock

import pytest

from SimEngine.SimSettings import SimSettings
from SimEngine import Mote
from SimEngine.MultiNetworkEngine import DiscreteEventEngine, MultiNetworkSimEngine, Event
from SimEngine.Mote.MoteDefines import INTRASLOTORDER_STARTSLOT
from SimEngine.SimEngineDefines import MILLISECOND
from SimEngine.SimLog import SimLog

class TestDiscreteEventEngine:

    @pytest.fixture(autouse=True)
    def reset_DEE_singleton(self):
        yield
        for k in list(DiscreteEventEngine._instances.keys()):
            instance = DiscreteEventEngine._instances[k]
            if instance:
                if hasattr(instance, "connectivity") and instance.connectivity:
                    instance.connectivity.destroy()

                if hasattr(instance, "settings") and instance.settings:
                    instance.settings.destroy()
                if hasattr(instance, "log") and instance.log:
                    instance.log.destroy()
                instance.destroy()
                

    def test_DEE_singleton(self):
        dee1 = DiscreteEventEngine()
        dee2 = DiscreteEventEngine()
        assert dee1 is dee2

    def test_DEE_reset_singleton(self):
        dee1 = DiscreteEventEngine()
        DiscreteEventEngine.reset_singleton()
        dee2 = DiscreteEventEngine()
        assert dee1 is not dee2

    def test_DEE_push_event(self):
        dee = DiscreteEventEngine()
        
        dee._push_event(event=Event(time=5, uniqueTag='event1', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))
        assert dee.events[0].time == 5
        assert len(dee.uniqueTagSchedule) == 1

        dee._push_event(event=Event(time=4, uniqueTag='event3', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))
        assert dee.events[0].time == 4
        assert len(dee.uniqueTagSchedule) == 2


        dee._push_event(event=Event(time=3, uniqueTag='event2', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))
        assert dee.events[0].time == 3
        assert len(dee.uniqueTagSchedule) == 3


    def test_DEE_pop_event(self):
        dee = DiscreteEventEngine()
        
        dee._push_event(event=Event(time=5, uniqueTag='event1', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))
        dee._push_event(event=Event(time=4, uniqueTag='event3', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=True)) # cancelled event, should be skipped
        dee._push_event(event=Event(time=3, uniqueTag='event2', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))

        event = dee._pop_event()
        assert event.time == 3
        assert event.uniqueTag == 'event2'
        assert len(dee.uniqueTagSchedule) == 2

        event = dee._pop_event()   
        assert event.time == 4
        assert event.uniqueTag == 'event3'  # although cancelled, still returned by _pop_event
        assert len(dee.uniqueTagSchedule) == 1

        event = dee._pop_event()
        assert event.time == 5
        assert event.uniqueTag == 'event1'
        # no more events
        event = dee._pop_event()
        assert event is None

    def test_DEE_pop_event_until_(self):
        dee = DiscreteEventEngine()

        dee._push_event(event=Event(time=5, uniqueTag='event1', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))
        dee._push_event(event=Event(time=4, uniqueTag='event3', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))
        dee._push_event(event=Event(time=3, uniqueTag='event2', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))

        event_list = dee._pop_event_until_(time=4)

        assert len(event_list) == 2
        assert len(dee.events) == len(dee.uniqueTagSchedule)  # ensure uniqueTagSchedule is in sync with events heap
        assert event_list[0].time == 3

    def test_DEE_check_schedule_required(self):
        dee = DiscreteEventEngine()
        
        assert dee._no_event_to_be_processed(dee.global_time+dee.time_step)

        dee._push_event(event=Event(time=5, uniqueTag='event1', callback='data1', intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))
        assert not dee._no_event_to_be_processed(dee.global_time+dee.time_step)
        
        dee._pop_event()
        assert dee._no_event_to_be_processed(dee.global_time+dee.time_step)


    def test_DEE_removeFutureEvent(self):
        dee = DiscreteEventEngine()
        
        dee._push_event(event=Event(time=5, uniqueTag='event1', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))
        dee._push_event(event=Event(time=4, uniqueTag='event2', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))
        dee._push_event(event=Event(time=3, uniqueTag='event3', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))

        assert len(dee.events) == 3
        assert len(dee.uniqueTagSchedule) == 3

        dee.removeFutureEvent('event2')

        assert len(dee.events) == 3  # event still in heap, but marked as cancelled
        assert len(dee.uniqueTagSchedule) == 3  # event removed from uniqueTagSchedule

        dee._push_event(event=Event(time=6, uniqueTag='event2', callback=None, intraSlotOrder=INTRASLOTORDER_STARTSLOT, cancelled=False))

        assert len(dee.events) == 4 # lazily removed cancelled event, new event added
        assert len(dee.uniqueTagSchedule) == 3  # event2 re-added

        event_list = dee._pop_event_until_(time=4)
        assert len(event_list) == 1  # event3 is popped, old event2 is skipped
        assert len(dee.uniqueTagSchedule) == 2  # event3 removed
        assert len(dee.events) == 2  # event1 and new event2 remains

    def test_DEE_run_basic(self):
        dee = DiscreteEventEngine()
        dee._process_single_event = MagicMock(name="_process_single_event") # mock a fake function

        dee.scheduleAtPreciseTime(Event(
            time=dee.global_time + dee.time_step,
            uniqueTag=("test", "run"),
            callback=None,
            intraSlotOrder=INTRASLOTORDER_STARTSLOT,
            cancelled=False
        ))
        
        dee.start()
        dee.join(timeout=1) # 1 sec should be enough for execution
        assert not dee.is_alive()
        dee._process_single_event.assert_called_once()

    def test_DEE_pause_and_resume(self):
        dee = DiscreteEventEngine()

        def real_process_single_event(event):
            if event:
                event.callback()

        dee._process_single_event = MagicMock(name='_process_single_event', side_effect=real_process_single_event)

        # 1. pause the engine at global_time+2*time_step 
        pause_time = dee.global_time + 2 * dee.time_step
        dee.pauseAt(pause_time)

        # 2. push another event to make sure the engine wouldn't quit early
        dee.scheduleAtPreciseTime(Event(
            time=pause_time + dee.time_step,
            uniqueTag=('test', 'late_event'),
            callback=lambda :None,
            intraSlotOrder=INTRASLOTORDER_STARTSLOT
        ))

        dee.start()

        import time
        for _ in range(100):           # 100 × 10 ms = 1 s should be enough for engine to handle all events
            if dee.simPaused:
                break
            time.sleep(0.01)

        assert dee.simPaused is True
        assert dee.is_alive() is True        # thread should be paused by semaphore and it still alive

        # play until the end
        dee.play()
        dee.join(timeout=1)
        assert dee.simPaused is False
        assert dee.is_alive() is False
        # _process_single_event is called
        assert dee._process_single_event.called

    def test_terminateSimulation(self):
        dee = DiscreteEventEngine()
        
        def real_process_single_event(event):
            event.callback()

        dee._process_single_event = MagicMock(name='_process_single_event', side_effect=real_process_single_event)
        assert DiscreteEventEngine._instances
        assert dee.goOn is True

        delay = 10
        dee.terminateSimulation(delay)
        dee.start()

        dee.join(timeout=1) # at most 1 second

        assert dee.goOn is False
        assert dee.is_alive() is False

class TestMultiNetworkSimEngine:
    @pytest.fixture(autouse=True)
    def reset_MNE_singleton(self):
        yield
        for k in list(DiscreteEventEngine._instances.keys()):
            instance = DiscreteEventEngine._instances[k]
            if instance:
                if hasattr(instance, "connectivity") and instance.connectivity:
                    instance.connectivity.destroy()

                if SimSettings._instance:
                    SimSettings._instance.destroy()
                if SimLog._instance:
                    SimLog._instance.destroy()
                instance.destroy()

    def init_MNE(self, network_id:str = None):        
        config = {
            "exec_numMotes":                               1,
            'tsch_slotDuration':                           10 * MILLISECOND,
            'tsch_slotframeLength':                        101,
            "exec_numSlotframesPerRun":                    1000,
            "exec_minutesPerRun":                          None,
            "exec_randomSeed":                             "random",
            "secjoin_enabled":                             True,
            "app":                                         "AppPeriodic",
            "app_pkPeriod":                                60,
            "app_pkPeriodVar":                             0.05,
            "app_pkLength":                                90,
            "app_burstTimestamp":                          None,
            "app_burstNumPackets":                         0,
            "rpl_of":                                      "OF0",
            "rpl_daoPeriod":                               60,
            "rpl_extensions":                              ["dis_unicast"],
            "fragmentation":                               "FragmentForwarding",
            "sixlowpan_reassembly_buffers_num":            1,
            "fragmentation_ff_discard_vrb_entry_policy":   [],
            "fragmentation_ff_vrb_table_size":             50,
            "tsch_max_payload_len":                        90,
            "sf_class":                                    "SFNone",
            "tsch_probBcast_ebProb":                       0.33,
            "tsch_clock_max_drift_ppm":                    30,
            "tsch_clock_frequency":                        32768,
            "tsch_keep_alive_interval":                    10,
            "tsch_tx_queue_size":                          10,
            "tsch_max_tx_retries":                         5,
            "radio_stats_log_period_s":                    60,
            "conn_class":                                  "Linear",
            "conn_simulate_ack_drop":                      False,
            "conn_trace":                                  None,
            "conn_random_square_side":                     2.000,
            "conn_random_init_min_pdr":                    0.5,
            "conn_random_init_min_neighbors":              3,
            "phy_numChans":                                16,
            "motes_eui64":                                 []

        }
        settings = SimSettings(cpuID=0, run_id=0, **config)
        settings.setLogDirectory('test_log')
        settings.setCombinationKeys([])

        mne = MultiNetworkSimEngine()

        mne._add_network(
            network_id=mne.default_network_id if network_id is None else network_id
        )

        return mne

    def test_MNE_add_network(self):
        network_id = '0001'
        mne = self.init_MNE(network_id)

        assert network_id in mne.networks

    def test_MNE_delete_network(self):
        network_id = '0001'
        mne = self.init_MNE(network_id=network_id)

        assert network_id in mne.networks

        mne._delete_network(network_id=network_id)

        assert network_id not in mne.networks


    def test_MNE_asn_to_global_time(self):
        network_id = '0001'
        mne = self.init_MNE(network_id=network_id)
        mne.global_time = 1000

        asn = 5
        asn_time = mne.asn_to_global_time(network_id=network_id, asn=asn)
        assert asn_time == mne.settings.tsch_slotDuration * asn  # 5 * 10 + 0 (start_time default is 0)
    
    def test_MNE_global_time_to_asn(self):
        mne = self.init_MNE()
        mne.global_time = 1000
        network_id = mne.default_network_id
        network = mne._get_network(network_id)

        expected_asn = 5
        global_time = network.start_time + expected_asn * mne.settings.tsch_slotDuration
        asn = mne.global_time_to_asn(global_time, network_id)
        assert asn == expected_asn

    def test_MNE_get_current_network_asn(self):
        network_id = '0001'
        mne = self.init_MNE(network_id=network_id)
        mne.global_time = 1000

        mne.global_time += mne.settings.tsch_slotframeLength * mne.settings.tsch_slotDuration  # simulate time after one slotframe
        expected_asn = mne.settings.tsch_slotframeLength

        current_asn = mne._get_current_network_asn(network_id=network_id)
        assert current_asn == expected_asn  # (1000 - 0) // 10 = 100

    def test_MNE_scheduleAtAsn(self):
        network_id = '0001'
        mne = self.init_MNE(network_id=network_id)
        mne.global_time = 1000

        asn = 5
        uniqueTag = 'event1'
        intraSlotOrder = INTRASLOTORDER_STARTSLOT

        mne.scheduleAtAsn(
            network_id=network_id,
            asn=asn,
            cb=None,
            uniqueTag=uniqueTag,
            intraSlotOrder=intraSlotOrder
        )

        # check if event is in schedule
        network = mne._get_network(network_id)
        scheduled_event = mne.uniqueTagSchedule.get(uniqueTag)
        assert scheduled_event is not None
        expected_time = network.start_time + mne.settings.tsch_slotDuration * asn  # 5 * 10 + 0 (start_time default is 0)
        assert scheduled_event.time == expected_time
        assert scheduled_event.uniqueTag == uniqueTag

    def test_NetworkInstance_set_root_mote(self):
        mne = self.init_MNE()
        network = mne._get_network(mne.default_network_id)
        assert network.network_id == mne.default_network_id

        root_mote_id = 0
        root_mote = Mote.Mote.Mote(
            id=root_mote_id,
            eui64=None,
        )

        network._set_root_mote(root_mote)

        assert network.root_mote_id == root_mote_id
        assert len(network.motes) == 1
        assert network.motes[root_mote_id] is root_mote

    def test_NetworkInstance_actionEndSlotframe(self):
        network_id = '0001'
        mne = self.init_MNE(network_id=network_id)
        mne.global_time = 1000
        network = mne._get_network(network_id)

        network._actionEndSlotframe()

        # assert _actionEndSlotframe in event schedule
        scheduled_event = mne.uniqueTagSchedule.get((u'DiscreteEventEngine', u'_actionEndSlotframe'))
        assert scheduled_event is not None
        assert len(mne.events) == 1 


