from typing import NamedTuple
from dataclasses import dataclass, field
import itertools

TIME_RESOLUTION = 'us'  # 'us' for microsecond
SECOND = 1_000_000
MILLISECOND = 1_000
MICROSECOND = 1
TIME_STEP = MICROSECOND * 100  # 100 microseconds


class EventView(NamedTuple):
    time: int
    intraSlotOrder: int
    uniqueTag: tuple
    callback: any          
    cancelled: bool

_event_seq = itertools.count()
@dataclass(order=True)
class Event:
    sort_index: tuple = field(init=False, repr=False)

    time: int
    intraSlotOrder: int
    uniqueTag: tuple
    callback: callable
    cancelled: bool = field(default=False, compare=False)

    @property
    def view(self)->EventView:
        return EventView(
            time=self.time,
            uniqueTag=self.uniqueTag,
            callback=self.callback,
            intraSlotOrder=self.intraSlotOrder,
            cancelled=self.cancelled
        )

    def __post_init__(self):
        self.sort_index = (
            self.time,
            self.intraSlotOrder,
            next(_event_seq),
        )
