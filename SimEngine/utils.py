from typing import Any

def dataclass_to_dict(obj: Any) -> Any:
    if obj is None:
        return None
    elif isinstance(obj, (list, tuple)):
        return [dataclass_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: dataclass_to_dict(v) for k, v in obj.items()}
    elif hasattr(obj, '__dataclass_fields__'):  
        if hasattr(obj, 'to_dict'):
            return obj.to_dict()
        else:
            result = {}
            for field_name in obj.__dataclass_fields__:
                field_value = getattr(obj, field_name)
                result[field_name] = dataclass_to_dict(field_value)
            return result
    else:
        return obj
