from dataclasses import dataclass, field, fields
from typing import Optional, Dict, Any, Type, TypeVar, Iterator, Tuple

T = TypeVar("T")

@dataclass
class DictLike:
    extra: Dict[str, Any] = field(default_factory=dict)

    def __contains__(self, key: str) -> bool:
        return key in self.__dict__ or key in self.extra

    def __getitem__(self, key: str) -> Any:
        if key in self.__dict__:
            return self.__dict__[key]
        if key in self.extra:
            return self.extra[key]
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self.__dict__ and key != "extra":
            setattr(self, key, value)
        else:
            self.extra[key] = value
            
    def __delitem__(self, key: str) -> None:
        if key in self.__dict__ and key != "extra":
            setattr(self, key, None)
        elif key in self.extra:
            del self.extra[key]
        else:
            raise KeyError(key)


    def items(self) -> Iterator[Tuple[str, Any]]:
        for f in fields(self):
            if f.name == "extra":
                continue
            value = getattr(self, f.name)
            yield f.name, value
        yield from self.extra.items()

    def keys(self) -> Iterator[str]:
        for f in fields(self):
            if f.name == "extra":
                continue
            yield f.name
        yield from self.extra.keys()

    def values(self) -> Iterator[Any]:
        for f in fields(self):
            if f.name == "extra":
                continue
            yield getattr(self, f.name)
        yield from self.extra.values()

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __iter__(self) -> Iterator[str]:
        return self.keys()

    def __len__(self) -> int:
        defined_count = len([f for f in fields(self) if f.name != "extra"])
        return defined_count + len(self.extra)


    def to_dict(self) -> Dict[str, Any]:
        result = {}
        for f in fields(self):
            if f.name == "extra":
                continue
            value = getattr(self, f.name)
            if value is not None:
                result[f.name] = value
        result.update(self.extra)
        return result

    @classmethod
    def split_defined_extra(cls, data: Dict[str, Any]):
        defined = {f.name for f in fields(cls) if f.name != "extra"}
        known = {}
        extra = {}
        if data:
            known = {k: v for k, v in data.items() if k in defined}
            extra = {k: v for k, v in data.items() if k not in defined}
        
        return known, extra

@dataclass
class AppInfo(DictLike):
    rank: int = 0
    dodagId: str = ""

@dataclass
class NetInfo(DictLike):
    srcIp: str = ""
    dstIp: str = ""
    hop_limit: int = 64
    downward: bool = False

@dataclass
class MacInfo(DictLike):
    srcMac: str = ""
    dstMac: str = ""
    pending_bit: bool = False
    retriesLeft: int | None = None
    seqnum: int | None = None
    priority: bool | None = None
    join_metric: int | None = None

@dataclass
class Packet(DictLike):
    type: str = ""
    mac: MacInfo | None = None
    app: AppInfo | None = None
    net: NetInfo | None = None
    pkt_len: int = 0

    # def __eq__(self, value):
    #     if self is value:
    #         return True
        
    #     if isinstance(value, dict):
    #         return self._eq_with_dict(value)
        
    #     if not isinstance(value, Packet):
    #         return False
        
    #     return (self.type       == value.type and
    #             self.pkt_len    == value.pkt_len and
    #             self.mac        == value.mac and
    #             self.app        == value.app and
    #             self.net        == value.net and
    #             self.extra      == value.extra)

    # def _eq_with_dict(self, value: dict):
    #     try:
    #         if self.type != value.get('type') or self.pkt_len != value.get('pkt_len'):
    #             return False
            
    #         if self.mac and 'mac' in value:
    #             if not (self.mac == value['mac']): return False
    #         elif self.mac or 'mac' in value: # 一个有一点没有
    #             return False

    #         return True 
    #     except Exception:
    #         return False

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "type": self.type,
            "pkt_len": self.pkt_len,
        }
        if self.mac:
            d["mac"] = self.mac.to_dict()
        if self.app:
            d["app"] = self.app.to_dict()
        if self.net:
            d["net"] = self.net.to_dict()
        d.update(self.extra)
        return d
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Packet":
        data = dict(data) 

        mac = None
        if "mac" in data:
            known, extra = MacInfo.split_defined_extra(data.pop("mac"))
            mac = MacInfo(**known, extra=extra)

        app = None
        if "app" in data:
            known, extra = AppInfo.split_defined_extra(data.pop("app"))
            app = AppInfo(**known, extra=extra)

        net = None
        if "net" in data:
            known, extra = NetInfo.split_defined_extra(data.pop("net"))
            net = NetInfo(**known, extra=extra)

        known, extra = cls.split_defined_extra(data)

        return cls(
            **known,
            mac=mac,
            app=app,
            net=net,
            extra=extra
        )
