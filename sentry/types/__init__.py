import fnmatch

__all__ = [
    'Model', 'SlottedModel', 'Field', 'ListField', 'DictField', 'text', 'snowflake', 'channel', 'raw',
    'rule_matcher', 'lower', 'ChannelField', 'UserField'
]

# --- Disco Type Stubs & Primitives ---
def lower(raw_str): return str(raw_str).lower()
def raw(obj): return obj
def text(obj): return str(obj)
def snowflake(val): return int(val)

def ChannelField(raw_str):
    if isinstance(raw_str, str) and raw_str:
        if raw_str[0] == '#':
            return raw_str[1:]
        elif not raw_str[0].isdigit():
            return raw_str
    return snowflake(raw_str)

def UserField(raw_str):
    return snowflake(raw_str)

# --- Disco Object Relational Model Drop-in Replacement ---
class Field:
    def __init__(self, type_=None, default=None, private=False, create=True):
        self.type = type_
        self.default = default
        self.private = private
        self.create = create
        self.metadata = {'private': private}

class ListField(Field):
    def __init__(self, type_=None, default=None, **kwargs):
        if default is None: default = []
        super().__init__(list, default, **kwargs)
        self.inner_type = type_

class DictField(Field):
    def __init__(self, key_type=None, value_type=None, default=None, **kwargs):
        if default is None: default = {}
        super().__init__(dict, default, **kwargs)
        self.key_type = key_type
        self.value_type = value_type

class ModelMeta(type):
    def __new__(mcs, name, bases, attrs):
        fields = {}
        # Inherit fields from parents
        for base in bases:
            if hasattr(base, '_fields'):
                fields.update(base._fields)
        # Process current class fields
        for k, v in attrs.items():
            if isinstance(v, Field):
                fields[k] = v
        attrs['_fields'] = fields
        return super().__new__(mcs, name, bases, attrs)

class Model(metaclass=ModelMeta):
    def __init__(self, obj=None, **kwargs):
        self.load(obj or kwargs)

    def load(self, obj, *args, **kwargs):
        if not obj: obj = {}
        for name, field in self._fields.items():
            if name in obj:
                val = obj[name]
                # Recursive Sub-Model parsing
                if isinstance(field.type, type) and issubclass(field.type, Model) and isinstance(val, dict):
                    val = field.type(val)
                # Recursive List of Models parsing
                elif isinstance(field, ListField) and isinstance(field.inner_type, type) and issubclass(field.inner_type, Model) and isinstance(val, list):
                    val = [field.inner_type(v) if isinstance(v, dict) else v for v in val]
                # Recursive Dict of Models parsing
                elif isinstance(field, DictField) and isinstance(field.value_type, type) and issubclass(field.value_type, Model) and isinstance(val, dict):
                    val = {k: field.value_type(v) if isinstance(v, dict) else v for k, v in val.items()}
                # Callable parsers (e.g. PluginsConfig.parse)
                elif callable(field.type) and not isinstance(field.type, type):
                    try: val = field.type(val)
                    except Exception: pass
                setattr(self, name, val)
            else:
                default = field.default() if callable(field.default) else getattr(field, 'default', None)
                setattr(self, name, default)
        return self

    def load_into(self, inst, obj):
        if not obj: obj = {}
        for name, field in self._fields.items():
            if name in obj:
                setattr(inst, name, obj[name])
            else:
                default = field.default() if callable(field.default) else getattr(field, 'default', None)
                setattr(inst, name, default)

    def to_dict(self):
        res = {}
        for k in self._fields:
            if hasattr(self, k):
                res[k] = getattr(self, k)
        return res

class SlottedModel(Model):
    # In disco, this enforced __slots__ for memory optimization.
    # We map it directly to Model as modern Python dataclasses achieve the same.
    pass

# --- Filtering and Rule Matching ---
class RuleException(Exception):
    pass

_FUNCS = {
    'length': lambda a: len(a),
}

_FILTERS = {
    'eq': ((str, int, float, list, tuple, set), lambda a, b: a == b),
    'gt': ((int, float), lambda a, b: a > b),
    'lt': ((int, float), lambda a, b: a < b),
    'gte': ((int, float), lambda a, b: a >= b),
    'lte': ((int, float), lambda a, b: a <= b),
    'match': ((str,), lambda a, b: fnmatch.fnmatch(a, b)),
    'contains': ((list, tuple, set), lambda a, b: b in a),
}

def get_object_path(obj, path):
    if '.' not in path:
        return getattr(obj, path, None)
    key, rest = path.split('.', 1)
    return get_object_path(getattr(obj, key, None), rest)

def _check_filter(filter_name, filter_data, value):
    if filter_name in _FUNCS:
        new_value = _FUNCS[filter_name](value)
        if isinstance(filter_data, dict):
            return all([_check_filter(k, v, new_value) for k, v in filter_data.items()])
        return new_value == filter_data
        
    negate = False
    if filter_name.startswith('not_'):
        negate = True
        filter_name = filter_name[4:]
        
    if filter_name not in _FILTERS:
        raise RuleException('unknown filter {}'.format(filter_name))
        
    typs, filt = _FILTERS[filter_name]
    if not isinstance(value, typs):
        raise RuleException('invalid type for filter, have {} but want {}'.format(
            type(value), typs,
        ))
        
    if negate:
        return not filt(value, filter_data)
    return filt(value, filter_data)

def rule_matcher(obj, rules, output_key='out'):
    for rule in rules:
        for field_name, field_rule in rule.items():
            if field_name == output_key:
                continue
                
            field_value = get_object_path(obj, field_name)
            if isinstance(field_rule, dict):
                field_matched = True
                for rule_filter, b in field_rule.items():
                    field_matched = _check_filter(rule_filter, b, field_value)
                if not field_matched:
                    break
            elif field_value != field_rule:
                break
        else:
            yield rule.get(output_key, True)