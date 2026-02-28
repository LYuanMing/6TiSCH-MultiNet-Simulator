"""
Multi-Network Parallel Engine

Expand the existing SimEngine to support parallel simulation of multiple 6TiSCH networks
"""
from __future__ import print_function
from __future__ import absolute_import
from __future__ import division

import inspect
from copy import deepcopy
from builtins import zip
from builtins import str
from builtins import range
import trace
from past.utils import old_div
import threading
import time
import heapq
import sys
import random
import traceback
from collections import defaultdict
import random
import sys
import threading
import time
import traceback
import json

from . import Mote
from . import SimSettings
from . import SimLog
from . import Connectivity
from . import SimConfig
from .SimEngineDefines import TIME_RESOLUTION, TIME_STEP, Event


class SingletonMeta(type):
    _instances = {}
    _lock = threading.RLock()

    def __call__(cls, *args, **kwargs):
        with cls._lock:  # make sure the thread safe
            if cls not in cls._instances:
                cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]

    def reset_singleton(cls):
        with cls._lock:
            cls._instances.pop(cls, None)

class DiscreteEventEngine(threading.Thread, metaclass=SingletonMeta):

    def __init__(self, cpuID=None, run_id=None, verbose=True):
        try:
            # store params
            self.cpuID                          = cpuID
            self.run_id                         = run_id
            self.verbose                        = verbose

            # local variables
            self.dataLock                       = threading.RLock()
            self.pauseSem                       = threading.Semaphore(0)
            self.simPaused                      = False
            self.goOn                           = True
            self.exc                            = None
            self.uniqueTagSchedule              = {}
            self.random_seed                    = None
            self.global_time                    = 0
            self.time_step                      = TIME_STEP
            self.time_resolution                = TIME_RESOLUTION
            self.events                         = []

            # initialize parent class
            threading.Thread.__init__(self)
            self.name                           = u'DiscreteEventEngine'
        except:
            # an exception happened when initializing the instance

            # destroy the singleton
            cls = type(self)
            cls.reset_singleton()
            raise

    def destroy(self):
        cls = type(self)
        with cls._lock:  # make sure the thread safe
            if cls in cls._instances:
                # initialization finished without exception

                if self.is_alive():
                    # thread is start'ed
                    self._actionEndSim()  # causes self.gOn to be set to False
                    self.play()           # cause one more loop in thread
                    self.join(timeout=10)           # wait until thread is dead
                    assert not self.is_alive()
                # destroy the singleton
                cls.reset_singleton()
            else:
                # initialization failed
                pass # do nothing, singleton already destroyed

    #======================== thread ==========================================

    def run(self):
        """ loop through events """
        try:
            # additional routine
            self._routine_thread_started()

            # consume events until self.goOn is False
            while self.goOn:
                with self.dataLock:
                    if not self.events:
                        # no more events to process
                        break
                    
                    self.global_time += self.time_step
                    
                    if self._check_schedule_required():
                        # next event is in the future
                        continue
                    
                    event_list = self._pop_event_until_(self.global_time)
                    self._process_events(event_list)
        except Exception as e:
            # thread crashed

            # record the exception
            self.exc = e

            # additional routine
            self._routine_thread_crashed()

            # print
            output  = []
            output += [u'']
            output += [u'==============================']
            output += [u'']
            output += [u'CRASH in {0}!'.format(self.name)]
            output += [u'']
            output += [traceback.format_exc()]
            output += [u'==============================']
            output += [u'']
            output += [u'The current global time is {0}'.format(self.global_time)]
            output += [u'The log file is {0}'.format(
                self.settings.getOutputFile()
            )]
            output += [u'']
            output += [u'==============================']
            output += [u'config.json to reproduce:']
            output += [u'']
            output += [u'']
            output  = u'\n'.join(output)
            output += json.dumps(
                SimConfig.SimConfig.generate_config(
                    settings_dict = self.settings.__dict__,
                    random_seed   = self.random_seed
                ),
                indent = 4
            )
            output += u'\n\n==============================\n'

            sys.stderr.write(output)

            # flush all the buffered log data
            SimLog.SimLog().flush()

        else:
            # thread ended (gracefully)

            # no exception
            self.exc = None

            # additional routine
            self._routine_thread_ended()

        finally:

            # destroy this singleton
            cls = type(self)
            cls._init                          = False


    def join(self, timeout=None):
        try:
            super(DiscreteEventEngine, self).join(timeout)
            if self.exc:
                print(f"[WARNING] inetr-thread error: {self.exc}")
                print(traceback.format_exc())
                return self.exc
        except Exception as e:
            print("join method error!")
            print(traceback.format_exc())

    #======================== schedule ==========================================

    def _push_event(self, event: Event):
        heapq.heappush(self.events, event)
        self.uniqueTagSchedule[event.uniqueTag] = event

    def _pop_event(self):
        if self.events:
            event = heapq.heappop(self.events)
            if self.uniqueTagSchedule.get(event.uniqueTag) is event: # only remove if it's the same instance
                self.uniqueTagSchedule.pop(event.uniqueTag, None)
            return event
        return None

    def _heap_top(self):
        while self.events:
            event = self.events[0]
            if event.cancelled: # skip cancelled event
                self._pop_event()
                continue
            return event.view
        return None

    def _pop_event_until_(self, time):
        """Pop events until the given time."""
        events = []
        while self._heap_top() and self._heap_top().time <= time:
            event = self._pop_event()
            if event is not None and not event.cancelled:
                events.append(event)
        return events

    def _check_schedule_required(self):
        """Check if scheduling is required (i.e., there are pending events)."""
        if not self.events:
            return False
        heap_top = self._heap_top()
        if not heap_top:
            return False
        return heap_top.time >= self.global_time
    
    def removeFutureEvent(self, uniqueTag):

        if uniqueTag not in self.uniqueTagSchedule:
            # new event, not need to delete old instances
            return

        # get old instances occurences
        event = self.uniqueTagSchedule[uniqueTag]

        # make sure it's in the future
        assert event.time >= self.global_time

        # logic delete it
        event.cancelled = True

    def scheduleAtPreciseTime(self, event: Event):
        """
        Schedule an event at a precise time (depends on time resolution).
        Also removed all future events with the same uniqueTag.
        """

        # make sure we are scheduling in the future
        assert event.time > self.global_time, self.global_time

        # remove all events with same uniqueTag (the event will be rescheduled)
        self.removeFutureEvent(event.uniqueTag)

        self._push_event(event)
                
    # === play/pause

    def play(self):
        self._actionResumeSim()

    def pauseAt(self, pause_time):
        self.scheduleAtPreciseTime(Event(
                time             = pause_time,
                callback         = self._actionPauseSim,
                uniqueTag        = (u'DiscreteEventEngine', u'_actionPauseSim'),
                intraSlotOrder   = Mote.MoteDefines.INTRASLOTORDER_ADMINTASKS
            )
        )

    def pauseAtNextStep(self):
        self.pauseAt(self.global_time + self.time_step)

    # === misc

    def is_scheduled(self, uniqueTag):
        with self.dataLock:
            if uniqueTag in self.uniqueTagSchedule and not self.uniqueTagSchedule[uniqueTag].cancelled:
                return True
            return False


    def terminateSimulation(self,delay):
        with self.dataLock:
            self.asnEndExperiment = self.global_time + delay
            self.scheduleAtPreciseTime(Event(
                time            = self.asnEndExperiment,
                uniqueTag      = (u'DiscreteEventEngine', u'_actionEndSim'),
                callback        = self._actionEndSim,
                intraSlotOrder = Mote.MoteDefines.INTRASLOTORDER_ADMINTASKS,
                cancelled       = False,
            ))

    # ======================== private ========================================

    def _actionPauseSim(self):
        assert self.simPaused==False
        self.simPaused = True
        self.pauseSem.acquire()

    def _actionResumeSim(self):
        if self.simPaused:
            self.simPaused = False
            self.pauseSem.release()

    def _actionEndSim(self):
        self.goOn = False


    # ======================== abstract =======================================

    def _process_events(self, event_list):
        raise NotImplementedError()

    def _init_additional_local_variables(self):
        pass

    def _routine_thread_started(self):
        pass

    def _routine_thread_crashed(self):
        pass

    def _routine_thread_ended(self):
        pass



class NetworkInstance:
    """a 6TiSCH network instance within the MultiNetworkEngine"""

    def __init__(self, engine, network_id, start_time=0):
        self.verbose:bool = engine.verbose
        self.network_id:str = network_id
        self.motes:dict = {}  # mote_id -> Mote instance
        self.start_time = start_time  # global time offset in the engine
        self.root_mote_id = None  # to be set when the network is started
        self.engine:MultiNetworkSimEngine = engine

    def _set_root_mote(self, mote: Mote.Mote):
        """set the root mote ID for a specific network"""
        if self.root_mote_id is not None:
            # we should withdraw the previous root mote and then set the new one
            raise ValueError("Root mote is already set for network {0}".format(self.network_id))
        self.root_mote_id = mote.id
        self.motes[mote.id] = mote
        mote.setDagRoot()
    
    def _actionEndSlotframe(self):
        """Called at each end of slotframe_iteration."""
        slotframe_iteration = int(old_div(self.engine._get_current_network_asn(self.network_id), self.engine.settings.tsch_slotframeLength))
        
        # print
        if self.verbose:
            print(u'Network: {0}   slotframe_iteration: {1}/{2}'.format(self.network_id, slotframe_iteration, self.engine.settings.exec_numSlotframesPerRun-1))
        
        # schedule next statistics collection
        self.engine.scheduleAtAsn(
            network_id       = self.network_id,
            asn              = self.engine._get_current_network_asn(self.network_id) + self.engine.settings.tsch_slotframeLength,
            cb               = self._actionEndSlotframe,
            uniqueTag        = (u'DiscreteEventEngine', u'_actionEndSlotframe'),
            intraSlotOrder   = Mote.MoteDefines.INTRASLOTORDER_ADMINTASKS
        )


class MultiNetworkSimEngine(DiscreteEventEngine):
    """
    MultiNetworkEngine extends DiscreteEventEngine to support multiple 6TiSCH networks running in parallel.
    Each network has its own set of motes and connectivity model.
    """

    def __init__(self, cpuID=None, run_id=None, verbose=True):
        # initialize the parent class
        super(MultiNetworkSimEngine, self).__init__(cpuID, run_id, verbose)

        self.settings = SimSettings.SimSettings()
        
        # multi-network specific variables
        self.networks = {}  # network_id -> NetworkInstance
        self.network_channels = defaultdict(set)  # channel -> set of network_ids using this channel, and this network channels should be initialized in _init_additional_local_variables
        self.connectivity = None

        self.default_network_id = "main"
        # create the main network first
        self._add_network(network_id=self.default_network_id)
    # ======================== multi-net specific =======================================

    def _process_events(self, event_list):
        # For all events in the event_list, we first sort them by time and intraSlotOrder
        event_list.sort(key=lambda e: (e.time, e.intraSlotOrder))
        for event in event_list:
            event.callback()

    def _add_network(self, network_id:str = None):
        """add a new network instance to the engine"""
        if network_id is None:
            network_id = str(random.randint(0, 10000))
        network = NetworkInstance(self, network_id, start_time=self.global_time)
        self.networks[network_id] = network
        return network_id
    
    def _delete_network(self, network_id:str):
        """delete a network instance from the engine"""
        if network_id in self.networks:
            del self.networks[network_id]

    def _get_network(self, network_id:str, should_raise=True)->NetworkInstance:
        """get a network instance by its ID"""
        network = self.networks.get(network_id, None)
        if network is None and should_raise:
            raise ValueError(f"Network ID {network_id} not found in MultiNetworkSimEngine.")
        return network

    def _get_current_network_asn(self, network_id: str):
        # if self.global_time > 0:
        network = self._get_network(network_id)
        current_asn = int(old_div((self.global_time - network.start_time), self.settings.tsch_slotDuration))
        assert type(current_asn) is int
        return current_asn

    def getAsn(self):
        """This function is used to be compatible with the previous one
            TODO: change the code to remove all getAsn"""
        network_id = self.default_network_id
        return self._get_current_network_asn(network_id)

    def getSlotStartTime(self, network_id:str=None):
        if network_id is None:
            raise Warning("network id is None when calling getSlotStartTime, so default network id is used")
            network_id = self.default_network_id
        network = self._get_network(network_id)
        current_asn = int(old_div((self.global_time - network.start_time), self.settings.tsch_slotDuration))
        return current_asn * self.settings.tsch_slotDuration


    def asn_to_global_time(self, asn, network_id:str):
        """convert ASN in a specific network to global time in the engine"""
        network = self._get_network(network_id)
        global_time = asn * self.settings.tsch_slotDuration + network.start_time
        return global_time

    def global_time_to_asn(self, global_time, network_id:str):
        network = self._get_network(network_id)
        asn = int((global_time - network.start_time) / self.settings.tsch_slotDuration)
        return asn

    def scheduleAtAsn(self, asn, cb, uniqueTag, intraSlotOrder, network_id:str = None):
        """schedule an event at a particular ASN in the future"""
        if network_id is None:
            # raise Warning("network id is None when calling getSlotStartTime, so default network id is used")
            assert self.default_network_id is not None, "Default network ID is not set."
            network_id = self.default_network_id
        event_time = self.asn_to_global_time(asn, network_id)
        event = Event(
            time            = event_time,
            uniqueTag      = uniqueTag,
            callback        = cb,
            intraSlotOrder = intraSlotOrder,
            cancelled       = False,
        )
        self.scheduleAtPreciseTime(event)

    def scheduleIn(self, delay, cb, uniqueTag, intraSlotOrder):
        """
        Schedule an event 'delay' seconds into the future.
        Also removed all future events with the same uniqueTag.
        """
        arrival_time = self.global_time + delay
        self.scheduleAtPreciseTime(Event(
            time=arrival_time,
            intraSlotOrder=intraSlotOrder,
            callback=cb,
            uniqueTag=uniqueTag
        ))


class MultiNetworkSimEngineInstance(MultiNetworkSimEngine):    

    def _init_additional_local_variables(self):
        """initialize additional local variables specific to MultiNetworkSimEngine, and it should be called explicitly"""
        for frame_info in inspect.stack():
            # check whether it is called in __init__
            if frame_info.function == "__init__":
                raise RuntimeError(
                    "_init_additional_local_variables cannot be called from __init__"
                )
            
        # set random seed
        if   self.settings.exec_randomSeed == u'random':
            self.random_seed = random.randint(0, sys.maxsize)
        elif self.settings.exec_randomSeed == u'context':
            # with context for exec_randomSeed, an MD5 value of
            # 'startTime-hostname-run_id' is used for a random seed
            import platform
            import hashlib
            startTime = SimConfig.SimConfig.get_startTime()
            if startTime is None:
                startTime = time.time()
            context = (platform.uname()[1], str(startTime), str(self.run_id))
            md5 = hashlib.md5()
            md5.update(u'-'.join(context).encode('utf-8'))
            self.random_seed = int(md5.hexdigest(), 16) % sys.maxsize
        else:
            assert isinstance(self.settings.exec_randomSeed, int)
            self.random_seed = self.settings.exec_randomSeed

        # apply the random seed; log the seed after self.log is initialized
        random.seed(a=self.random_seed)

        if hasattr(self.settings, 'motes_eui64') and self.settings.motes_eui64:
            eui64_table = self.settings.motes_eui64[:]
            if len(eui64_table) < self.settings.exec_numMotes:
                eui64_table.extend([None] * (self.settings.exec_numMotes - len(eui64_table)))
        else:
            eui64_table = [None] * self.settings.exec_numMotes
        # the engine creates all motes
        self.motes = [
            Mote.Mote.Mote(id, eui64)
            for id, eui64 in zip(range(self.settings.exec_numMotes), eui64_table)
        ]
        
        eui64_list = set([mote.get_mac_addr() for mote in self.motes])
        if len(eui64_list) != len(self.motes):
            assert len(eui64_list) < len(self.motes)
            raise ValueError(u'given motes_eui64 causes dulicates')

        self.connectivity               = Connectivity.Connectivity(self)
        self.log                        = SimLog.SimLog().log
        SimLog.SimLog().set_simengine(self)

        # log the random seed
        self.log(
            SimLog.LOG_SIMULATOR_RANDOM_SEED,
            {u'value': self.random_seed}
        )
        # flush buffered logs, which are supposed to be 'config' and
        # 'random_seed' lines, right now. This could help, for instance, when a
        # simulation is stuck by an infinite loop without writing these
        # 'config' and 'random_seed' to a log file.
        SimLog.SimLog().flush()

        # set the network root mote
        network = self._get_network(self.default_network_id)
        network._set_root_mote(self.motes[0])

        # boot all nodes
        for mote in self.motes:
            mote.boot()

    def _routine_thread_started(self):
        # log
        self.log(
            SimLog.LOG_SIMULATOR_STATE,
            {
                u'name':   self.name,
                u'state':  u'started'
            }
        )

        # schedule end of simulation
        self.scheduleAtAsn(
            asn              = self.settings.tsch_slotframeLength*self.settings.exec_numSlotframesPerRun,
            cb               = self._actionEndSim,
            uniqueTag        = (u'SimEngine',u'_actionEndSim'),
            intraSlotOrder   = Mote.MoteDefines.INTRASLOTORDER_ADMINTASKS,
        )

        for network in self.networks.values():
            # schedule first statistics collection for each network
            self.scheduleAtAsn(
                asn              = self._get_current_network_asn(network_id=network.network_id) + self.settings.tsch_slotframeLength,
                cb               = network._actionEndSlotframe,
                uniqueTag        = (u'DiscreteEventEngine', u'_actionEndSlotframe'),
                intraSlotOrder   = Mote.MoteDefines.INTRASLOTORDER_ADMINTASKS
            )

    def _routine_thread_crashed(self):
        # log
        self.log(
            SimLog.LOG_SIMULATOR_STATE,
            {
                "name": self.name,
                "state": "crash"
            }
        )

    def _routine_thread_ended(self):
        # log
        self.log(
            SimLog.LOG_SIMULATOR_STATE,
            {
                "name": self.name,
                "state": "stopped"
            }
        )

    def get_mote_by_mac_addr(self, mac_addr):
        for mote in self.motes:
            if mote.is_my_mac_addr(mac_addr):
                return mote
        return None

